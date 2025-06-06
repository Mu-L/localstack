import pytest

from localstack.services.cloudformation.v2.utils import is_v2_engine
from localstack.testing.aws.util import is_aws_cloud
from localstack.testing.pytest import markers
from localstack.utils.common import short_uid

TEST_TEMPLATE = """
Resources:
  cert1:
    Type: "AWS::CertificateManager::Certificate"
    Properties:
      DomainName: "{{domain}}"
      DomainValidationOptions:
        - DomainName: "{{domain}}"
          HostedZoneId: zone123  # using dummy ID for now
      ValidationMethod: DNS
Outputs:
  Cert:
    Value: !Ref cert1
"""

pytestmark = pytest.mark.skipif(
    condition=not is_v2_engine() and not is_aws_cloud(),
    reason="Only targeting the new engine",
)


@markers.aws.only_localstack
def test_cfn_acm_certificate(deploy_cfn_template, aws_client):
    domain = f"domain-{short_uid()}.com"
    deploy_cfn_template(template=TEST_TEMPLATE, template_mapping={"domain": domain})

    result = aws_client.acm.list_certificates()["CertificateSummaryList"]
    result = [cert for cert in result if cert["DomainName"] == domain]
    assert result
