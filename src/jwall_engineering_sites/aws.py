"""AWS preflight checks and the small AWS calls the commands make.

Everything here runs before Terraform or S3 is touched, so a wrong role or a
missing bucket is a one-line error instead of an AccessDenied halfway through.
"""

from __future__ import annotations

import os
import re
from logging import Logger
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from .errors import PreflightError, RunError

if TYPE_CHECKING:
    from mypy_boto3_cloudfront import CloudFrontClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_sts import STSClient

ENV_SKIP_ROLE_CHECK = "JES_SKIP_ROLE_CHECK"

_ASSUMED_ROLE = re.compile(r"^arn:aws:sts::\d{12}:assumed-role/([^/]+)/")


def caller_identity(sts: STSClient) -> tuple[str, str]:
    """(account id, ARN) of the current credentials."""
    try:
        identity = sts.get_caller_identity()
    except ClientError as exc:
        raise PreflightError(f"no usable AWS credentials: {exc}") from exc
    return identity["Account"], identity["Arn"]


def role_name_from_arn(arn: str) -> str | None:
    """The role name of an assumed-role session ARN, else None."""
    match = _ASSUMED_ROLE.match(arn)
    return match.group(1) if match else None


def check_role(
    sts: STSClient,
    expected_role: str,
    logger: Logger,
    environ: dict[str, str] | None = None,
) -> str:
    """Fail unless the credentials are an assumed-role session of `expected_role`.

    `JES_SKIP_ROLE_CHECK=1` disables the check for local runs with admin
    credentials; it is logged as a warning so it never passes unnoticed.
    Returns the caller ARN.
    """
    env = os.environ if environ is None else environ
    _, arn = caller_identity(sts)
    actual = role_name_from_arn(arn)
    if env.get(ENV_SKIP_ROLE_CHECK) == "1":
        logger.warning(
            "%s=1: not checking that %s is the %s role", ENV_SKIP_ROLE_CHECK, arn, expected_role
        )
        return arn
    if actual != expected_role:
        raise PreflightError(
            f"this command must run as the {expected_role} role; the credentials are {arn}"
            + ("" if actual else " (not an assumed role)")
        )
    logger.info("running as %s", arn)
    return arn


def check_bucket(s3: S3Client, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchBucket"):
            raise PreflightError(f"bucket {bucket} does not exist") from exc
        if code in ("403", "AccessDenied"):
            raise PreflightError(
                f"bucket {bucket} exists but these credentials may not reach it"
            ) from exc
        raise PreflightError(f"cannot check bucket {bucket}: {exc}") from exc


def invalidate_distribution(
    cloudfront: CloudFrontClient, distribution_id: str, reference: str
) -> str:
    """Invalidate everything on a distribution; returns the invalidation ID."""
    try:
        response: Any = cloudfront.create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": ["/*"]},
                "CallerReference": reference,
            },
        )
    except ClientError as exc:
        raise RunError(f"CloudFront invalidation of {distribution_id} failed: {exc}") from exc
    return str(response["Invalidation"]["Id"])
