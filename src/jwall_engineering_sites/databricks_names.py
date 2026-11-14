"""The names the released Databricks modules derive from a project.

One place, shared by the config checks (length limits) and the live checks
(is the bucket free?). Keep in step with the modules' naming.
"""

from __future__ import annotations

from dataclasses import dataclass

from .project import Project

#: The two zones the account stack's subnets use.
ZONES = ("us-east-1a", "us-east-1b")
REGION = "us-east-1"
S3_BUCKET_MAX = 63
IAM_ROLE_MAX = 64


@dataclass(frozen=True)
class DerivedNames:
    root_bucket: str
    catalog_bucket: str
    cross_account_role: str
    uc_role: str

    @property
    def buckets(self) -> dict[str, str]:
        return {"root bucket": self.root_bucket, "catalog bucket": self.catalog_bucket}

    @property
    def roles(self) -> dict[str, str]:
        return {"cross-account role": self.cross_account_role, "Unity Catalog role": self.uc_role}


def derived_names(project: Project) -> DerivedNames:
    base = f"{project.organization}-{project.project}-databricks"
    return DerivedNames(
        root_bucket=f"{base}-root",
        catalog_bucket=f"{base}-catalog",
        cross_account_role=f"{base}-cross-account",
        uc_role=f"{base}-uc",
    )
