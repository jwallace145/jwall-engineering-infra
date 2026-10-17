from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from jwall_engineering_sites.aws import (
    check_bucket,
    check_role,
    invalidate_distribution,
    role_name_from_arn,
)
from jwall_engineering_sites.errors import PreflightError

if TYPE_CHECKING:
    from mypy_boto3_sts import STSClient

ROLE_ARN = (
    "arn:aws:sts::650503560686:assumed-role/"
    "jwall-engineering-guitar-knowledge-base-ci-plan/gha-plan-pr-1"
)
USER_ARN = "arn:aws:iam::650503560686:user/prog-strength-admin"


def _sts(arn: str) -> tuple[STSClient, Stubber]:
    sts = boto3.client("sts", region_name="us-east-1")
    stub = Stubber(sts)
    stub.add_response(
        "get_caller_identity",
        {"Account": "650503560686", "Arn": arn, "UserId": "AROAEXAMPLE:session"},
    )
    stub.activate()
    return sts, stub


def test_role_name_from_arn() -> None:
    assert role_name_from_arn(ROLE_ARN) == "jwall-engineering-guitar-knowledge-base-ci-plan"
    assert role_name_from_arn(USER_ARN) is None


def test_check_role_accepts_the_expected_role(logger: logging.Logger) -> None:
    sts, _ = _sts(ROLE_ARN)
    assert (
        check_role(sts, "jwall-engineering-guitar-knowledge-base-ci-plan", logger, environ={})
        == ROLE_ARN
    )


def test_check_role_rejects_another_role(logger: logging.Logger) -> None:
    sts, _ = _sts(ROLE_ARN)
    with pytest.raises(
        PreflightError,
        match="must run as the jwall-engineering-guitar-knowledge-base-ci-apply role",
    ):
        check_role(sts, "jwall-engineering-guitar-knowledge-base-ci-apply", logger, environ={})


def test_check_role_rejects_a_user(logger: logging.Logger) -> None:
    sts, _ = _sts(USER_ARN)
    with pytest.raises(PreflightError, match="not an assumed role"):
        check_role(sts, "jwall-engineering-guitar-knowledge-base-ci-plan", logger, environ={})


def test_check_role_can_be_skipped_with_a_warning(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    sts, _ = _sts(USER_ARN)
    with caplog.at_level(logging.WARNING, logger=logger.name):
        arn = check_role(sts, "any-role", logger, environ={"JES_SKIP_ROLE_CHECK": "1"})
    assert arn == USER_ARN
    assert "JES_SKIP_ROLE_CHECK=1" in caplog.text


@mock_aws
def test_check_bucket() -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="exists")
    check_bucket(s3, "exists")
    with pytest.raises(PreflightError, match="does not exist"):
        check_bucket(s3, "missing")


@mock_aws
def test_invalidate_distribution() -> None:
    cloudfront = boto3.client("cloudfront", region_name="us-east-1")
    distribution = cloudfront.create_distribution(
        DistributionConfig={
            "CallerReference": "test",
            "Comment": "",
            "Enabled": True,
            "Origins": {
                "Quantity": 1,
                "Items": [
                    {
                        "Id": "origin",
                        "DomainName": "bucket.s3.us-east-1.amazonaws.com",
                        "S3OriginConfig": {"OriginAccessIdentity": ""},
                    }
                ],
            },
            "DefaultCacheBehavior": {
                "TargetOriginId": "origin",
                "ViewerProtocolPolicy": "redirect-to-https",
                "ForwardedValues": {"QueryString": False, "Cookies": {"Forward": "none"}},
                "MinTTL": 0,
            },
        }
    )
    invalidation = invalidate_distribution(cloudfront, distribution["Distribution"]["Id"], "ref")
    assert invalidation
