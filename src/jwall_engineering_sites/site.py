"""The site file, jwall-engineering-sites.yaml, and everything derived from it.

The file holds only what differs between sites. Bucket, roles, state key and
URLs are derived here, which is what guarantees they follow the convention.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .errors import ConfigError
from .settings import Settings

SITE_FILE = Path("jwall-engineering-sites.yaml")

#: The tags every resource carries. The site file must state all of them.
REQUIRED_TAGS = ("Organization", "Project", "Environment", "Repository", "ManagedBy", "Owner")

ENVIRONMENT = "prod"
PROD_PREFIX = "prod/"
DEV_PREFIX = "dev/"
PREVIEWS_PREFIX = "previews/"

_FQDN = re.compile(r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SiteSection(_Model):
    output_dir: str
    immutable_paths: tuple[str, ...] = ()

    @field_validator("output_dir")
    @classmethod
    def _relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value in ("", "."):
            raise ValueError("must be a relative directory inside the repository")
        return value

    @field_validator("immutable_paths")
    @classmethod
    def _globs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in value:
            if pattern.startswith("/") or ".." in pattern.split("/"):
                raise ValueError(f"{pattern!r} must be a glob relative to output_dir")
        return value


class PreviewSection(_Model):
    enabled: bool = True


class DevSection(_Model):
    domain: str = Field(pattern=_FQDN.pattern)


class SiteConfig(_Model):
    """jwall-engineering-sites.yaml, validated on its own.

    Rules that need config.yaml (the DNS zone, the organisation) live in
    `Site.from_config`, so that this model can be checked without it.
    """

    version: Literal[1]
    project: str = Field(pattern=_KEBAB.pattern, max_length=30)
    repository: str = Field(pattern=_REPOSITORY.pattern)
    domain: str = Field(pattern=_FQDN.pattern)
    tags: dict[str, str]
    site: SiteSection
    preview: PreviewSection = PreviewSection()
    dev: DevSection | None = None

    @model_validator(mode="after")
    def _required_tags(self) -> SiteConfig:
        missing = [tag for tag in REQUIRED_TAGS if tag not in self.tags]
        if missing:
            raise ValueError(
                f"tags is missing the required keys {', '.join(missing)}; every site must "
                f"state all of {', '.join(REQUIRED_TAGS)} explicitly"
            )
        expected = {
            "Project": self.project,
            "Environment": ENVIRONMENT,
            "Repository": self.repository,
            "ManagedBy": "terraform",
        }
        mismatched = [
            f"{key} must be {value!r}, got {self.tags[key]!r}"
            for key, value in expected.items()
            if self.tags[key] != value
        ]
        if mismatched:
            raise ValueError("tags: " + "; ".join(mismatched))
        empty = [key for key, value in self.tags.items() if not value.strip()]
        if empty:
            raise ValueError(f"tags: empty values for {', '.join(empty)}")
        return self

    @model_validator(mode="after")
    def _dev_differs(self) -> SiteConfig:
        if self.dev is not None and self.dev.domain == self.domain:
            raise ValueError("dev.domain must differ from domain")
        return self


def load_site_config(path: Path = SITE_FILE) -> SiteConfig:
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"site file not found at {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    try:
        return SiteConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {_format_validation_error(exc)}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"{location}: {message}")
    return "; ".join(lines)


class Site(_Model):
    """A validated site plus everything derived from it and config.yaml."""

    config: SiteConfig
    organization: str
    zone_id: str
    state_bucket: str
    state_key: str
    region: str

    @classmethod
    def from_config(cls, config: SiteConfig, settings: Settings) -> Site:
        problems = []
        if config.tags["Organization"] != settings.context.organization:
            problems.append(
                f"tags.Organization must be {settings.context.organization!r}, "
                f"got {config.tags['Organization']!r}"
            )
        zone_suffix = "." + settings.dns.zone
        for label, domain in (
            ("domain", config.domain),
            ("dev.domain", config.dev and config.dev.domain),
        ):
            if domain and domain != settings.dns.zone and not domain.endswith(zone_suffix):
                problems.append(f"{label} {domain!r} is not under the zone {settings.dns.zone!r}")
        if problems:
            raise ConfigError("; ".join(problems))
        return cls(
            config=config,
            organization=settings.context.organization,
            zone_id=settings.dns.zone_id,
            state_bucket=settings.state.bucket,
            state_key=settings.state_key(config.project, ENVIRONMENT),
            region=settings.state.region,
        )

    @property
    def project(self) -> str:
        return self.config.project

    @property
    def bucket(self) -> str:
        return f"{self.organization}-{self.project}"

    def role_name(self, role: Literal["plan", "preview", "apply"]) -> str:
        return f"{self.organization}-{self.project}-ci-{role}"

    @property
    def prod_url(self) -> str:
        return f"https://{self.config.domain}/"

    @property
    def dev_url(self) -> str | None:
        return f"https://{self.config.dev.domain}/" if self.config.dev else None

    @staticmethod
    def preview_prefix(pr: int) -> str:
        return f"{PREVIEWS_PREFIX}pr-{pr}/"

    def preview_url(self, pr: int) -> str:
        return f"https://pr-{pr}.preview.{self.config.domain}/"
