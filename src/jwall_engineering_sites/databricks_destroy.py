"""What `destroy` deletes from a Databricks project, and emptying its catalog bucket.

Destroy order (ruling R7): empty the catalog bucket while its bucket policy still
grants the apply role list and delete, then destroy the workspace stack (which
removes the empty bucket and the catalog), then the account stack. Every step is
idempotent, so a re-run continues where a failed one stopped.
"""

from __future__ import annotations

from logging import Logger
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .databricks_names import derived_names
from .errors import RunError
from .project import Project
from .site import ENVIRONMENT

#: The most keys one DeleteObjects request takes.
DELETE_BATCH = 1000

KEPT = "Kept: the metastore, the event log and the client secret in SSM."


def deletion_list(project: Project) -> list[str]:
    """Everything `destroy` deletes, for the confirmation before it acts."""
    names = derived_names(project)
    catalog = project.project.replace("-", "_")
    return [
        f"the workspace {project.project}-{ENVIRONMENT} and everything in it: clusters, "
        "notebooks, jobs, its compute policies and settings",
        f"the catalog {catalog} with its schemas, tables and grants, its storage credential "
        "and external location",
        f"the catalog bucket s3://{names.catalog_bucket} and every object version in it",
        f"the root bucket s3://{names.root_bucket} and its contents",
        f"the VPC {project.vpc_name} with its subnets, endpoints and security group, "
        "and its NAT instance",
        f"the roles {names.cross_account_role} and {names.uc_role}, the workspace's account "
        "groups and its metastore assignment",
    ]


def render_deletion_list(project: Project) -> str:
    lines = [f"destroy {project.project} deletes:"]
    lines += [f"  - {item}" for item in deletion_list(project)]
    lines.append(KEPT)
    return "\n".join(lines)


def _delete_batch(s3: Any, bucket: str, batch: list[dict[str, str]]) -> int:
    response = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
    errors = response.get("Errors") or []
    if errors:
        first = errors[0]
        raise RunError(
            f"cannot delete {len(errors)} object version(s) from s3://{bucket}: "
            f"{first.get('Code', 'unknown')} on {first.get('Key', '?')}; re-run destroy, or "
            "check the bucket policy still grants the apply role s3:DeleteObjectVersion"
        )
    return len(batch)


def empty_bucket(s3: Any, bucket: str, logger: Logger) -> int | None:
    """Delete every object version and delete marker in `bucket`, in batches of 1000.

    Returns how many were deleted, or None when the bucket no longer exists (a
    previous destroy already removed it). `s3` is a boto3 S3 client.
    """
    deleted = 0
    batch: list[dict[str, str]] = []
    try:
        for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket):
            for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
                batch.append({"Key": item["Key"], "VersionId": item["VersionId"]})
                if len(batch) == DELETE_BATCH:
                    deleted += _delete_batch(s3, bucket, batch)
                    batch = []
        if batch:
            deleted += _delete_batch(s3, bucket, batch)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "unknown")
        if code == "NoSuchBucket":
            logger.info("s3://%s does not exist; nothing to empty", bucket)
            return None
        raise RunError(f"cannot empty s3://{bucket}: {code}") from None
    except BotoCoreError as exc:
        raise RunError(f"cannot empty s3://{bucket}: {type(exc).__name__}") from None
    logger.info("deleted %d object versions from s3://%s", deleted, bucket)
    return deleted
