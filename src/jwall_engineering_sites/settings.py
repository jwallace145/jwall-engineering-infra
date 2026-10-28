"""The tool configuration: config.yaml, loaded once and passed to every command.

It describes how the tool runs (Terraform version, library pin, state bucket,
organisation constants, ECR, sync headers), as opposed to what one site is,
which lives in each site repository's jwall-engineering-sites.yaml.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
    availability_zones: tuple[str, ...] = Field(min_length=1, max_length=4)


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

    def state_key(self, project: str, environment: str) -> str:
        return self.state.key_template.format(project=project, environment=environment)


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
