"""Components of a bot project: the container image, the Lichess bot, the task runner and datasets.

A project file with `components` (instead of `domain` and `site`) describes a
container image built from the repository's GitHub releases and a Lichess bot
that runs it on one EC2 instance. Names, roles and paths are derived in
`project.py`; this module only validates what the file says.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PARAMETER = re.compile(r"^/[a-z0-9-]+(/[a-z0-9-]+)+$")

RetentionDays = Literal[
    1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731,
    1096, 1827, 2192, 2557, 2922, 3288, 3653,
]  # fmt: skip


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in ("", "."):
        raise ValueError("must be a relative path inside the repository")
    return value


class ArtifactSection(_Model):
    """The release assets the image ships: a tarball, its checksum, the binary inside."""

    asset: str
    checksum: str
    binary: str

    @field_validator("asset", "checksum")
    @classmethod
    def _versioned(cls, value: str) -> str:
        if "{version}" not in value:
            raise ValueError("must contain {version}, replaced by the release tag")
        if "/" in value:
            raise ValueError("must be a release asset file name, not a path")
        return value

    @field_validator("binary")
    @classmethod
    def _one_component(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value) or value in (".", ".."):
            raise ValueError("binary must be a single file name inside the tarball's directory")
        return value

    def asset_name(self, version: str) -> str:
        return self.asset.replace("{version}", version)

    def checksum_name(self, version: str) -> str:
        return self.checksum.replace("{version}", version)


class ContainerImage(_Model):
    kind: Literal["container-image"]
    architecture: Literal["x86_64", "arm64"] = "x86_64"
    artifact: ArtifactSection
    dockerfile: str | None = None

    @field_validator("dockerfile")
    @classmethod
    def _dockerfile(cls, value: str | None) -> str | None:
        return None if value is None else _relative_path(value)


class EngineSection(_Model):
    hash_mb: int = Field(default=128, ge=1, le=4096)
    move_overhead_ms: int = Field(default=500, ge=0, le=5000)


class RecordsSection(_Model):
    s3: bool = True


class LogsSection(_Model):
    retention_days: RetentionDays = 30


class LichessBot(_Model):
    kind: Literal["lichess-bot"]
    track: Literal["latest-release"] = "latest-release"
    instance_type: str = Field(default="t3.micro", pattern=r"^[a-z0-9]+\.[a-z0-9]+$")
    config: str
    lichess_user: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{2,30}$")
    secrets: dict[str, str]
    engine: EngineSection = EngineSection()
    records: RecordsSection = RecordsSection()
    logs: LogsSection = LogsSection()

    @field_validator("config")
    @classmethod
    def _config(cls, value: str) -> str:
        _relative_path(value)
        if not value.endswith((".yml", ".yaml")):
            raise ValueError("config must be a .yml or .yaml file")
        return value

    @field_validator("secrets")
    @classmethod
    def _secrets(cls, value: dict[str, str]) -> dict[str, str]:
        if "LICHESS_TOKEN" not in value:
            raise ValueError("secrets must name the LICHESS_TOKEN parameter")
        for name, parameter in value.items():
            if not _ENV_NAME.match(name):
                raise ValueError(f"{name!r} is not an environment variable name")
            if not _PARAMETER.match(parameter):
                raise ValueError(f"{parameter!r} is not an SSM parameter path like /a/b/c")
        return value


_INSTANCE_TYPE = re.compile(r"^[a-z0-9]+\.[a-z0-9]+$")


class TaskRunner(_Model):
    """Ephemeral EC2 instances, spot by default, that run one CI task each (`task run`)."""

    kind: Literal["task-runner"]
    #: The launch role's allow-list. Never burstable, never SMT-on: `task run`
    #: refuses a type with more than one thread per core.
    instance_types: tuple[str, ...] = ("c8a.xlarge", "c8a.8xlarge", "c8a.16xlarge")
    default_instance_type: str = "c8a.16xlarge"
    market: Literal["spot", "on-demand"] = "spot"
    #: The spot price cap, as a fraction of the on-demand price (resolved and
    #: logged at launch). Spot has no bidding: you pay the current price; the
    #: cap only decides when AWS stops the instance for price.
    max_price_fraction: float = Field(default=0.58, gt=0, le=1)
    #: On demand at launch only, after spot fails in every zone for
    #: spot_wait_minutes. Never mid-task: an interrupted task is reported.
    on_demand_fallback: bool = True
    spot_wait_minutes: int = Field(default=5, ge=1, le=30)
    #: At most 6: GitHub-hosted jobs stop at six hours, and the launch role's
    #: sessions last seven (the task plus an hour for launch and setup).
    max_hours: int = Field(default=6, ge=1, le=6)
    #: Running task vCPUs per project (the account's standard spot and
    #: on-demand quotas are 256 each); a launch over it waits for a slot.
    max_vcpus: int = Field(default=192, ge=1, le=256)
    slot_wait_minutes: int = Field(default=30, ge=0, le=120)
    results_days: int = Field(default=7, ge=1, le=30)
    log_retention_days: RetentionDays = 14

    @field_validator("instance_types")
    @classmethod
    def _instance_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("must list at least one instance type")
        for name in value:
            if not _INSTANCE_TYPE.match(name):
                raise ValueError(f"{name!r} is not an EC2 instance type like c8a.xlarge")
            if name.startswith("t"):
                # Burstable types throttle under sustained load: benchmark noise.
                raise ValueError(f"{name!r} is burstable; tasks need dedicated cores")
        return value

    @model_validator(mode="after")
    def _default_is_allowed(self) -> TaskRunner:
        if self.default_instance_type not in self.instance_types:
            chosen = self.default_instance_type
            raise ValueError(f"default_instance_type {chosen!r} must be one of instance_types")
        return self


_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_PREFIX = re.compile(r"^([a-z0-9][a-z0-9._=-]*/)+$")
_ROLE_ARN = re.compile(r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$")
#: Names whose buckets the project already has: <Organization>-<project>-<name>.
_RESERVED_DATASET_NAMES = ("records", "tasks")


class Dataset(_Model):
    """A kept-forever, append-only S3 bucket, `<Organization>-<project>-<name>`."""

    kind: Literal["dataset"]
    name: str
    #: The derived bucket name, written out so workflows can read it without
    #: the tool; checked against the derivation in `project.py`.
    bucket: str
    #: Everything the project writes lives under it; writers and readers get
    #: this prefix only.
    prefix: str
    #: Components whose roles may add objects under the prefix (never read or
    #: delete them): `task-runner`, the task instances.
    writers: tuple[Literal["task-runner"], ...] = ()
    #: IAM role ARNs that may read under the prefix. Administrators can
    #: always read.
    readers: tuple[str, ...] = ()
    #: Days before objects move to Intelligent-Tiering; 0 moves them at once.
    transition_days: int = Field(default=0, ge=0, le=3650)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _KEBAB.match(value):
            raise ValueError("name must be kebab-case, e.g. nnue")
        if value in _RESERVED_DATASET_NAMES:
            raise ValueError(f"name {value!r} is reserved: the project already has that bucket")
        return value

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str) -> str:
        if not _PREFIX.match(value):
            raise ValueError(
                "prefix must be path segments of lowercase letters, digits, '.', '_', '=' "
                "and '-', ending in '/' and without a leading '/', e.g. data/"
            )
        return value

    @field_validator("readers")
    @classmethod
    def _readers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for arn in value:
            if not _ROLE_ARN.match(arn):
                raise ValueError(f"readers: {arn!r} is not an IAM role ARN")
        return value


Component = Annotated[
    ContainerImage | LichessBot | TaskRunner | Dataset, Field(discriminator="kind")
]
