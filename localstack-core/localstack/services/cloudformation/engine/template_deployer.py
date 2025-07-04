import base64
import json
import logging
import re
import traceback
import uuid
from typing import Optional

from botocore.exceptions import ClientError

from localstack import config
from localstack.aws.connect import connect_to
from localstack.constants import INTERNAL_AWS_SECRET_ACCESS_KEY
from localstack.services.cloudformation.analytics import track_resource_operation
from localstack.services.cloudformation.deployment_utils import (
    PLACEHOLDER_AWS_NO_VALUE,
    get_action_name_for_resource_change,
    log_not_available_message,
    remove_none_values,
)
from localstack.services.cloudformation.engine.changes import ChangeConfig, ResourceChange
from localstack.services.cloudformation.engine.entities import Stack, StackChangeSet
from localstack.services.cloudformation.engine.parameters import StackParameter
from localstack.services.cloudformation.engine.quirks import VALID_GETATT_PROPERTIES
from localstack.services.cloudformation.engine.resource_ordering import (
    order_changes,
    order_resources,
)
from localstack.services.cloudformation.engine.template_utils import (
    AWS_URL_SUFFIX,
    fn_equals_type_conversion,
    get_deps_for_resource,
)
from localstack.services.cloudformation.resource_provider import (
    Credentials,
    NoResourceProvider,
    OperationStatus,
    ProgressEvent,
    ResourceProviderExecutor,
    ResourceProviderPayload,
    get_resource_type,
)
from localstack.services.cloudformation.service_models import (
    DependencyNotYetSatisfied,
)
from localstack.services.cloudformation.stores import exports_map, find_stack
from localstack.utils.aws.arns import get_partition
from localstack.utils.functions import prevent_stack_overflow
from localstack.utils.json import clone_safe
from localstack.utils.strings import to_bytes, to_str
from localstack.utils.threads import start_worker_thread

from localstack.services.cloudformation.models import *  # noqa: F401, F403, isort:skip
from localstack.utils.urls import localstack_host

ACTION_CREATE = "create"
ACTION_DELETE = "delete"

REGEX_OUTPUT_APIGATEWAY = re.compile(
    rf"^(https?://.+\.execute-api\.)(?:[^-]+-){{2,3}}\d\.(amazonaws\.com|{AWS_URL_SUFFIX})/?(.*)$"
)
REGEX_DYNAMIC_REF = re.compile("{{resolve:([^:]+):(.+)}}")

LOG = logging.getLogger(__name__)

# list of static attribute references to be replaced in {'Fn::Sub': '...'} strings
STATIC_REFS = ["AWS::Region", "AWS::Partition", "AWS::StackName", "AWS::AccountId"]

# Mock value for unsupported type references
MOCK_REFERENCE = "unknown"


class NoStackUpdates(Exception):
    """Exception indicating that no actions are to be performed in a stack update (which is not allowed)"""

    pass


# ---------------------
# CF TEMPLATE HANDLING
# ---------------------


def get_attr_from_model_instance(
    resource: dict,
    attribute_name: str,
    resource_type: str,
    resource_id: str,
    attribute_sub_name: Optional[str] = None,
) -> str:
    if resource["PhysicalResourceId"] == MOCK_REFERENCE:
        LOG.warning(
            "Attribute '%s' requested from unsupported resource with id %s",
            attribute_name,
            resource_id,
        )
        return MOCK_REFERENCE

    properties = resource.get("Properties", {})
    # if there's no entry in VALID_GETATT_PROPERTIES for the resource type we still default to "open" and accept anything
    valid_atts = VALID_GETATT_PROPERTIES.get(resource_type)
    if valid_atts is not None and attribute_name not in valid_atts:
        LOG.warning(
            "Invalid attribute in Fn::GetAtt for %s:  | %s.%s",
            resource_type,
            resource_id,
            attribute_name,
        )
        raise Exception(
            f"Resource type {resource_type} does not support attribute {{{attribute_name}}}"
        )  # TODO: check CFn behavior via snapshot

    attribute_candidate = properties.get(attribute_name)
    if attribute_sub_name:
        return attribute_candidate.get(attribute_sub_name)
    if "." in attribute_name:
        # was used for legacy, but keeping it since it might have to work for a custom resource as well
        if attribute_candidate:
            return attribute_candidate

        # some resources (e.g. ElastiCache) have their readOnly attributes defined as Aa.Bb but the property is named AaBb
        if attribute_candidate := properties.get(attribute_name.replace(".", "")):
            return attribute_candidate

        # accessing nested properties
        parts = attribute_name.split(".")
        attribute = properties
        # TODO: the attribute fetching below is a temporary workaround for the dependency resolution.
        #  It is caused by trying to access the resource attribute that has not been deployed yet.
        #  This should be a hard error.“
        for part in parts:
            if attribute is None:
                return None
            attribute = attribute.get(part)
        return attribute

    # If we couldn't find the attribute, this is actually an irrecoverable error.
    # After the resource has a state of CREATE_COMPLETE, all attributes should already be set.
    # TODO: raise here instead
    # if attribute_candidate is None:
    # raise Exception(
    #     f"Failed to resolve attribute for Fn::GetAtt in {resource_type}: {resource_id}.{attribute_name}"
    # )  # TODO: check CFn behavior via snapshot
    return attribute_candidate


def resolve_ref(
    account_id: str,
    region_name: str,
    stack_name: str,
    resources: dict,
    parameters: dict[str, StackParameter],
    ref: str,
):
    """
    ref always needs to be a static string
    ref can be one of these:
    1. a pseudo-parameter (e.g. AWS::Region)
    2. a parameter
    3. the id of a resource (PhysicalResourceId
    """
    # pseudo parameter
    if ref == "AWS::Region":
        return region_name
    if ref == "AWS::Partition":
        return get_partition(region_name)
    if ref == "AWS::StackName":
        return stack_name
    if ref == "AWS::StackId":
        stack = find_stack(account_id, region_name, stack_name)
        if not stack:
            raise ValueError(f"No stack {stack_name} found")
        return stack.stack_id
    if ref == "AWS::AccountId":
        return account_id
    if ref == "AWS::NoValue":
        return PLACEHOLDER_AWS_NO_VALUE
    if ref == "AWS::NotificationARNs":
        # TODO!
        return {}
    if ref == "AWS::URLSuffix":
        return AWS_URL_SUFFIX

    # parameter
    if parameter := parameters.get(ref):
        parameter_type: str = parameter["ParameterType"]
        parameter_value = parameter.get("ResolvedValue") or parameter.get("ParameterValue")

        if "CommaDelimitedList" in parameter_type or parameter_type.startswith("List<"):
            return [p.strip() for p in parameter_value.split(",")]
        else:
            return parameter_value

    # resource
    resource = resources.get(ref)
    if not resource:
        raise Exception(
            f"Resource target for `Ref {ref}` could not be found. Is there a resource with name {ref} in your stack?"
        )

    return resources[ref].get("PhysicalResourceId")


# Using a @prevent_stack_overflow decorator here to avoid infinite recursion
# in case we load stack exports that have circular dependencies (see issue 3438)
# TODO: Potentially think about a better approach in the future
@prevent_stack_overflow(match_parameters=True)
def resolve_refs_recursively(
    account_id: str,
    region_name: str,
    stack_name: str,
    resources: dict,
    mappings: dict,
    conditions: dict[str, bool],
    parameters: dict,
    value,
):
    result = _resolve_refs_recursively(
        account_id, region_name, stack_name, resources, mappings, conditions, parameters, value
    )

    # localstack specific patches
    if isinstance(result, str):
        # we're trying to filter constructed API urls here (e.g. via Join in the template)
        api_match = REGEX_OUTPUT_APIGATEWAY.match(result)
        if api_match and result in config.CFN_STRING_REPLACEMENT_DENY_LIST:
            return result
        elif api_match:
            prefix = api_match[1]
            host = api_match[2]
            path = api_match[3]
            port = localstack_host().port
            return f"{prefix}{host}:{port}/{path}"

        # basic dynamic reference support
        # see: https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/dynamic-references.html
        # technically there are more restrictions for each of these services but checking each of these
        # isn't really necessary for the current level of emulation
        dynamic_ref_match = REGEX_DYNAMIC_REF.match(result)
        if dynamic_ref_match:
            service_name = dynamic_ref_match[1]
            reference_key = dynamic_ref_match[2]

            # only these 3 services are supported for dynamic references right now
            if service_name == "ssm":
                ssm_client = connect_to(aws_access_key_id=account_id, region_name=region_name).ssm
                try:
                    return ssm_client.get_parameter(Name=reference_key)["Parameter"]["Value"]
                except ClientError as e:
                    LOG.error("client error accessing SSM parameter '%s': %s", reference_key, e)
                    raise
            elif service_name == "ssm-secure":
                ssm_client = connect_to(aws_access_key_id=account_id, region_name=region_name).ssm
                try:
                    return ssm_client.get_parameter(Name=reference_key, WithDecryption=True)[
                        "Parameter"
                    ]["Value"]
                except ClientError as e:
                    LOG.error("client error accessing SSM parameter '%s': %s", reference_key, e)
                    raise
            elif service_name == "secretsmanager":
                # reference key needs to be parsed further
                # because {{resolve:secretsmanager:secret-id:secret-string:json-key:version-stage:version-id}}
                # we match for "secret-id:secret-string:json-key:version-stage:version-id"
                # where
                #   secret-id can either be the secret name or the full ARN of the secret
                #   secret-string *must* be SecretString
                #   all other values are optional
                secret_id = reference_key
                [json_key, version_stage, version_id] = [None, None, None]
                if "SecretString" in reference_key:
                    parts = reference_key.split(":SecretString:")
                    secret_id = parts[0]
                    # json-key, version-stage and version-id are optional.
                    [json_key, version_stage, version_id] = f"{parts[1]}::".split(":")[:3]

                kwargs = {}  # optional args for get_secret_value
                if version_id:
                    kwargs["VersionId"] = version_id
                if version_stage:
                    kwargs["VersionStage"] = version_stage

                secretsmanager_client = connect_to(
                    aws_access_key_id=account_id, region_name=region_name
                ).secretsmanager
                try:
                    secret_value = secretsmanager_client.get_secret_value(
                        SecretId=secret_id, **kwargs
                    )["SecretString"]
                except ClientError:
                    LOG.error("client error while trying to access key '%s': %s", secret_id)
                    raise

                if json_key:
                    json_secret = json.loads(secret_value)
                    if json_key not in json_secret:
                        raise DependencyNotYetSatisfied(
                            resource_ids=secret_id,
                            message=f"Key {json_key} is not yet available in secret {secret_id}.",
                        )
                    return json_secret[json_key]
                else:
                    return secret_value
            else:
                LOG.warning(
                    "Unsupported service for dynamic parameter: service_name=%s", service_name
                )

    return result


@prevent_stack_overflow(match_parameters=True)
def _resolve_refs_recursively(
    account_id: str,
    region_name: str,
    stack_name: str,
    resources: dict,
    mappings: dict,
    conditions: dict,
    parameters: dict,
    value: dict | list | str | bytes | None,
):
    if isinstance(value, dict):
        keys_list = list(value.keys())
        stripped_fn_lower = keys_list[0].lower().split("::")[-1] if len(keys_list) == 1 else None

        # process special operators
        if keys_list == ["Ref"]:
            ref = resolve_ref(
                account_id, region_name, stack_name, resources, parameters, value["Ref"]
            )
            if ref is None:
                msg = 'Unable to resolve Ref for resource "%s" (yet)' % value["Ref"]
                LOG.debug("%s - %s", msg, resources.get(value["Ref"]) or set(resources.keys()))

                raise DependencyNotYetSatisfied(resource_ids=value["Ref"], message=msg)

            ref = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                ref,
            )
            return ref

        if stripped_fn_lower == "getatt":
            attr_ref = value[keys_list[0]]
            attr_ref = attr_ref.split(".") if isinstance(attr_ref, str) else attr_ref
            resource_logical_id = attr_ref[0]
            attribute_name = attr_ref[1]
            attribute_sub_name = attr_ref[2] if len(attr_ref) > 2 else None

            # the attribute name can be a Ref
            attribute_name = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                attribute_name,
            )
            resource = resources.get(resource_logical_id)

            resource_type = get_resource_type(resource)
            resolved_getatt = get_attr_from_model_instance(
                resource,
                attribute_name,
                resource_type,
                resource_logical_id,
                attribute_sub_name,
            )

            # TODO: we should check the deployment state and not try to GetAtt from a resource that is still IN_PROGRESS or hasn't started yet.
            if resolved_getatt is None:
                raise DependencyNotYetSatisfied(
                    resource_ids=resource_logical_id,
                    message=f"Could not resolve attribute '{attribute_name}' on resource '{resource_logical_id}'",
                )

            return resolved_getatt

        if stripped_fn_lower == "join":
            join_values = value[keys_list[0]][1]

            # this can actually be another ref that produces a list as output
            if isinstance(join_values, dict):
                join_values = resolve_refs_recursively(
                    account_id,
                    region_name,
                    stack_name,
                    resources,
                    mappings,
                    conditions,
                    parameters,
                    join_values,
                )

            # resolve reference in the items list
            assert isinstance(join_values, list)
            join_values = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                join_values,
            )

            none_values = [v for v in join_values if v is None]
            if none_values:
                LOG.warning(
                    "Cannot resolve Fn::Join '%s' due to null values: '%s'", value, join_values
                )
                raise Exception(
                    f"Cannot resolve CF Fn::Join {value} due to null values: {join_values}"
                )
            return value[keys_list[0]][0].join([str(v) for v in join_values])

        if stripped_fn_lower == "sub":
            item_to_sub = value[keys_list[0]]

            attr_refs = {r: {"Ref": r} for r in STATIC_REFS}
            if not isinstance(item_to_sub, list):
                item_to_sub = [item_to_sub, {}]
            result = item_to_sub[0]
            item_to_sub[1].update(attr_refs)

            for key, val in item_to_sub[1].items():
                resolved_val = resolve_refs_recursively(
                    account_id,
                    region_name,
                    stack_name,
                    resources,
                    mappings,
                    conditions,
                    parameters,
                    val,
                )

                if isinstance(resolved_val, (list, dict, tuple)):
                    # We don't have access to the resource that's a dependency in this case,
                    # so do the best we can with the resource ids
                    raise DependencyNotYetSatisfied(
                        resource_ids=key, message=f"Could not resolve {val} to terminal value type"
                    )
                result = result.replace("${%s}" % key, str(resolved_val))

            # resolve placeholders
            result = resolve_placeholders_in_string(
                account_id,
                region_name,
                result,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
            )
            return result

        if stripped_fn_lower == "findinmap":
            # "Fn::FindInMap"
            mapping_id = value[keys_list[0]][0]

            if isinstance(mapping_id, dict) and "Ref" in mapping_id:
                # TODO: ??
                mapping_id = resolve_ref(
                    account_id, region_name, stack_name, resources, parameters, mapping_id["Ref"]
                )

            selected_map = mappings.get(mapping_id)
            if not selected_map:
                raise Exception(
                    f"Cannot find Mapping with ID {mapping_id} for Fn::FindInMap: {value[keys_list[0]]} {list(resources.keys())}"
                    # TODO: verify
                )

            first_level_attribute = value[keys_list[0]][1]
            first_level_attribute = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                first_level_attribute,
            )

            if first_level_attribute not in selected_map:
                raise Exception(
                    f"Cannot find map key '{first_level_attribute}' in mapping '{mapping_id}'"
                )
            first_level_mapping = selected_map[first_level_attribute]

            second_level_attribute = value[keys_list[0]][2]
            if not isinstance(second_level_attribute, str):
                second_level_attribute = resolve_refs_recursively(
                    account_id,
                    region_name,
                    stack_name,
                    resources,
                    mappings,
                    conditions,
                    parameters,
                    second_level_attribute,
                )
            if second_level_attribute not in first_level_mapping:
                raise Exception(
                    f"Cannot find map key '{second_level_attribute}' in mapping '{mapping_id}' under key '{first_level_attribute}'"
                )

            return first_level_mapping[second_level_attribute]

        if stripped_fn_lower == "importvalue":
            import_value_key = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                value[keys_list[0]],
            )
            exports = exports_map(account_id, region_name)
            stack_export = exports.get(import_value_key) or {}
            if not stack_export.get("Value"):
                LOG.info(
                    'Unable to find export "%s" in stack "%s", existing export names: %s',
                    import_value_key,
                    stack_name,
                    list(exports.keys()),
                )
                return None
            return stack_export["Value"]

        if stripped_fn_lower == "if":
            condition, option1, option2 = value[keys_list[0]]
            condition = conditions.get(condition)
            if condition is None:
                LOG.warning(
                    "Cannot find condition '%s' in conditions mapping: '%s'",
                    condition,
                    conditions.keys(),
                )
                raise KeyError(
                    f"Cannot find condition '{condition}' in conditions mapping: '{conditions.keys()}'"
                )

            result = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                option1 if condition else option2,
            )
            return result

        if stripped_fn_lower == "condition":
            # FIXME: this should only allow strings, no evaluation should be performed here
            #   see https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/intrinsic-function-reference-condition.html
            key = value[keys_list[0]]
            result = conditions.get(key)
            if result is None:
                LOG.warning("Cannot find key '%s' in conditions: '%s'", key, conditions.keys())
                raise KeyError(f"Cannot find key '{key}' in conditions: '{conditions.keys()}'")
            return result

        if stripped_fn_lower == "not":
            condition = value[keys_list[0]][0]
            condition = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                condition,
            )
            return not condition

        if stripped_fn_lower in ["and", "or"]:
            conditions = value[keys_list[0]]
            results = [
                resolve_refs_recursively(
                    account_id,
                    region_name,
                    stack_name,
                    resources,
                    mappings,
                    conditions,
                    parameters,
                    cond,
                )
                for cond in conditions
            ]
            result = all(results) if stripped_fn_lower == "and" else any(results)
            return result

        if stripped_fn_lower == "equals":
            operand1, operand2 = value[keys_list[0]]
            operand1 = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                operand1,
            )
            operand2 = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                operand2,
            )
            # TODO: investigate type coercion here
            return fn_equals_type_conversion(operand1) == fn_equals_type_conversion(operand2)

        if stripped_fn_lower == "select":
            index, values = value[keys_list[0]]
            index = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                index,
            )
            values = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                values,
            )
            try:
                return values[index]
            except TypeError:
                return values[int(index)]

        if stripped_fn_lower == "split":
            delimiter, string = value[keys_list[0]]
            delimiter = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                delimiter,
            )
            string = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                string,
            )
            return string.split(delimiter)

        if stripped_fn_lower == "getazs":
            region = (
                resolve_refs_recursively(
                    account_id,
                    region_name,
                    stack_name,
                    resources,
                    mappings,
                    conditions,
                    parameters,
                    value["Fn::GetAZs"],
                )
                or region_name
            )

            ec2_client = connect_to(aws_access_key_id=account_id, region_name=region).ec2
            try:
                get_availability_zones = ec2_client.describe_availability_zones()[
                    "AvailabilityZones"
                ]
            except ClientError:
                LOG.error("client error describing availability zones")
                raise

            azs = [az["ZoneName"] for az in get_availability_zones]

            return azs

        if stripped_fn_lower == "base64":
            value_to_encode = value[keys_list[0]]
            value_to_encode = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                value_to_encode,
            )
            return to_str(base64.b64encode(to_bytes(value_to_encode)))

        for key, val in dict(value).items():
            value[key] = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                val,
            )

    if isinstance(value, list):
        # in some cases, intrinsic functions are passed in as, e.g., `[['Fn::Sub', '${MyRef}']]`
        if len(value) == 1 and isinstance(value[0], list) and len(value[0]) == 2:
            inner_list = value[0]
            if str(inner_list[0]).lower().startswith("fn::"):
                return resolve_refs_recursively(
                    account_id,
                    region_name,
                    stack_name,
                    resources,
                    mappings,
                    conditions,
                    parameters,
                    {inner_list[0]: inner_list[1]},
                )

        # remove _aws_no_value_ from resulting references
        clean_list = []
        for item in value:
            temp_value = resolve_refs_recursively(
                account_id,
                region_name,
                stack_name,
                resources,
                mappings,
                conditions,
                parameters,
                item,
            )
            if not (isinstance(temp_value, str) and temp_value == PLACEHOLDER_AWS_NO_VALUE):
                clean_list.append(temp_value)
        value = clean_list

    return value


def resolve_placeholders_in_string(
    account_id: str,
    region_name: str,
    result,
    stack_name: str,
    resources: dict,
    mappings: dict,
    conditions: dict[str, bool],
    parameters: dict,
):
    """
    Resolve individual Fn::Sub variable replacements

    Variables can be template parameter names, resource logical IDs, resource attributes, or a variable in a key-value map
    """

    def _validate_result_type(value: str):
        is_another_account_id = value.isdigit() and len(value) == len(account_id)
        if value == account_id or is_another_account_id:
            return value

        if value.isdigit():
            return int(value)
        else:
            try:
                res = float(value)
                return res
            except ValueError:
                return value

    def _replace(match):
        ref_expression = match.group(1)
        parts = ref_expression.split(".")
        if len(parts) >= 2:
            # Resource attributes specified => Use GetAtt to resolve
            logical_resource_id, _, attr_name = ref_expression.partition(".")
            resolved = get_attr_from_model_instance(
                resources[logical_resource_id],
                attr_name,
                get_resource_type(resources[logical_resource_id]),
                logical_resource_id,
            )
            if resolved is None:
                raise DependencyNotYetSatisfied(
                    resource_ids=logical_resource_id,
                    message=f"Unable to resolve attribute ref {ref_expression}",
                )
            if not isinstance(resolved, str):
                resolved = str(resolved)
            return resolved
        if len(parts) == 1:
            if parts[0] in resources or parts[0].startswith("AWS::"):
                # Logical resource ID or parameter name specified => Use Ref for lookup
                result = resolve_ref(
                    account_id, region_name, stack_name, resources, parameters, parts[0]
                )

                if result is None:
                    raise DependencyNotYetSatisfied(
                        resource_ids=parts[0],
                        message=f"Unable to resolve attribute ref {ref_expression}",
                    )
                # TODO: is this valid?
                # make sure we resolve any functions/placeholders in the extracted string
                result = resolve_refs_recursively(
                    account_id,
                    region_name,
                    stack_name,
                    resources,
                    mappings,
                    conditions,
                    parameters,
                    result,
                )
                # make sure we convert the result to string
                # TODO: do this more systematically
                result = "" if result is None else str(result)
                return result
            elif parts[0] in parameters:
                parameter = parameters[parts[0]]
                parameter_type: str = parameter["ParameterType"]
                parameter_value = parameter.get("ResolvedValue") or parameter.get("ParameterValue")

                if parameter_type in ["CommaDelimitedList"] or parameter_type.startswith("List<"):
                    return [p.strip() for p in parameter_value.split(",")]
                elif parameter_type == "Number":
                    return str(parameter_value)
                else:
                    return parameter_value
            else:
                raise DependencyNotYetSatisfied(
                    resource_ids=parts[0],
                    message=f"Unable to resolve attribute ref {ref_expression}",
                )
        # TODO raise exception here?
        return match.group(0)

    regex = r"\$\{([^\}]+)\}"
    result = re.sub(regex, _replace, result)
    return _validate_result_type(result)


def evaluate_resource_condition(conditions: dict[str, bool], resource: dict) -> bool:
    if condition := resource.get("Condition"):
        return conditions.get(condition, True)
    return True


# -----------------------
# MAIN TEMPLATE DEPLOYER
# -----------------------


class TemplateDeployer:
    def __init__(self, account_id: str, region_name: str, stack):
        self.stack = stack
        self.account_id = account_id
        self.region_name = region_name

    @property
    def resources(self):
        return self.stack.resources

    @property
    def mappings(self):
        return self.stack.mappings

    @property
    def stack_name(self):
        return self.stack.stack_name

    # ------------------
    # MAIN ENTRY POINTS
    # ------------------

    def deploy_stack(self):
        self.stack.set_stack_status("CREATE_IN_PROGRESS")
        try:
            self.apply_changes(
                self.stack,
                self.stack,
                initialize=True,
                action="CREATE",
            )
        except Exception as e:
            log_method = LOG.info
            if config.CFN_VERBOSE_ERRORS:
                log_method = LOG.exception
            log_method("Unable to create stack %s: %s", self.stack.stack_name, e)
            self.stack.set_stack_status("CREATE_FAILED")
            raise

    def apply_change_set(self, change_set: StackChangeSet):
        action = (
            "UPDATE"
            if change_set.stack.status in {"CREATE_COMPLETE", "UPDATE_COMPLETE"}
            else "CREATE"
        )
        change_set.stack.set_stack_status(f"{action}_IN_PROGRESS")
        # update parameters on parent stack
        change_set.stack.set_resolved_parameters(change_set.resolved_parameters)
        # update conditions on parent stack
        change_set.stack.set_resolved_stack_conditions(change_set.resolved_conditions)

        # update attributes that the stack inherits from the changeset
        change_set.stack.metadata["Capabilities"] = change_set.metadata.get("Capabilities")

        try:
            self.apply_changes(
                change_set.stack,
                change_set,
                action=action,
            )
        except Exception as e:
            LOG.info(
                "Unable to apply change set %s: %s", change_set.metadata.get("ChangeSetName"), e
            )
            change_set.metadata["Status"] = f"{action}_FAILED"
            self.stack.set_stack_status(f"{action}_FAILED")
            raise

    def update_stack(self, new_stack):
        self.stack.set_stack_status("UPDATE_IN_PROGRESS")
        # apply changes
        self.apply_changes(self.stack, new_stack, action="UPDATE")
        self.stack.set_time_attribute("LastUpdatedTime")

    # ----------------------------
    # DEPENDENCY RESOLUTION UTILS
    # ----------------------------

    def is_deployed(self, resource):
        return self.stack.resource_states.get(resource["LogicalResourceId"], {}).get(
            "ResourceStatus"
        ) in [
            "CREATE_COMPLETE",
            "UPDATE_COMPLETE",
        ]

    def all_resource_dependencies_satisfied(self, resource) -> bool:
        unsatisfied = self.get_unsatisfied_dependencies(resource)
        return not unsatisfied

    def get_unsatisfied_dependencies(self, resource):
        res_deps = self.get_resource_dependencies(
            resource
        )  # the output here is currently a set of merged IDs from both resources and parameters
        parameter_deps = {d for d in res_deps if d in self.stack.resolved_parameters}
        resource_deps = res_deps.difference(parameter_deps)
        res_deps_mapped = {v: self.stack.resources.get(v) for v in resource_deps}
        return self.get_unsatisfied_dependencies_for_resources(res_deps_mapped, resource)

    def get_unsatisfied_dependencies_for_resources(
        self, resources, depending_resource=None, return_first=True
    ):
        result = {}
        for resource_id, resource in resources.items():
            if not resource:
                raise Exception(
                    f"Resource '{resource_id}' not found in stack {self.stack.stack_name}"
                )
            if not self.is_deployed(resource):
                LOG.debug(
                    "Dependency for resource %s not yet deployed: %s %s",
                    depending_resource,
                    resource_id,
                    resource,
                )
                result[resource_id] = resource
                if return_first:
                    break
        return result

    def get_resource_dependencies(self, resource: dict) -> set[str]:
        """
        Takes a resource and returns its dependencies on other resources via a str -> str mapping
        """
        # Note: using the original, unmodified template here to preserve Ref's ...
        raw_resources = self.stack.template_original["Resources"]
        raw_resource = raw_resources[resource["LogicalResourceId"]]
        return get_deps_for_resource(raw_resource, self.stack.resolved_conditions)

    # -----------------
    # DEPLOYMENT UTILS
    # -----------------

    def init_resource_status(self, resources=None, stack=None, action="CREATE"):
        resources = resources or self.resources
        stack = stack or self.stack
        for resource_id, resource in resources.items():
            stack.set_resource_status(resource_id, f"{action}_IN_PROGRESS")

    def get_change_config(
        self, action: str, resource: dict, change_set_id: Optional[str] = None
    ) -> ChangeConfig:
        result = ChangeConfig(
            **{
                "Type": "Resource",
                "ResourceChange": ResourceChange(
                    **{
                        "Action": action,
                        # TODO(srw): how can the resource not contain a logical resource id?
                        "LogicalResourceId": resource.get("LogicalResourceId"),
                        "PhysicalResourceId": resource.get("PhysicalResourceId"),
                        "ResourceType": resource["Type"],
                        # TODO ChangeSetId is only set for *nested* change sets
                        # "ChangeSetId": change_set_id,
                        "Scope": [],  # TODO
                        "Details": [],  # TODO
                    }
                ),
            }
        )
        if action == "Modify":
            result["ResourceChange"]["Replacement"] = "False"
        return result

    def resource_config_differs(self, resource_new):
        """Return whether the given resource properties differ from the existing config (for stack updates)."""
        # TODO: this is broken for default fields and result_handler property modifications when they're added to the properties in the model
        resource_id = resource_new["LogicalResourceId"]
        resource_old = self.resources[resource_id]
        props_old = resource_old.get("SpecifiedProperties", {})
        props_new = resource_new["Properties"]
        ignored_keys = ["LogicalResourceId", "PhysicalResourceId"]
        old_keys = set(props_old.keys()) - set(ignored_keys)
        new_keys = set(props_new.keys()) - set(ignored_keys)
        if old_keys != new_keys:
            return True
        for key in old_keys:
            if props_old[key] != props_new[key]:
                return True
        old_status = self.stack.resource_states.get(resource_id) or {}
        previous_state = (
            old_status.get("PreviousResourceStatus") or old_status.get("ResourceStatus") or ""
        )
        if old_status and "DELETE" in previous_state:
            return True

    # TODO: ?
    def merge_properties(self, resource_id: str, old_stack, new_stack) -> None:
        old_resources = old_stack.template["Resources"]
        new_resources = new_stack.template["Resources"]
        new_resource = new_resources[resource_id]

        old_resource = old_resources[resource_id] = old_resources.get(resource_id) or {}
        for key, value in new_resource.items():
            if key == "Properties":
                continue
            old_resource[key] = old_resource.get(key, value)
        old_res_props = old_resource["Properties"] = old_resource.get("Properties", {})
        for key, value in new_resource["Properties"].items():
            old_res_props[key] = value

        old_res_props = {
            k: v for k, v in old_res_props.items() if k in new_resource["Properties"].keys()
        }
        old_resource["Properties"] = old_res_props

        # overwrite original template entirely
        old_stack.template_original["Resources"][resource_id] = new_stack.template_original[
            "Resources"
        ][resource_id]

    def construct_changes(
        self,
        existing_stack,
        new_stack,
        # TODO: remove initialize argument from here, and determine action based on resource status
        initialize: Optional[bool] = False,
        change_set_id=None,
        append_to_changeset: Optional[bool] = False,
        filter_unchanged_resources: Optional[bool] = False,
    ) -> list[ChangeConfig]:
        old_resources = existing_stack.template["Resources"]
        new_resources = new_stack.template["Resources"]
        deletes = [val for key, val in old_resources.items() if key not in new_resources]
        adds = [val for key, val in new_resources.items() if initialize or key not in old_resources]
        modifies = [
            val for key, val in new_resources.items() if not initialize and key in old_resources
        ]

        changes = []
        for action, items in (("Remove", deletes), ("Add", adds), ("Modify", modifies)):
            for item in items:
                item["Properties"] = item.get("Properties", {})
                if (
                    not filter_unchanged_resources  # TODO: find out purpose of this
                    or action != "Modify"
                    or self.resource_config_differs(item)
                ):
                    change = self.get_change_config(action, item, change_set_id=change_set_id)
                    changes.append(change)

        # append changes to change set
        if append_to_changeset and isinstance(new_stack, StackChangeSet):
            new_stack.changes.extend(changes)

        return changes

    def apply_changes(
        self,
        existing_stack: Stack,
        new_stack: StackChangeSet,
        change_set_id: Optional[str] = None,
        initialize: Optional[bool] = False,
        action: Optional[str] = None,
    ):
        old_resources = existing_stack.template["Resources"]
        new_resources = new_stack.template["Resources"]
        action = action or "CREATE"
        # TODO: this seems wrong, not every resource here will be in an UPDATE_IN_PROGRESS state? (only the ones that will actually be updated)
        self.init_resource_status(old_resources, action="UPDATE")

        # apply parameter changes to existing stack
        # self.apply_parameter_changes(existing_stack, new_stack)

        # construct changes
        changes = self.construct_changes(
            existing_stack,
            new_stack,
            initialize=initialize,
            change_set_id=change_set_id,
        )

        # check if we have actual changes in the stack, and prepare properties
        contains_changes = False
        for change in changes:
            res_action = change["ResourceChange"]["Action"]
            resource = new_resources.get(change["ResourceChange"]["LogicalResourceId"])
            #  FIXME: we need to resolve refs before diffing to detect if for example a parameter causes the change or not
            #   unfortunately this would currently cause issues because we might not be able to resolve everything yet
            # resource = resolve_refs_recursively(
            #     self.stack_name,
            #     self.resources,
            #     self.mappings,
            #     self.stack.resolved_conditions,
            #     self.stack.resolved_parameters,
            #     resource,
            # )
            if res_action in ["Add", "Remove"] or self.resource_config_differs(resource):
                contains_changes = True
            if res_action in ["Modify", "Add"]:
                # mutating call that overwrites resource properties with new properties and overwrites the template in old stack with new template
                self.merge_properties(resource["LogicalResourceId"], existing_stack, new_stack)
        if not contains_changes:
            raise NoStackUpdates("No updates are to be performed.")

        # merge stack outputs and conditions
        existing_stack.outputs.update(new_stack.outputs)
        existing_stack.conditions.update(new_stack.conditions)

        # TODO: ideally the entire template has to be replaced, but tricky at this point
        existing_stack.template["Metadata"] = new_stack.template.get("Metadata")
        existing_stack.template_body = new_stack.template_body

        # start deployment loop
        return self.apply_changes_in_loop(
            changes, existing_stack, action=action, new_stack=new_stack
        )

    def apply_changes_in_loop(
        self,
        changes: list[ChangeConfig],
        stack: Stack,
        action: Optional[str] = None,
        new_stack=None,
    ):
        def _run(*args):
            status_reason = None
            try:
                self.do_apply_changes_in_loop(changes, stack)
                status = f"{action}_COMPLETE"
            except Exception as e:
                log_method = LOG.debug
                if config.CFN_VERBOSE_ERRORS:
                    log_method = LOG.exception
                log_method(
                    'Error applying changes for CloudFormation stack "%s": %s %s',
                    stack.stack_name,
                    e,
                    traceback.format_exc(),
                )
                status = f"{action}_FAILED"
                status_reason = str(e)
            stack.set_stack_status(status, status_reason)
            if isinstance(new_stack, StackChangeSet):
                new_stack.metadata["Status"] = status
                exec_result = "EXECUTE_FAILED" if "FAILED" in status else "EXECUTE_COMPLETE"
                new_stack.metadata["ExecutionStatus"] = exec_result
                result = "failed" if "FAILED" in status else "succeeded"
                new_stack.metadata["StatusReason"] = status_reason or f"Deployment {result}"

        # run deployment in background loop, to avoid client network timeouts
        return start_worker_thread(_run)

    def prepare_should_deploy_change(
        self, resource_id: str, change: ResourceChange, stack, new_resources: dict
    ) -> bool:
        """
        TODO: document
        """
        resource = new_resources[resource_id]
        res_change = change["ResourceChange"]
        action = res_change["Action"]

        # check resource condition, if present
        if not evaluate_resource_condition(stack.resolved_conditions, resource):
            LOG.debug(
                'Skipping deployment of "%s", as resource condition evaluates to false', resource_id
            )
            return False

        # resolve refs in resource details
        resolve_refs_recursively(
            self.account_id,
            self.region_name,
            stack.stack_name,
            stack.resources,
            stack.mappings,
            stack.resolved_conditions,
            stack.resolved_parameters,
            resource,
        )

        if action in ["Add", "Modify"]:
            is_deployed = self.is_deployed(resource)
            # TODO: Attaching the cached _deployed info here, as we should not change the "Add"/"Modify" attribute
            #  here, which is used further down the line to determine the resource action CREATE/UPDATE. This is a
            #  temporary workaround for now - to be refactored once we introduce proper stack resource state models.
            res_change["_deployed"] = is_deployed
            if not is_deployed:
                return True
            if action == "Add":
                return False
        elif action == "Remove":
            return True
        return True

    # Stack is needed here
    def apply_change(self, change: ChangeConfig, stack: Stack) -> None:
        change_details = change["ResourceChange"]
        action = change_details["Action"]
        resource_id = change_details["LogicalResourceId"]
        resources = stack.resources
        resource = resources[resource_id]

        # TODO: this should not be needed as resources are filtered out if the
        # condition evaluates to False.
        if not evaluate_resource_condition(stack.resolved_conditions, resource):
            return

        # remove AWS::NoValue entries
        resource_props = resource.get("Properties")
        if resource_props:
            resource["Properties"] = remove_none_values(resource_props)

        executor = self.create_resource_provider_executor()
        resource_provider_payload = self.create_resource_provider_payload(
            action, logical_resource_id=resource_id
        )

        resource_type = get_resource_type(resource)
        resource_provider = executor.try_load_resource_provider(resource_type)
        track_resource_operation(action, resource_type, missing=resource_provider is None)
        if resource_provider is not None:
            # add in-progress event
            resource_status = f"{get_action_name_for_resource_change(action)}_IN_PROGRESS"
            physical_resource_id = None
            if action in ("Modify", "Remove"):
                previous_state = self.resources[resource_id].get("_last_deployed_state")
                if not previous_state:
                    # TODO: can this happen?
                    previous_state = self.resources[resource_id]["Properties"]
                physical_resource_id = executor.extract_physical_resource_id_from_model_with_schema(
                    resource_model=previous_state,
                    resource_type=resource["Type"],
                    resource_type_schema=resource_provider.SCHEMA,
                )
            stack.add_stack_event(
                resource_id=resource_id,
                physical_res_id=physical_resource_id,
                status=resource_status,
            )

            # perform the deploy
            progress_event = executor.deploy_loop(
                resource_provider, resource, resource_provider_payload
            )
        else:
            # track that we don't handle the resource, and possibly raise an exception
            log_not_available_message(
                resource_type,
                f'No resource provider found for "{resource_type}"',
            )

            if not config.CFN_IGNORE_UNSUPPORTED_RESOURCE_TYPES:
                raise NoResourceProvider

            resource["PhysicalResourceId"] = MOCK_REFERENCE
            progress_event = ProgressEvent(OperationStatus.SUCCESS, resource_model={})

        # TODO: clean up the surrounding loop (do_apply_changes_in_loop) so that the responsibilities are clearer
        stack_action = get_action_name_for_resource_change(action)
        match progress_event.status:
            case OperationStatus.FAILED:
                stack.set_resource_status(
                    resource_id,
                    f"{stack_action}_FAILED",
                    status_reason=progress_event.message or "",
                )
                # TODO: remove exception raising here?
                # TODO: fix request token
                raise Exception(
                    f'Resource handler returned message: "{progress_event.message}" (RequestToken: 10c10335-276a-33d3-5c07-018b684c3d26, HandlerErrorCode: InvalidRequest){progress_event.error_code}'
                )
            case OperationStatus.SUCCESS:
                stack.set_resource_status(resource_id, f"{stack_action}_COMPLETE")
            case OperationStatus.PENDING:
                # signal to the main loop that we should come back to this resource in the future
                raise DependencyNotYetSatisfied(
                    resource_ids=[], message="Resource dependencies not yet satisfied"
                )
            case OperationStatus.IN_PROGRESS:
                raise Exception("Resource deployment loop should not finish in this state")
            case unknown_status:
                raise Exception(f"Unknown operation status: {unknown_status}")

        # TODO: this is probably already done in executor, try removing this
        resource["Properties"] = progress_event.resource_model

    def create_resource_provider_executor(self) -> ResourceProviderExecutor:
        return ResourceProviderExecutor(
            stack_name=self.stack.stack_name,
            stack_id=self.stack.stack_id,
        )

    def create_resource_provider_payload(
        self, action: str, logical_resource_id: str
    ) -> ResourceProviderPayload:
        # FIXME: use proper credentials
        creds: Credentials = {
            "accessKeyId": self.account_id,
            "secretAccessKey": INTERNAL_AWS_SECRET_ACCESS_KEY,
            "sessionToken": "",
        }
        resource = self.resources[logical_resource_id]

        resource_provider_payload: ResourceProviderPayload = {
            "awsAccountId": self.account_id,
            "callbackContext": {},
            "stackId": self.stack.stack_name,
            "resourceType": resource["Type"],
            "resourceTypeVersion": "000000",
            # TODO: not actually a UUID
            "bearerToken": str(uuid.uuid4()),
            "region": self.region_name,
            "action": action,
            "requestData": {
                "logicalResourceId": logical_resource_id,
                "resourceProperties": resource["Properties"],
                "previousResourceProperties": resource.get("_last_deployed_state"),  # TODO
                "callerCredentials": creds,
                "providerCredentials": creds,
                "systemTags": {},
                "previousSystemTags": {},
                "stackTags": {},
                "previousStackTags": {},
            },
        }
        return resource_provider_payload

    def delete_stack(self):
        if not self.stack:
            return
        self.stack.set_stack_status("DELETE_IN_PROGRESS")
        stack_resources = list(self.stack.resources.values())
        resources = {r["LogicalResourceId"]: clone_safe(r) for r in stack_resources}
        original_resources = self.stack.template_original["Resources"]

        # TODO: what is this doing?
        for key, resource in resources.items():
            resource["Properties"] = resource.get(
                "Properties", clone_safe(resource)
            )  # TODO: why is there a fallback?
            resource["ResourceType"] = get_resource_type(resource)

        ordered_resource_ids = list(
            order_resources(
                resources=original_resources,
                resolved_conditions=self.stack.resolved_conditions,
                resolved_parameters=self.stack.resolved_parameters,
                reverse=True,
            ).keys()
        )
        for i, resource_id in enumerate(ordered_resource_ids):
            resource = resources[resource_id]
            resource_type = get_resource_type(resource)
            try:
                # TODO: cache condition value in resource details on deployment and use cached value here
                if not evaluate_resource_condition(
                    self.stack.resolved_conditions,
                    resource,
                ):
                    continue

                action = "Remove"
                executor = self.create_resource_provider_executor()
                resource_provider_payload = self.create_resource_provider_payload(
                    action, logical_resource_id=resource_id
                )
                LOG.debug(
                    'Handling "Remove" for resource "%s" (%s/%s) type "%s"',
                    resource_id,
                    i + 1,
                    len(resources),
                    resource_type,
                )
                resource_provider = executor.try_load_resource_provider(resource_type)
                track_resource_operation(action, resource_type, missing=resource_provider is None)
                if resource_provider is not None:
                    event = executor.deploy_loop(
                        resource_provider, resource, resource_provider_payload
                    )
                else:
                    log_not_available_message(
                        resource_type,
                        f'No resource provider found for "{resource_type}"',
                    )

                    if not config.CFN_IGNORE_UNSUPPORTED_RESOURCE_TYPES:
                        raise NoResourceProvider
                    event = ProgressEvent(OperationStatus.SUCCESS, resource_model={})
                match event.status:
                    case OperationStatus.SUCCESS:
                        self.stack.set_resource_status(resource_id, "DELETE_COMPLETE")
                    case OperationStatus.PENDING:
                        # the resource is still being deleted, specifically the provider has
                        # signalled that the deployment loop should skip this resource this
                        # time and come back to it later, likely due to unmet child
                        # resources still existing because we don't delete things in the
                        # correct order yet.
                        continue
                    case OperationStatus.FAILED:
                        LOG.exception(
                            "Failed to delete resource with id %s. Reason: %s",
                            resource_id,
                            event.message or "unknown",
                        )
                    case OperationStatus.IN_PROGRESS:
                        # the resource provider executor should not return this state, so
                        # this state is a programming error
                        raise Exception(
                            "Programming error: ResourceProviderExecutor cannot return IN_PROGRESS"
                        )
                    case other_status:
                        raise Exception(f"Use of unsupported status found: {other_status}")

            except Exception as e:
                LOG.exception(
                    "Failed to delete resource with id %s. Final exception: %s",
                    resource_id,
                    e,
                )

        # update status
        self.stack.set_stack_status("DELETE_COMPLETE")
        self.stack.set_time_attribute("DeletionTime")

    def do_apply_changes_in_loop(self, changes: list[ChangeConfig], stack: Stack) -> list:
        # apply changes in a retry loop, to resolve resource dependencies and converge to the target state
        changes_done = []
        new_resources = stack.resources

        sorted_changes = order_changes(
            given_changes=changes,
            resources=new_resources,
            resolved_conditions=stack.resolved_conditions,
            resolved_parameters=stack.resolved_parameters,
        )
        for change_idx, change in enumerate(sorted_changes):
            res_change = change["ResourceChange"]
            action = res_change["Action"]
            is_add_or_modify = action in ["Add", "Modify"]
            resource_id = res_change["LogicalResourceId"]

            # TODO: do resolve_refs_recursively once here
            try:
                if is_add_or_modify:
                    should_deploy = self.prepare_should_deploy_change(
                        resource_id, change, stack, new_resources
                    )
                    LOG.debug(
                        'Handling "%s" for resource "%s" (%s/%s) type "%s" (should_deploy=%s)',
                        action,
                        resource_id,
                        change_idx + 1,
                        len(changes),
                        res_change["ResourceType"],
                        should_deploy,
                    )
                    if not should_deploy:
                        stack_action = get_action_name_for_resource_change(action)
                        stack.set_resource_status(resource_id, f"{stack_action}_COMPLETE")
                        continue
                elif action == "Remove":
                    should_remove = self.prepare_should_deploy_change(
                        resource_id, change, stack, new_resources
                    )
                    if not should_remove:
                        continue
                    LOG.debug(
                        'Handling "%s" for resource "%s" (%s/%s) type "%s"',
                        action,
                        resource_id,
                        change_idx + 1,
                        len(changes),
                        res_change["ResourceType"],
                    )
                self.apply_change(change, stack=stack)
                changes_done.append(change)
            except Exception as e:
                status_action = {
                    "Add": "CREATE",
                    "Modify": "UPDATE",
                    "Dynamic": "UPDATE",
                    "Remove": "DELETE",
                }[action]
                stack.add_stack_event(
                    resource_id=resource_id,
                    physical_res_id=new_resources[resource_id].get("PhysicalResourceId"),
                    status=f"{status_action}_FAILED",
                    status_reason=str(e),
                )
                if config.CFN_VERBOSE_ERRORS:
                    LOG.exception("Failed to deploy resource %s, stack deploy failed", resource_id)
                raise

        # clean up references to deleted resources in stack
        deletes = [c for c in changes_done if c["ResourceChange"]["Action"] == "Remove"]
        for delete in deletes:
            stack.template["Resources"].pop(delete["ResourceChange"]["LogicalResourceId"], None)

        # resolve outputs
        stack.resolved_outputs = resolve_outputs(self.account_id, self.region_name, stack)

        return changes_done


# FIXME: resolve_refs_recursively should not be needed, the resources themselves should have those values available already
def resolve_outputs(account_id: str, region_name: str, stack) -> list[dict]:
    result = []
    for k, details in stack.outputs.items():
        if not evaluate_resource_condition(stack.resolved_conditions, details):
            continue
        value = None
        try:
            resolve_refs_recursively(
                account_id,
                region_name,
                stack.stack_name,
                stack.resources,
                stack.mappings,
                stack.resolved_conditions,
                stack.resolved_parameters,
                details,
            )
            value = details["Value"]
        except Exception as e:
            log_method = LOG.debug
            if config.CFN_VERBOSE_ERRORS:
                raise  # unresolvable outputs cause a stack failure
                # log_method = getattr(LOG, "exception")
            log_method("Unable to resolve references in stack outputs: %s - %s", details, e)
        exports = details.get("Export") or {}
        export = exports.get("Name")
        export = resolve_refs_recursively(
            account_id,
            region_name,
            stack.stack_name,
            stack.resources,
            stack.mappings,
            stack.resolved_conditions,
            stack.resolved_parameters,
            export,
        )
        description = details.get("Description")
        entry = {
            "OutputKey": k,
            "OutputValue": value,
            "Description": description,
            "ExportName": export,
        }
        result.append(entry)
    return result
