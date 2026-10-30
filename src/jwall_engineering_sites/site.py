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

from .components import Component, ContainerImage, Dataset, LichessBot, TaskRunner
from .errors import ConfigError
from .settings import Settings

SITE_FILE = Path("jwall-engineering-sites.yaml")

#: The tags the site file must state. Every resource also carries ManagedBy.
REQUIRED_TAGS = ("Organization", "Project", "Environment", "Repository", "Owner")

#: The ManagedBy tag value. The tool owns it: a site file may leave it out, and
#: the value project files stated before the tool owned it is still accepted.
MANAGED_BY = "jwall-engineering-infra"
LEGACY_MANAGED_BY = "terraform"

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

    A file describes either a site (`domain` and `site`) or a bot project
    (`components`: one container image and one Lichess bot). Rules that need
    config.yaml (the DNS zone, the organisation) live in `Site.from_config`
    and `Project.from_config`, so that this model can be checked without it.
    """

    version: Literal[1]
    project: str = Field(pattern=_KEBAB.pattern, max_length=30)
    repository: str = Field(pattern=_REPOSITORY.pattern)
    domain: str | None = Field(default=None, pattern=_FQDN.pattern)
    tags: dict[str, str]
    site: SiteSection | None = None
    preview: PreviewSection = PreviewSection()
    dev: DevSection | None = None
    components: tuple[Component, ...] = ()

    @field_validator("tags")
    @classmethod
    def _managed_by(cls, value: dict[str, str]) -> dict[str, str]:
        stated = value.get("ManagedBy", MANAGED_BY)
        if stated not in (MANAGED_BY, LEGACY_MANAGED_BY):
            raise ValueError(f"ManagedBy must be {MANAGED_BY!r} or left out, got {stated!r}")
        return {**value, "ManagedBy": MANAGED_BY}

    @model_validator(mode="after")
    def _one_kind(self) -> SiteConfig:
        is_site = self.domain is not None and self.site is not None and not self.components
        is_bot = (
            bool(self.components)
            and self.domain is None
            and self.site is None
            and self.dev is None
            and "preview" not in self.model_fields_set
        )
        if not (is_site or is_bot):
            raise ValueError(
                "a file describes either a site (domain and site, no components) or a bot "
                "project (components, without domain, site, dev or preview)"
            )
        if is_bot:
            for kind in ("container-image", "lichess-bot"):
                count = sum(1 for c in self.components if c.kind == kind)
                if count != 1:
                    raise ValueError(f"components must hold exactly one {kind}, got {count}")
            runners = sum(1 for c in self.components if c.kind == "task-runner")
            if runners > 1:
                raise ValueError(f"components may hold at most one task-runner, got {runners}")
            names = [c.name for c in self.datasets]
            if len(names) != len(set(names)):
                raise ValueError(f"dataset names must be unique, got {names}")
            if runners == 0 and any("task-runner" in c.writers for c in self.datasets):
                raise ValueError(
                    "a dataset with writers: [task-runner] needs a task-runner component"
                )
            runner = self.task_runner
            known = {d.name for d in self.datasets}
            for name, profile in (runner.profiles if runner else {}).items():
                unknown = sorted(set(profile.datasets) - known)
                if unknown:
                    raise ValueError(
                        f"task-runner profile {name}: datasets {', '.join(unknown)} are not "
                        "dataset components of this file"
                    )
        return self

    @property
    def kind(self) -> Literal["site", "bot"]:
        return "bot" if self.components else "site"

    @property
    def image(self) -> ContainerImage:
        return next(c for c in self.components if isinstance(c, ContainerImage))

    @property
    def bot(self) -> LichessBot:
        return next(c for c in self.components if isinstance(c, LichessBot))

    @property
    def task_runner(self) -> TaskRunner | None:
        return next((c for c in self.components if isinstance(c, TaskRunner)), None)

    @property
    def datasets(self) -> tuple[Dataset, ...]:
        return tuple(c for c in self.components if isinstance(c, Dataset))

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
        if config.kind != "site":
            raise ConfigError(
                f"{config.project} is a bot project, not a site; this command works on sites only"
            )
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
    def domain(self) -> str:
        assert self.config.domain is not None  # from_config admits sites only
        return self.config.domain

    @property
    def site_section(self) -> SiteSection:
        assert self.config.site is not None  # from_config admits sites only
        return self.config.site

    @property
    def prod_url(self) -> str:
        return f"https://{self.domain}/"

    @property
    def dev_url(self) -> str | None:
        return f"https://{self.config.dev.domain}/" if self.config.dev else None

    @staticmethod
    def preview_prefix(pr: int) -> str:
        return f"{PREVIEWS_PREFIX}pr-{pr}/"

    def preview_url(self, pr: int) -> str:
        return f"https://pr-{pr}.preview.{self.domain}/"
