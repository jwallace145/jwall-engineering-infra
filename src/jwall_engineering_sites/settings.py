"""The tool configuration: config.yaml, loaded once and passed to every command.

It describes how the tool runs (Terraform version, library pin, state bucket,
organisation constants, ECR, sync headers), as opposed to what one site is,
which lives in each site repository's jwall-engineering-sites.yaml.
"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .components import check_prefixes
from .errors import ConfigError

ENV_CONFIG_PATH = "JES_CONFIG"
IMAGE_CONFIG_PATH = Path("/etc/jwall-engineering-sites/config.yaml")
LOCAL_CONFIG_PATH = Path("config.yaml")

_DURATION = re.compile(r"^(\d+)([smh])$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(value: str) -> int:
    """Seconds for a Terraform-style duration such as `120s`, `15m` or `1h`."""
    match = _DURATION.match(value)
    if not match:
        raise ValueError(f"{value!r} is not a duration like 120s, 15m or 1h")
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolSection(_Section):
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["console", "json"] = "console"


class TerraformSection(_Section):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    lock_timeout: str = "120s"
    plan_timeout: str = "15m"

    @field_validator("lock_timeout", "plan_timeout")
    @classmethod
    def _duration(cls, value: str) -> str:
        parse_duration(value)
        return value

    @property
    def plan_timeout_seconds(self) -> int:
        return parse_duration(self.plan_timeout)


class LibrarySection(_Section):
    source: str = Field(pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[a-z0-9]+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class StateSection(_Section):
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    region: str = Field(pattern=r"^[a-z]{2}-[a-z]+-\d$")
    key_template: str

    @field_validator("key_template")
    @classmethod
    def _placeholders(cls, value: str) -> str:
        for placeholder in ("{project}", "{environment}"):
            if placeholder not in value:
                raise ValueError(f"key_template must contain {placeholder}")
        if value.startswith("/") or "*" in value or "?" in value:
            raise ValueError("key_template must be a relative key without IAM wildcards")
        return value


class AwsSection(_Section):
    account_id: str = Field(pattern=r"^\d{12}$")


class ContextSection(_Section):
    organization: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    owner: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")


class DnsSection(_Section):
    zone: str = Field(pattern=r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
    zone_id: str = Field(pattern=r"^Z[A-Z0-9]{5,31}$")


class EcrSection(_Section):
    repository: str = Field(pattern=r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
    keep_release_images: int = Field(ge=1)
    expire_untagged_after_days: int = Field(ge=1)


class SyncSection(_Section):
    cache_control: str
    preview_cache_control: str
    html_cache_control: str
    immutable_cache_control: str


class BotSection(_Section):
    """How bot projects run: the health check and the records layout prefix."""

    health_log_pattern: str
    health_seconds: int = Field(ge=10, le=600)
    records_prefix: str = Field(pattern=r"^([a-z0-9][a-z0-9-]*/)+$")
    #: Zones of the bot VPC's public subnets, in order (the bot uses the first).
    #: Spot tasks try each, so list every zone that offers their type; append
    #: only, since subnets are numbered by position.
    availability_zones: tuple[str, ...] = Field(min_length=1, max_length=6)
    #: How long a build waits for a release's assets to be attached: release
    #: workflows publish first and upload their build outputs afterwards.
    asset_wait: str = "30m"
    #: The pinned Amazon Linux 2023 image per architecture, by its Amazon-owned image
    #: name (e.g. al2023-ami-2023.9.20251027.0-kernel-6.1-x86_64). A bot's instance
    #: changes OS only when its pin changes; an architecture without a pin follows
    #: the latest image, which replaces the instance on every Amazon release.
    amis: dict[Literal["x86_64", "arm64"], str] = {}

    @field_validator("asset_wait")
    @classmethod
    def _asset_wait(cls, value: str) -> str:
        parse_duration(value)
        return value

    @property
    def asset_wait_seconds(self) -> int:
        return parse_duration(self.asset_wait)

    @field_validator("amis")
    @classmethod
    def _ami_names(cls, value: dict[str, str]) -> dict[str, str]:
        for architecture, name in value.items():
            if name.startswith("ami-") or not re.fullmatch(r"[A-Za-z0-9._()/-]{3,128}", name):
                raise ValueError(
                    f"{architecture}: {name!r} must be an Amazon image name like "
                    f"al2023-ami-2023.9.20251027.0-kernel-6.1-{architecture}, not an ID"
                )
            if not name.endswith(f"-{architecture}"):
                raise ValueError(f"{architecture}: {name!r} must end in -{architecture}")
        return value


class TaskLimits(_Section):
    """What any project file may ask for. `project.py` checks files against these."""

    #: Longest a task may run. GitHub stops a job at six hours.
    max_hours: int = Field(ge=1)
    max_vcpus: int = Field(ge=1)
    root_volume_gb: int = Field(ge=1)


class DatasetRetention(_Section):
    """The centrally approved retention of one dataset bucket.

    A consumer's dataset component must request exactly these values.
    """

    #: The dataset's `prefix` that `write_once` and `expire_prefixes` are
    #: relative to. Required when any retention is set; the file's own prefix
    #: must then equal it. An empty entry imposes no prefix.
    prefix: str | None = None
    write_once: tuple[str, ...] = ()
    expire_prefixes: dict[str, int] = {}
    expire_noncurrent_days: int | None = None
    versioning: bool = False

    @field_validator("prefix")
    @classmethod
    def _prefix(cls, value: str | None) -> str | None:
        if value is not None:
            check_prefixes((value,))
        return value

    @field_validator("write_once")
    @classmethod
    def _write_once(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return check_prefixes(value)

    @field_validator("expire_prefixes")
    @classmethod
    def _expire_prefixes(cls, value: dict[str, int]) -> dict[str, int]:
        check_prefixes(tuple(value))
        return value

    @model_validator(mode="after")
    def _retention_needs_a_prefix(self) -> DatasetRetention:
        relative = self.write_once or self.expire_prefixes
        if (relative or self.expire_noncurrent_days is not None) and self.prefix is None:
            raise ValueError(
                "prefix is required when write_once, expire_prefixes or "
                "expire_noncurrent_days is set: the dataset prefix they apply to"
            )
        return self


#: The default CIDR of the bot VPC; a Databricks project's `vpc_cidr` may not overlap it.
BOT_VPC_CIDR = "10.0.0.0/16"
#: The longest project name with a `databricks` entry: IAM role names are limited
#: to 64 characters and `<name_root>-<project>-databricks-cross-account` must fit.
DATABRICKS_PROJECT_NAME_MAX = 21
PLACEHOLDER_UUID = "00000000-0000-0000-0000-000000000000"

_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _check_emails(value: tuple[str, ...]) -> tuple[str, ...]:
    for email in value:
        if not _EMAIL.match(email):
            raise ValueError(f"{email!r} is not an email address")
    return value


class DatabricksPolicy(_Section):
    """One cluster policy, centrally reviewed; a project lists the names it gets."""

    description: str
    #: The Databricks policy definition: attribute path to its rule.
    definition: dict[str, Any]


class DatabricksProject(_Section):
    """What one project's Databricks workspace is approved for."""

    compute_policies: tuple[str, ...]
    cost_center: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    #: Allocated centrally so IP capacity is owned here and overlaps are checkable.
    vpc_cidr: str
    #: Workspace members, by email.
    admins: tuple[str, ...] = Field(min_length=1)
    users: tuple[str, ...] = ()

    @field_validator("vpc_cidr")
    @classmethod
    def _vpc_cidr(cls, value: str) -> str:
        try:
            network = ipaddress.IPv4Network(value, strict=True)
        except ValueError as exc:
            raise ValueError(f"vpc_cidr {value!r} is not an IPv4 network: {exc}") from exc
        if not 16 <= network.prefixlen <= 20:
            raise ValueError(f"vpc_cidr {value!r} must be between /16 and /20")
        return value

    @field_validator("admins", "users")
    @classmethod
    def _emails(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _check_emails(value)


class DatabricksSection(_Section):
    """The Databricks account the workspace projects live in (the `databricks:` section)."""

    #: False until the owner has created the service principal and set its secret;
    #: the foundation's Databricks resources and the live checks wait for true.
    enabled: bool
    account_id: str = Field(pattern=_UUID)
    client_id: str = Field(pattern=_UUID)
    secret_parameter: str = "/jwall-engineering/databricks/client-secret"
    metastore_name: str = "jwall-engineering-us-east-1"
    metastore_admins: tuple[str, ...] = Field(min_length=1)
    #: The project that owns the metastore; must have a `databricks` guardrail entry.
    governance_project: str | None = None
    #: The pinned Amazon Linux 2023 arm64 image name of the NAT instance.
    nat_ami: str
    #: The deployment-name prefix Databricks configured for this account, if any. With
    #: one, a workspace's URL is `<prefix>-<project>`; without, Databricks picks the URL.
    deployment_name_prefix: str | None = Field(default=None, pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    audit_bucket: str = "jwall-engineering-databricks-audit-logs"
    events_prefix: str = Field(default="events/", pattern=r"^([a-z0-9][a-z0-9-]*/)+$")
    compute_policies: dict[str, DatabricksPolicy] = {}
    readiness_timeout: str = "20m"

    @field_validator("metastore_admins")
    @classmethod
    def _emails(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _check_emails(value)

    @field_validator("nat_ami")
    @classmethod
    def _nat_ami(cls, value: str) -> str:
        if value.startswith("ami-") or not re.fullmatch(r"[A-Za-z0-9._()/-]{3,128}", value):
            raise ValueError(f"{value!r} must be an Amazon image name, not an ID")
        if not value.endswith("-arm64"):
            raise ValueError(f"{value!r} must be an arm64 image (end in -arm64)")
        return value

    @field_validator("compute_policies")
    @classmethod
    def _policy_names(cls, value: dict[str, DatabricksPolicy]) -> dict[str, DatabricksPolicy]:
        for name in value:
            if not _KEBAB.match(name):
                raise ValueError(f"compute_policies: name {name!r} must be kebab-case")
        return value

    @field_validator("readiness_timeout")
    @classmethod
    def _readiness_timeout(cls, value: str) -> str:
        parse_duration(value)
        return value

    @property
    def readiness_timeout_seconds(self) -> int:
        return parse_duration(self.readiness_timeout)

    @property
    def has_placeholder_ids(self) -> bool:
        """True while `account_id` or `client_id` is still the nil UUID."""
        return PLACEHOLDER_UUID in (self.account_id.lower(), self.client_id.lower())


class ProjectGuardrails(_Section):
    """What one project's task launch role and buckets are approved for.

    Reviewed in this repository; the project's own file cannot widen any of it.
    """

    instance_types: tuple[str, ...] = ()
    task_profiles: tuple[str, ...] = ()
    datasets: dict[str, DatasetRetention] = {}
    #: Per-project override of `tasks.max_run_cost_usd`.
    max_run_cost_usd: float | None = None
    #: Present for a Databricks workspace project (needs the top-level `databricks`).
    databricks: DatabricksProject | None = None


class TasksSection(_Section):
    """How `task run` sets up and follows task instances (component `task-runner`)."""

    #: The Rust toolchain every task instance installs with rustup. Pin a
    #: version: changing it is a new launch template version, not a rebuild.
    rust_toolchain: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]*$")
    #: Seconds between polls of a task's output and status.
    poll_seconds: int = Field(default=5, ge=1, le=60)
    #: Pinned images for task profiles, by key (a profile's `image`): the
    #: Amazon-owned image *name*, resolved to its ID by the bot stack. A
    #: profile's image changes only when its pin changes.
    images: dict[str, str] = {}
    #: What any project file may ask for.
    limits: TaskLimits
    #: Worst-case cost of one task; above it, the launch is refused.
    max_run_cost_usd: float = Field(gt=0)

    @field_validator("images")
    @classmethod
    def _image_names(cls, value: dict[str, str]) -> dict[str, str]:
        for key, name in value.items():
            if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", key):
                raise ValueError(f"images: key {key!r} must be kebab-case")
            if name.startswith("ami-") or not re.fullmatch(r"[A-Za-z0-9._()/ -]{3,128}", name):
                raise ValueError(f"images.{key}: {name!r} must be an Amazon image name, not an ID")
        return value


class PlanSection(_Section):
    comment_max_bytes: int = Field(ge=1024, le=65000)


class Settings(_Section):
    """The whole of config.yaml."""

    tool: ToolSection = ToolSection()
    terraform: TerraformSection
    library: LibrarySection
    state: StateSection
    aws: AwsSection
    context: ContextSection
    dns: DnsSection
    ecr: EcrSection
    sync: SyncSection
    plan: PlanSection
    bot: BotSection
    tasks: TasksSection
    projects: dict[str, ProjectGuardrails] = {}
    databricks: DatabricksSection | None = None

    @model_validator(mode="after")
    def _databricks_consistency(self) -> Settings:
        projects = {n: g.databricks for n, g in self.projects.items() if g.databricks}
        if self.databricks is None:
            if projects:
                raise ValueError(
                    f"projects {', '.join(sorted(projects))} have a databricks entry "
                    "but config.yaml has no databricks section"
                )
            return self
        governance = self.databricks.governance_project
        if governance is not None and governance not in projects:
            raise ValueError(
                f"databricks.governance_project {governance!r} must name a project "
                "with a databricks guardrail entry"
            )
        central = self.databricks.compute_policies
        taken: list[tuple[str, ipaddress.IPv4Network]] = [
            ("the bot VPC", ipaddress.IPv4Network(BOT_VPC_CIDR))
        ]
        for name, project in sorted(projects.items()):
            if len(name) > DATABRICKS_PROJECT_NAME_MAX:
                raise ValueError(
                    f"project {name!r} has a databricks entry, so its name may be at most "
                    f"{DATABRICKS_PROJECT_NAME_MAX} characters (IAM role name limit)"
                )
            for policy in project.compute_policies:
                if policy not in central:
                    raise ValueError(
                        f"projects.{name}.databricks: compute policy {policy!r} is not "
                        "defined under databricks.compute_policies"
                    )
            network = ipaddress.IPv4Network(project.vpc_cidr)
            for other, other_network in taken:
                if network.overlaps(other_network):
                    raise ValueError(
                        f"projects.{name}.databricks.vpc_cidr {project.vpc_cidr} overlaps "
                        f"{other} ({other_network})"
                    )
            taken.append((f"project {name!r}", network))
        return self

    def run_cost_ceiling(self, project: str) -> float:
        """The project's `max_run_cost_usd` override, else `tasks.max_run_cost_usd`."""
        guardrails = self.projects.get(project)
        if guardrails is not None and guardrails.max_run_cost_usd is not None:
            return guardrails.max_run_cost_usd
        return self.tasks.max_run_cost_usd

    def state_key(self, project: str, environment: str, stack: str | None = None) -> str:
        """The state key of a project's environment, or of one of its stacks.

        A Databricks project has several stacks (`account`, `workspace`); each gets
        `<project>-<environment>-<stack>` as its file name. Without `stack` the key
        is unchanged.
        """
        key = self.state.key_template.format(project=project, environment=environment)
        if stack is None:
            return key
        if not key.endswith(".tfstate"):
            raise ValueError("key_template must end in .tfstate to add a stack suffix")
        return f"{key.removesuffix('.tfstate')}-{stack}.tfstate"


def default_config_path() -> Path:
    """JES_CONFIG, else the path baked into the image, else ./config.yaml."""
    if ENV_CONFIG_PATH in os.environ:
        return Path(os.environ[ENV_CONFIG_PATH])
    if IMAGE_CONFIG_PATH.is_file():
        return IMAGE_CONFIG_PATH
    return LOCAL_CONFIG_PATH


def load_settings(path: Path | None = None, environ: dict[str, str] | None = None) -> Settings:
    """Load config.yaml and apply the JES_* overrides.

    Only `JES_LOG_LEVEL` and `JES_LOG_FORMAT` may be overridden: they are the
    knobs a person debugging locally needs, and nothing else about how the
    tool runs should vary between runs of the same image.
    """
    env = os.environ if environ is None else environ
    path = path or default_config_path()
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"tool configuration not found at {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")

    tool = dict(raw.get("tool") or {})
    if "JES_LOG_LEVEL" in env:
        tool["log_level"] = env["JES_LOG_LEVEL"]
    if "JES_LOG_FORMAT" in env:
        tool["log_format"] = env["JES_LOG_FORMAT"]
    raw["tool"] = tool

    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {_format_validation_error(exc)}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"{location}: {error['msg']}")
    return "; ".join(lines)
