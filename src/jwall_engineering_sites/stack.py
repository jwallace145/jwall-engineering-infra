"""Rendering the bundled site stack's inputs and running Terraform against it.

Each run copies the stack into a temporary directory, writes
terraform.tfvars.json and backend.hcl from the two configurations, and runs
Terraform there. The image is never mutated, and only the S3 backend touches
the network: modules and providers were fetched when the image was built.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from logging import Logger
from pathlib import Path
from typing import Any, Final

from .components import TaskRunner
from .errors import ConfigError, RunError
from .project import Project
from .settings import DatabricksProject, DatabricksSection, Settings
from .site import ENVIRONMENT, Site

#: What a stack is rendered for: a site (site commands) or a project of any kind.
Target = Site | Project

#: Directory inside the bot image where lichess-bot writes one PGN per game.
#: The harness Dockerfile and entrypoint fix it; the instance mounts it per tag.
RECORDS_CONTAINER_PATH = "/opt/lichess-bot/game_records"

#: A research project has no container image to take its architecture from.
RESEARCH_ARCHITECTURE: Final = "x86_64"

ENV_STACK_DIR = "JES_STACK_DIR"
ENV_STACKS_ROOT = "JES_STACKS_ROOT"
ENV_PLUGIN_DIR = "JES_PLUGIN_DIR"
ENV_KEEP_WORKDIR = "JES_KEEP_WORKDIR"
IMAGE_STACKS_ROOT = Path("/opt/jwall-engineering-sites/stacks")
IMAGE_PLUGIN_DIR = Path("/opt/jwall-engineering-sites/plugins")
LOCAL_STACKS_ROOT = Path("stacks")

PLAN_NO_CHANGES = 0
PLAN_ERROR = 1
PLAN_CHANGES = 2

_LOCK_INFO = re.compile(r"Lock Info:\n((?:\s{2,}\S.*\n?)+)")

#: One resource of a dataset in `terraform state list`: its dataset's name (a
#: quoted key, which may hold anything) and the segment after it, the resource type.
_DATASET_RESOURCE = re.compile(r'^module\.datasets\["((?:[^"\\]|\\.)*)"\]\.([a-z0-9_]+)\.')
_BUCKET_TYPE_PREFIX = "aws_s3_bucket"


#: The stacks of a Databricks project, in apply order; each is `stacks/databricks-<stack>`.
DATABRICKS_STACKS: Final = ("account", "workspace")

#: The workspace's deployment name: the first label of its URL.
_DEPLOYMENT_NAME = re.compile(r"^[a-z0-9-]{3,30}$")
#: `<prefix>-<deployment_name>` is a DNS label of the workspace URL.
_URL_LABEL = re.compile(r"^(?=.{1,63}$)[a-z0-9]+(-[a-z0-9]+)*$")

#: The account stack's outputs the workspace stack takes as inputs, by tfvars name.
_ACCOUNT_OUTPUTS: Final = (
    "workspace_url",
    "workspace_id",
    "admin_group_name",
    "users_group_name",
    "metastore_id",
)


def render_tfvars(
    target: Target,
    settings: Settings | None = None,
    stack: str | None = None,
    account_outputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The stack's input variables. Pure, so it can be tested.

    A bot or research project needs `settings` (health check, records prefix,
    zones and the ECR lifecycle come from config.yaml). A Databricks project
    needs `settings` and `stack` (`account` or `workspace`); the workspace stack
    also needs the account stack's outputs. Other kinds ignore both.
    """
    if isinstance(target, Project):
        if target.stack_kind == "databricks":
            if settings is None:
                raise ValueError("render_tfvars needs settings for a databricks project")
            if stack == "account":
                return _databricks_account_tfvars(target, settings)
            if stack == "workspace":
                if account_outputs is None:
                    raise ConfigError(
                        f"{target.project}: the workspace stack needs the account stack's "
                        "outputs; apply the account stack first"
                    )
                return _databricks_workspace_tfvars(target, settings, account_outputs)
            raise ValueError(
                f"a databricks project has the stacks {', '.join(DATABRICKS_STACKS)}; "
                f"got stack={stack!r}"
            )
        if target.has_components:
            if settings is None:
                raise ValueError("render_tfvars needs settings for a project with components")
            return _bot_tfvars(target, settings)
        return _site_tfvars_from_project(target)
    return _site_tfvars(target)


def _site_tfvars(site: Site) -> dict[str, Any]:
    return {
        "project": site.project,
        "repository": site.config.repository,
        "domain_name": site.domain,
        "dev_domain_name": site.config.dev.domain if site.config.dev else None,
        "enable_preview": site.config.preview.enabled,
        "zone_id": site.zone_id,
        "tags": dict(site.config.tags),
    }


def _site_tfvars_from_project(project: Project) -> dict[str, Any]:
    config = project.config
    return {
        "project": project.project,
        "repository": config.repository,
        "domain_name": config.domain,
        "dev_domain_name": config.dev.domain if config.dev else None,
        "enable_preview": config.preview.enabled,
        "zone_id": project.zone_id,
        "tags": dict(config.tags),
    }


def _lichess_bot_tfvars(project: Project, settings: Settings) -> dict[str, Any]:
    """The inputs of the image repository and the bot instance."""
    bot, image = project.bot, project.image
    return {
        "bot_enabled": True,
        "instance_type": bot.instance_type,
        "architecture": image.architecture,
        "secret_parameters": dict(bot.secrets),
        "container_environment": {
            "ENGINE_HASH_MB": str(bot.engine.hash_mb),
            "ENGINE_MOVE_OVERHEAD_MS": str(bot.engine.move_overhead_ms),
        },
        "bot_name": bot.lichess_user or "",
        "records_enabled": bot.records.s3,
        "records_prefix": settings.bot.records_prefix,
        "records_container_path": RECORDS_CONTAINER_PATH,
        "log_retention_days": bot.logs.retention_days,
        "health_log_pattern": settings.bot.health_log_pattern,
        "health_seconds": settings.bot.health_seconds,
        "keep_release_images": settings.ecr.keep_release_images,
        "expire_untagged_after_days": settings.ecr.expire_untagged_after_days,
        "ami_name": settings.bot.amis.get(image.architecture),
    }


def _research_tfvars(project: Project, settings: Settings) -> dict[str, Any]:
    """The bot's modules are gated off; its other variables keep the stack's defaults."""
    return {
        "bot_enabled": False,
        "architecture": RESEARCH_ARCHITECTURE,
        "records_enabled": False,
        # The image tasks without their own use. Without a task runner nothing
        # launches, so there is no image for the plan to look up.
        "ami_name": settings.bot.amis.get(RESEARCH_ARCHITECTURE) if project.task_runner else None,
    }


def _bot_tfvars(project: Project, settings: Settings) -> dict[str, Any]:
    runner = project.task_runner or TaskRunner(kind="task-runner")
    kind_tfvars = (
        _lichess_bot_tfvars(project, settings)
        if project.kind == "bot"
        else _research_tfvars(project, settings)
    )
    return {
        "project": project.project,
        "repository": project.config.repository,
        "tags": dict(project.config.tags),
        **kind_tfvars,
        "availability_zones": list(settings.bot.availability_zones),
        # Without a task runner the stack creates none of its resources; the
        # other variables keep the component's defaults so they stay valid.
        "task_runner_enabled": project.task_runner is not None,
        "task_instance_type": runner.default_instance_type,
        "task_max_lifetime_hours": runner.max_hours,
        "task_results_days": runner.results_days,
        "task_log_retention_days": runner.log_retention_days,
        "task_rust": runner.rust,
        "task_rust_toolchain": settings.tasks.rust_toolchain,
        # Profiles beyond cpu: one launch template and instance role each.
        "task_profiles": [
            {
                "name": name,
                "default_instance_type": profile.default_instance_type,
                "max_hours": profile.max_hours,
                "ami_name": settings.tasks.images[profile.image] if profile.image else None,
                "root_volume_gb": profile.root_volume_gb,
                "gpu": profile.gpu,
                "rust": profile.rust,
                "datasets": [
                    {
                        "name": dataset,
                        "read": list(access.read),
                        "write": list(access.write),
                        "write_once": list(access.write_once),
                    }
                    for dataset, access in profile.datasets.items()
                ],
            }
            for name, profile in runner.profiles.items()
        ],
        "datasets": [
            {
                "name": d.name,
                "bucket": d.bucket,
                "prefix": d.prefix,
                "writers": list(d.writers),
                "readers": list(d.readers),
                "transition_days": d.transition_days,
                "versioning": d.versioning,
                "expire_noncurrent_days": d.expire_noncurrent_days,
                "expire_prefixes": dict(d.expire_prefixes),
            }
            for d in project.datasets
        ],
    }


def deployment_name(project: str, prefix: str | None, requested: str | None = None) -> str | None:
    """The workspace's deployment name, or None to let Databricks pick the URL.

    Databricks accepts a deployment name only once it has configured a prefix for
    the account, and the URL is then `<prefix>-<name>`. Without a prefix in
    config.yaml this is None. With one, the name is the project file's
    `deployment_name`, else the project. Passed explicitly because the provider
    does not compute it, so a null would drift. Changing it replaces the workspace.
    """
    if prefix is None:
        return None
    name = requested or project
    if not _DEPLOYMENT_NAME.match(name):
        raise ConfigError(
            f"{project}: deployment_name {name!r} must be 3 to 30 lowercase letters, digits "
            "and hyphens; set a shorter deployment_name on the databricks-workspace component"
        )
    url_label = f"{prefix}-{name}"
    if not _URL_LABEL.match(url_label):
        raise ConfigError(
            f"{project}: the workspace URL label {url_label!r} (<deployment_name_prefix>-"
            "<deployment_name>) must be at most 63 lowercase letters, digits and single "
            "hyphens; set a shorter deployment_name on the databricks-workspace component"
        )
    return name


def _databricks_section(project: Project, settings: Settings) -> DatabricksSection:
    if settings.databricks is None:
        raise ConfigError(f"{project.project} is a databricks project but config.yaml has none")
    return settings.databricks


def _approved(project: Project) -> DatabricksProject:
    if project.databricks_approved is None:
        raise ConfigError(f"{project.project} has no projects.{project.project}.databricks entry")
    return project.databricks_approved


def _databricks_account_tfvars(project: Project, settings: Settings) -> dict[str, Any]:
    """The account stack's inputs: network, workspace, metastore and members."""
    section = _databricks_section(project, settings)
    approved = _approved(project)
    return {
        "project": project.project,
        "environment": ENVIRONMENT,
        "repository": project.config.repository,
        "tags": project.databricks_tags,
        "vpc_cidr": approved.vpc_cidr,
        "nat_ami_name": section.nat_ami,
        "metastore_name": section.metastore_name,
        "deployment_name": deployment_name(
            project.project, section.deployment_name_prefix, project.databricks.deployment_name
        ),
        "admin_user_names": list(approved.admins),
        "user_names": list(approved.users),
        "service_principal_application_id": section.client_id,
        "account_id": section.account_id,
    }


def _databricks_workspace_tfvars(
    project: Project, settings: Settings, account_outputs: Mapping[str, Any]
) -> dict[str, Any]:
    """The workspace stack's inputs: the account stack's outputs, the project's
    compute policies, its catalog and the CI roles that manage it."""
    section = _databricks_section(project, settings)
    missing = [key for key in _ACCOUNT_OUTPUTS if key not in account_outputs]
    if missing:
        raise ConfigError(
            f"{project.project}: the account stack's outputs lack {', '.join(missing)}; "
            "apply the account stack first"
        )
    tags = project.databricks_tags
    absent = [key for key in ("Project", "Owner") if key not in tags]
    if absent:
        raise ConfigError(f"{project.project}: tags need {', '.join(absent)} for the catalog")
    policies = {}
    for name in project.databricks.compute_policies:
        if name not in section.compute_policies:
            raise ConfigError(
                f"{project.project}: compute policy {name!r} is not defined under "
                "databricks.compute_policies in config.yaml"
            )
        policy = section.compute_policies[name]
        policies[name] = {"description": policy.description, "definition": policy.definition}
    account = settings.aws.account_id
    return {
        "project": project.project,
        "environment": ENVIRONMENT,
        "repository": project.config.repository,
        "tags": tags,
        **{key: account_outputs[key] for key in _ACCOUNT_OUTPUTS},
        "compute_policies": policies,
        "fixed_tags": tags,
        "governance_project": section.governance_project,
        "apply_role_arn": f"arn:aws:iam::{account}:role/{project.role_name('apply')}",
        "management_role_arns": [f"arn:aws:iam::{account}:role/{project.role_name('plan')}"],
        "catalog_tags": {"Project": tags["Project"], "Owner": tags["Owner"]},
    }


def dataset_bucket_addresses(state: list[str]) -> list[str]:
    """The dataset buckets' resources in `state`, in its order: what a destroy
    releases to keep them. The datasets' IAM grants stay, to go with their roles."""
    return [
        address
        for address in state
        if (found := _DATASET_RESOURCE.match(address))
        and found.group(2).startswith(_BUCKET_TYPE_PREFIX)
    ]


def dataset_names(addresses: list[str]) -> list[str]:
    """The datasets these addresses belong to, sorted, each once."""
    return sorted({found.group(1) for a in addresses if (found := _DATASET_RESOURCE.match(a))})


def render_backend(site: Target, state_key: str | None = None) -> str:
    """backend.hcl for `terraform init -backend-config=`; `state_key` overrides the target's."""
    return (
        f'bucket       = "{site.state_bucket}"\n'
        f'key          = "{state_key or site.state_key}"\n'
        f'region       = "{site.region}"\n'
        "use_lockfile = true\n"
        "encrypt      = true\n"
    )


def default_stack_dir(kind: str = "site") -> Path:
    """JES_STACK_DIR, else <JES_STACKS_ROOT>/<kind>, else the image's copy, else ./stacks/<kind>."""
    if ENV_STACK_DIR in os.environ:
        return Path(os.environ[ENV_STACK_DIR])
    if ENV_STACKS_ROOT in os.environ:
        return Path(os.environ[ENV_STACKS_ROOT]) / kind
    image = IMAGE_STACKS_ROOT / kind
    return image if image.is_dir() else LOCAL_STACKS_ROOT / kind


def default_plugin_dir() -> Path | None:
    if ENV_PLUGIN_DIR in os.environ:
        value = os.environ[ENV_PLUGIN_DIR]
        return Path(value) if value else None
    return IMAGE_PLUGIN_DIR if IMAGE_PLUGIN_DIR.is_dir() else None


@dataclass
class CommandResult:
    returncode: int
    output: str
    seconds: float


class Terraform:
    """Runs Terraform in a working copy of the stack."""

    def __init__(
        self,
        site: Target,
        settings: Settings,
        logger: Logger,
        stack_dir: Path | None = None,
        plugin_dir: Path | None = None,
        binary: str = "terraform",
        stack: str | None = None,
        extra_env: Mapping[str, str] = {},  # copied, never mutated
        tfvars_override: Mapping[str, Any] | None = None,
    ) -> None:
        """`stack` picks one of a project's stacks (a Databricks project's `account`
        or `workspace`): `stacks/<kind>-<stack>` and its own state key. `extra_env`
        (credentials) reaches only the Terraform process; `tfvars_override` is
        written instead of the rendered tfvars."""
        self.site = site
        self.settings = settings
        self.logger = logger
        self.kind = site.stack_kind if isinstance(site, Project) else "site"
        self.stack = stack
        #: The stack directory's name, e.g. `bot` or `databricks-account`.
        self.stack_name = self.kind if stack is None else f"{self.kind}-{stack}"
        self.state_key = (
            site.state_key
            if stack is None
            else settings.state_key(site.project, ENVIRONMENT, stack)
        )
        self.stack_dir = stack_dir or default_stack_dir(self.stack_name)
        self._extra_env = dict(extra_env)
        self.tfvars_override = dict(tfvars_override) if tfvars_override is not None else None
        self.plugin_dir = default_plugin_dir() if plugin_dir is None else plugin_dir
        self.binary = binary
        self.workdir: Path | None = None

    # -- working directory ----------------------------------------------------

    def check_version(self) -> str:
        """Fail fast unless the terraform on PATH is the version config.yaml pins."""
        try:
            completed = subprocess.run(
                [self.binary, "version", "-json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=60,
            )
        except FileNotFoundError as exc:
            raise RunError(f"{self.binary} not found on PATH") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise RunError(f"{self.binary} version failed: {exc}") from exc
        actual = str(json.loads(completed.stdout).get("terraform_version", ""))
        expected = self.settings.terraform.version
        if actual != expected:
            raise RunError(
                f"terraform {actual} is on PATH but config.yaml pins {expected}; "
                "the image and its configuration disagree"
            )
        self.logger.debug("terraform %s", actual)
        return actual

    def prepare(self) -> Path:
        if not self.stack_dir.is_dir():
            raise RunError(f"{self.stack_name} stack not found at {self.stack_dir}")
        self.check_version()
        self.workdir = Path(tempfile.mkdtemp(prefix="jwall-engineering-sites-"))
        shutil.copytree(self.stack_dir, self.workdir, dirs_exist_ok=True, symlinks=True)
        tfvars = (
            self.tfvars_override
            if self.tfvars_override is not None
            else render_tfvars(self.site, self.settings, stack=self.stack)
        )
        backend = self.backend()
        (self.workdir / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2) + "\n")
        (self.workdir / "backend.hcl").write_text(backend)
        self.logger.debug("working directory %s", self.workdir)
        self.logger.debug("terraform.tfvars.json:\n%s", json.dumps(tfvars, indent=2))
        self.logger.debug("backend.hcl:\n%s", backend)
        return self.workdir

    def backend(self) -> str:
        """backend.hcl for this stack: the target's state key, or the stack's own."""
        return render_backend(self.site, self.state_key)

    def cleanup(self, failed: bool) -> None:
        if self.workdir is None:
            return
        keep = failed and (
            os.environ.get(ENV_KEEP_WORKDIR) == "1"
            or self.logger.getEffectiveLevel() <= 10  # DEBUG
        )
        if keep:
            self.logger.warning("keeping working directory %s for inspection", self.workdir)
            return
        shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir = None

    # -- commands -------------------------------------------------------------

    def init(self, backend: bool = True) -> None:
        args = ["init", "-input=false", "-no-color"]
        if self.plugin_dir is not None:
            args.append(f"-plugin-dir={self.plugin_dir}")
        if backend:
            args += ["-backend-config=backend.hcl", "-reconfigure"]
        else:
            args.append("-backend=false")
        self._run(args, allowed=(0,), timeout=300)

    def validate(self) -> None:
        self._run(["validate", "-no-color"], allowed=(0,), timeout=120)

    def plan(self, out: Path | None = None, detailed_exitcode: bool = False) -> CommandResult:
        args = [
            "plan",
            "-input=false",
            "-no-color",
            f"-lock-timeout={self.settings.terraform.lock_timeout}",
        ]
        if detailed_exitcode:
            args.append("-detailed-exitcode")
        if out is not None:
            args.append(f"-out={out}")
        allowed = (PLAN_NO_CHANGES, PLAN_CHANGES) if detailed_exitcode else (0,)
        return self._run(
            args, allowed=allowed, timeout=self.settings.terraform.plan_timeout_seconds
        )

    def apply(self, planfile: Path) -> CommandResult:
        args = [
            "apply",
            "-input=false",
            "-no-color",
            f"-lock-timeout={self.settings.terraform.lock_timeout}",
            str(planfile),
        ]
        return self._run(
            args, allowed=(0,), timeout=self.settings.terraform.plan_timeout_seconds * 2
        )

    def destroy(self) -> CommandResult:
        args = [
            "destroy",
            "-input=false",
            "-no-color",
            "-auto-approve",
            f"-lock-timeout={self.settings.terraform.lock_timeout}",
        ]
        return self._run(
            args, allowed=(0,), timeout=self.settings.terraform.plan_timeout_seconds * 2
        )

    def state_list(self) -> list[str]:
        result = self._run(["state", "list"], allowed=(0,), timeout=120, stream=False)
        return [line.strip() for line in result.output.splitlines() if line.strip()]

    def state_rm(self, address: str) -> None:
        self._run(
            ["state", "rm", f"-lock-timeout={self.settings.terraform.lock_timeout}", address],
            allowed=(0,),
            timeout=120,
        )

    def outputs(self) -> dict[str, Any]:
        result = self._run(
            ["output", "-no-color", "-json"], allowed=(0,), timeout=120, stream=False
        )
        raw = json.loads(result.output or "{}")
        return {name: value["value"] for name, value in raw.items()}

    # -- execution ------------------------------------------------------------

    def _run(
        self,
        args: Sequence[str],
        allowed: Sequence[int],
        timeout: int,
        stream: bool = True,
    ) -> CommandResult:
        if self.workdir is None:
            raise RunError("prepare() must run before terraform")
        command = [self.binary, *args]
        env = self._environment()
        self.logger.debug("$ %s (cwd %s, timeout %ss)", " ".join(command), self.workdir, timeout)
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workdir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise RunError(f"{self.binary} not found on PATH") from exc

        lines: list[str] = []
        assert process.stdout is not None
        try:
            for line in process.stdout:
                lines.append(line)
                if stream:
                    self.logger.info("%s", line.rstrip("\n"))
                else:
                    self.logger.debug("%s", line.rstrip("\n"))
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise RunError(f"terraform {args[0]} exceeded {timeout}s and was killed") from exc
        seconds = time.monotonic() - started
        output = "".join(lines)
        self.logger.debug("terraform %s exited %s after %.1fs", args[0], returncode, seconds)

        if returncode not in allowed:
            lock = _LOCK_INFO.search(output)
            if lock:
                details = " ".join(
                    part.strip() for part in lock.group(1).splitlines() if part.strip()
                )
                self.logger.error("the state is locked: %s", details)
            raise RunError(f"terraform {args[0]} failed with exit code {returncode}", output=output)
        return CommandResult(returncode=returncode, output=output, seconds=seconds)

    def _environment(self) -> Mapping[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "TF_IN_AUTOMATION": "1",
                "TF_INPUT": "0",
                "AWS_REGION": self.site.region,
                "AWS_DEFAULT_REGION": self.site.region,
            }
        )
        env.update(self._extra_env)  # credentials: never logged or written to a file
        return env
