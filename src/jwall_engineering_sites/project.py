"""A project file of either kind, with everything derived from it.

Sites keep using `Site` (site.py) for their commands. `Project` is the view
the kind-aware commands (validate, plan, apply, destroy) and the bot
commands work with: it derives the bot's names, parameters and roles the
same way the foundation stack and the container-instance module do.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .components import CPU_PROFILE, ContainerImage, Dataset, LichessBot, TaskRunner
from .errors import ConfigError
from .settings import Settings
from .site import ENVIRONMENT, Site, SiteConfig

Role = Literal["plan", "preview", "apply", "release", "task"]


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config: SiteConfig
    organization: str
    state_bucket: str
    state_key: str
    region: str
    zone_id: str | None

    @classmethod
    def from_config(cls, config: SiteConfig, settings: Settings) -> Project:
        if config.kind == "site":
            site = Site.from_config(config, settings)
            return cls(
                config=config,
                organization=site.organization,
                state_bucket=site.state_bucket,
                state_key=site.state_key,
                region=site.region,
                zone_id=site.zone_id,
            )
        organization = settings.context.organization
        problems = []
        if config.tags["Organization"] != organization:
            problems.append(
                f"tags.Organization must be {organization!r}, got {config.tags['Organization']!r}"
            )
        own = f"/{organization}/{config.project}/"
        for name, parameter in config.bot.secrets.items():
            if not parameter.startswith(own):
                problems.append(f"secrets.{name} must start with {own}, got {parameter!r}")
            elif parameter.startswith(f"{own}bot/"):
                problems.append(
                    f"secrets.{name} must not live under {own}bot/, "
                    "which the release role can write"
                )
        for dataset in config.datasets:
            bucket = f"{organization}-{config.project}-{dataset.name}"
            if dataset.bucket != bucket:
                problems.append(
                    f"dataset {dataset.name}: bucket must be {bucket!r} "
                    f"(<Organization>-<project>-<name>), got {dataset.bucket!r}"
                )
            elif len(bucket) > 63:
                problems.append(f"dataset {dataset.name}: bucket {bucket!r} exceeds 63 characters")
        runner = config.task_runner
        for name, profile in (runner.profiles if runner else {}).items():
            if profile.image is not None and profile.image not in settings.tasks.images:
                problems.append(
                    f"task-runner profile {name}: image {profile.image!r} is not pinned in "
                    "config.yaml tasks.images"
                )
        if problems:
            raise ConfigError("; ".join(problems))
        return cls(
            config=config,
            organization=organization,
            state_bucket=settings.state.bucket,
            state_key=settings.state_key(config.project, ENVIRONMENT),
            region=settings.state.region,
            zone_id=None,
        )

    @property
    def project(self) -> str:
        return self.config.project

    @property
    def kind(self) -> Literal["site", "bot"]:
        return self.config.kind

    @property
    def image(self) -> ContainerImage:
        self._require("bot")
        return self.config.image

    @property
    def bot(self) -> LichessBot:
        self._require("bot")
        return self.config.bot

    @property
    def site_bucket(self) -> str:
        self._require("site")
        return f"{self.organization}-{self.project}"

    @property
    def image_repository(self) -> str:
        return f"{self.organization}-{self.project}"

    @property
    def bot_name(self) -> str:
        return f"{self.organization}-{self.project}-bot"

    @property
    def records_bucket(self) -> str:
        return f"{self.organization}-{self.project}-records"

    @property
    def vpc_name(self) -> str:
        return f"{self.organization}-{self.project}"

    @property
    def parameter_prefix(self) -> str:
        return f"/{self.organization}/{self.project}/bot"

    @property
    def log_group(self) -> str:
        return self.parameter_prefix

    @property
    def task_runner(self) -> TaskRunner | None:
        self._require("bot")
        return self.config.task_runner

    @property
    def task_name(self) -> str:
        """The task launch template, instance role, profile and security group."""
        return f"{self.organization}-{self.project}-task"

    @property
    def task_template(self) -> str:
        return self.task_name

    def profile_task_name(self, profile: str) -> str:
        """A profile's launch template, instance role and profile: `cpu` is
        `task_name`, every other `<task_name>-<profile>`."""
        return self.task_name if profile == CPU_PROFILE else f"{self.task_name}-{profile}"

    @property
    def task_log_group(self) -> str:
        return f"/{self.organization}/{self.project}/tasks"

    @property
    def task_bucket(self) -> str:
        return f"{self.organization}-{self.project}-tasks"

    @property
    def task_prefix(self) -> str:
        return "tasks/"

    @property
    def datasets(self) -> tuple[Dataset, ...]:
        self._require("bot")
        return self.config.datasets

    def dataset_bucket(self, name: str) -> str:
        return f"{self.organization}-{self.project}-{name}"

    def parameter(self, name: str) -> str:
        return f"{self.parameter_prefix}/{name}"

    def role_name(self, role: Role) -> str:
        return f"{self.organization}-{self.project}-ci-{role}"

    @property
    def profile_url(self) -> str | None:
        user = self.bot.lichess_user
        return f"https://lichess.org/@/{user}" if user else None

    def _require(self, kind: Literal["site", "bot"]) -> None:
        if self.kind != kind:
            raise ConfigError(f"{self.project} is not a {kind} project")
