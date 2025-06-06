import os

import pytest

from localstack.services.cloudformation.v2.utils import is_v2_engine
from localstack.testing.aws.util import is_aws_cloud
from localstack.testing.pytest import markers

pytestmark = pytest.mark.skipif(
    condition=not is_v2_engine() and not is_aws_cloud(),
    reason="Only targeting the new engine",
)


@markers.aws.validated
def test_create_record_set_via_id(route53_hosted_zone, deploy_cfn_template):
    create_zone_response = route53_hosted_zone()
    hosted_zone_id = create_zone_response["HostedZone"]["Id"]
    route53_name = create_zone_response["HostedZone"]["Name"]
    parameters = {"HostedZoneId": hosted_zone_id, "Name": route53_name}
    deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__), "../../../../../templates/route53_hostedzoneid_template.yaml"
        ),
        parameters=parameters,
        max_wait=300,
    )


@markers.aws.validated
def test_create_record_set_via_name(deploy_cfn_template, route53_hosted_zone):
    create_zone_response = route53_hosted_zone()
    route53_name = create_zone_response["HostedZone"]["Name"]
    parameters = {"HostedZoneName": route53_name, "Name": route53_name}
    deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__),
            "../../../../../templates/route53_hostedzonename_template.yaml",
        ),
        parameters=parameters,
    )


@markers.aws.validated
def test_create_record_set_without_resource_record(deploy_cfn_template, route53_hosted_zone):
    create_zone_response = route53_hosted_zone()
    hosted_zone_id = create_zone_response["HostedZone"]["Id"]
    route53_name = create_zone_response["HostedZone"]["Name"]
    parameters = {"HostedZoneId": hosted_zone_id, "Name": route53_name}
    deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__),
            "../../../../../templates/route53_recordset_without_resource_records.yaml",
        ),
        parameters=parameters,
    )


@markers.aws.validated
@markers.snapshot.skip_snapshot_verify(
    paths=["$..HealthCheckConfig.EnableSNI", "$..HealthCheckVersion"]
)
def test_create_health_check(deploy_cfn_template, aws_client, snapshot):
    stack = deploy_cfn_template(
        template_path=os.path.join(
            os.path.dirname(__file__),
            "../../../../../templates/route53_healthcheck.yml",
        ),
    )
    health_check_id = stack.outputs["HealthCheckId"]
    print(health_check_id)
    health_check = aws_client.route53.get_health_check(HealthCheckId=health_check_id)

    snapshot.add_transformer(snapshot.transform.key_value("Id", "id"))
    snapshot.add_transformer(snapshot.transform.key_value("CallerReference", "caller-reference"))
    snapshot.match("HealthCheck", health_check["HealthCheck"])
