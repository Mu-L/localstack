from enum import StrEnum
from typing import Dict, List, Optional, TypedDict

from localstack.aws.api import RequestContext, ServiceException, ServiceRequest, handler

AmazonResourceType = str
ComplianceStatus = bool
ErrorMessage = str
ExceptionMessage = str
ExcludeCompliantResources = bool
IncludeComplianceDetails = bool
LastUpdated = str
MaxResultsGetComplianceSummary = int
PaginationToken = str
Region = str
ResourceARN = str
ResourcesPerPage = int
S3Bucket = str
S3Location = str
Status = str
StatusCode = int
TagKey = str
TagValue = str
TagsPerPage = int
TargetId = str


class ErrorCode(StrEnum):
    InternalServiceException = "InternalServiceException"
    InvalidParameterException = "InvalidParameterException"


class GroupByAttribute(StrEnum):
    TARGET_ID = "TARGET_ID"
    REGION = "REGION"
    RESOURCE_TYPE = "RESOURCE_TYPE"


class TargetIdType(StrEnum):
    ACCOUNT = "ACCOUNT"
    OU = "OU"
    ROOT = "ROOT"


class ConcurrentModificationException(ServiceException):
    code: str = "ConcurrentModificationException"
    sender_fault: bool = False
    status_code: int = 400


class ConstraintViolationException(ServiceException):
    code: str = "ConstraintViolationException"
    sender_fault: bool = False
    status_code: int = 400


class InternalServiceException(ServiceException):
    code: str = "InternalServiceException"
    sender_fault: bool = False
    status_code: int = 400


class InvalidParameterException(ServiceException):
    code: str = "InvalidParameterException"
    sender_fault: bool = False
    status_code: int = 400


class PaginationTokenExpiredException(ServiceException):
    code: str = "PaginationTokenExpiredException"
    sender_fault: bool = False
    status_code: int = 400


class ThrottledException(ServiceException):
    code: str = "ThrottledException"
    sender_fault: bool = False
    status_code: int = 400


TagKeyList = List[TagKey]


class ComplianceDetails(TypedDict, total=False):
    NoncompliantKeys: Optional[TagKeyList]
    KeysWithNoncompliantValues: Optional[TagKeyList]
    ComplianceStatus: Optional[ComplianceStatus]


class DescribeReportCreationInput(ServiceRequest):
    pass


class DescribeReportCreationOutput(TypedDict, total=False):
    Status: Optional[Status]
    S3Location: Optional[S3Location]
    ErrorMessage: Optional[ErrorMessage]


class FailureInfo(TypedDict, total=False):
    StatusCode: Optional[StatusCode]
    ErrorCode: Optional[ErrorCode]
    ErrorMessage: Optional[ErrorMessage]


FailedResourcesMap = Dict[ResourceARN, FailureInfo]
GroupBy = List[GroupByAttribute]
TagKeyFilterList = List[TagKey]
ResourceTypeFilterList = List[AmazonResourceType]
RegionFilterList = List[Region]
TargetIdFilterList = List[TargetId]


class GetComplianceSummaryInput(ServiceRequest):
    TargetIdFilters: Optional[TargetIdFilterList]
    RegionFilters: Optional[RegionFilterList]
    ResourceTypeFilters: Optional[ResourceTypeFilterList]
    TagKeyFilters: Optional[TagKeyFilterList]
    GroupBy: Optional[GroupBy]
    MaxResults: Optional[MaxResultsGetComplianceSummary]
    PaginationToken: Optional[PaginationToken]


NonCompliantResources = int


class Summary(TypedDict, total=False):
    LastUpdated: Optional[LastUpdated]
    TargetId: Optional[TargetId]
    TargetIdType: Optional[TargetIdType]
    Region: Optional[Region]
    ResourceType: Optional[AmazonResourceType]
    NonCompliantResources: Optional[NonCompliantResources]


SummaryList = List[Summary]


class GetComplianceSummaryOutput(TypedDict, total=False):
    SummaryList: Optional[SummaryList]
    PaginationToken: Optional[PaginationToken]


ResourceARNListForGet = List[ResourceARN]
TagValueList = List[TagValue]


class TagFilter(TypedDict, total=False):
    Key: Optional[TagKey]
    Values: Optional[TagValueList]


TagFilterList = List[TagFilter]


class GetResourcesInput(ServiceRequest):
    PaginationToken: Optional[PaginationToken]
    TagFilters: Optional[TagFilterList]
    ResourcesPerPage: Optional[ResourcesPerPage]
    TagsPerPage: Optional[TagsPerPage]
    ResourceTypeFilters: Optional[ResourceTypeFilterList]
    IncludeComplianceDetails: Optional[IncludeComplianceDetails]
    ExcludeCompliantResources: Optional[ExcludeCompliantResources]
    ResourceARNList: Optional[ResourceARNListForGet]


class Tag(TypedDict, total=False):
    Key: TagKey
    Value: TagValue


TagList = List[Tag]


class ResourceTagMapping(TypedDict, total=False):
    ResourceARN: Optional[ResourceARN]
    Tags: Optional[TagList]
    ComplianceDetails: Optional[ComplianceDetails]


ResourceTagMappingList = List[ResourceTagMapping]


class GetResourcesOutput(TypedDict, total=False):
    PaginationToken: Optional[PaginationToken]
    ResourceTagMappingList: Optional[ResourceTagMappingList]


class GetTagKeysInput(ServiceRequest):
    PaginationToken: Optional[PaginationToken]


class GetTagKeysOutput(TypedDict, total=False):
    PaginationToken: Optional[PaginationToken]
    TagKeys: Optional[TagKeyList]


class GetTagValuesInput(ServiceRequest):
    PaginationToken: Optional[PaginationToken]
    Key: TagKey


TagValuesOutputList = List[TagValue]


class GetTagValuesOutput(TypedDict, total=False):
    PaginationToken: Optional[PaginationToken]
    TagValues: Optional[TagValuesOutputList]


ResourceARNListForTagUntag = List[ResourceARN]


class StartReportCreationInput(ServiceRequest):
    S3Bucket: S3Bucket


class StartReportCreationOutput(TypedDict, total=False):
    pass


TagKeyListForUntag = List[TagKey]
TagMap = Dict[TagKey, TagValue]


class TagResourcesInput(ServiceRequest):
    ResourceARNList: ResourceARNListForTagUntag
    Tags: TagMap


class TagResourcesOutput(TypedDict, total=False):
    FailedResourcesMap: Optional[FailedResourcesMap]


class UntagResourcesInput(ServiceRequest):
    ResourceARNList: ResourceARNListForTagUntag
    TagKeys: TagKeyListForUntag


class UntagResourcesOutput(TypedDict, total=False):
    FailedResourcesMap: Optional[FailedResourcesMap]


class ResourcegroupstaggingapiApi:
    service = "resourcegroupstaggingapi"
    version = "2017-01-26"

    @handler("DescribeReportCreation")
    def describe_report_creation(
        self, context: RequestContext, **kwargs
    ) -> DescribeReportCreationOutput:
        raise NotImplementedError

    @handler("GetComplianceSummary")
    def get_compliance_summary(
        self,
        context: RequestContext,
        target_id_filters: TargetIdFilterList | None = None,
        region_filters: RegionFilterList | None = None,
        resource_type_filters: ResourceTypeFilterList | None = None,
        tag_key_filters: TagKeyFilterList | None = None,
        group_by: GroupBy | None = None,
        max_results: MaxResultsGetComplianceSummary | None = None,
        pagination_token: PaginationToken | None = None,
        **kwargs,
    ) -> GetComplianceSummaryOutput:
        raise NotImplementedError

    @handler("GetResources")
    def get_resources(
        self,
        context: RequestContext,
        pagination_token: PaginationToken | None = None,
        tag_filters: TagFilterList | None = None,
        resources_per_page: ResourcesPerPage | None = None,
        tags_per_page: TagsPerPage | None = None,
        resource_type_filters: ResourceTypeFilterList | None = None,
        include_compliance_details: IncludeComplianceDetails | None = None,
        exclude_compliant_resources: ExcludeCompliantResources | None = None,
        resource_arn_list: ResourceARNListForGet | None = None,
        **kwargs,
    ) -> GetResourcesOutput:
        raise NotImplementedError

    @handler("GetTagKeys")
    def get_tag_keys(
        self, context: RequestContext, pagination_token: PaginationToken | None = None, **kwargs
    ) -> GetTagKeysOutput:
        raise NotImplementedError

    @handler("GetTagValues")
    def get_tag_values(
        self,
        context: RequestContext,
        key: TagKey,
        pagination_token: PaginationToken | None = None,
        **kwargs,
    ) -> GetTagValuesOutput:
        raise NotImplementedError

    @handler("StartReportCreation")
    def start_report_creation(
        self, context: RequestContext, s3_bucket: S3Bucket, **kwargs
    ) -> StartReportCreationOutput:
        raise NotImplementedError

    @handler("TagResources")
    def tag_resources(
        self,
        context: RequestContext,
        resource_arn_list: ResourceARNListForTagUntag,
        tags: TagMap,
        **kwargs,
    ) -> TagResourcesOutput:
        raise NotImplementedError

    @handler("UntagResources")
    def untag_resources(
        self,
        context: RequestContext,
        resource_arn_list: ResourceARNListForTagUntag,
        tag_keys: TagKeyListForUntag,
        **kwargs,
    ) -> UntagResourcesOutput:
        raise NotImplementedError
