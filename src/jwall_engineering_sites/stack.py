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
from typing import Any

from .errors import RunError
from .settings import Settings
from .site import Site

ENV_STACK_DIR = "JES_STACK_DIR"
ENV_PLUGIN_DIR = "JES_PLUGIN_DIR"
ENV_KEEP_WORKDIR = "JES_KEEP_WORKDIR"
IMAGE_STACK_DIR = Path("/opt/jwall-engineering-sites/stacks/site")
IMAGE_PLUGIN_DIR = Path("/opt/jwall-engineering-sites/plugins")
LOCAL_STACK_DIR = Path("stacks/site")

PLAN_NO_CHANGES = 0
PLAN_ERROR = 1
PLAN_CHANGES = 2

_LOCK_INFO = re.compile(r"Lock Info:\n((?:\s{2,}\S.*\n?)+)")


def render_tfvars(site: Site) -> dict[str, Any]:
    """The stack's input variables for a site. Pure, so it can be tested."""
    return {
        "project": site.project,
        "repository": site.config.repository,
        "domain_name": site.domain,
        "dev_domain_name": site.config.dev.domain if site.config.dev else None,
        "enable_preview": site.config.preview.enabled,
        "zone_id": site.zone_id,
        "tags": dict(site.config.tags),
    }


def render_backend(site: Site) -> str:
    """backend.hcl for `terraform init -backend-config=`."""
    return (
        f'bucket       = "{site.state_bucket}"\n'
        f'key          = "{site.state_key}"\n'
        f'region       = "{site.region}"\n'
        "use_lockfile = true\n"
        "encrypt      = true\n"
    )


def default_stack_dir() -> Path:
    if ENV_STACK_DIR in os.environ:
        return Path(os.environ[ENV_STACK_DIR])
    return IMAGE_STACK_DIR if IMAGE_STACK_DIR.is_dir() else LOCAL_STACK_DIR


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
        site: Site,
        settings: Settings,
        logger: Logger,
        stack_dir: Path | None = None,
        plugin_dir: Path | None = None,
        binary: str = "terraform",
    ) -> None:
        self.site = site
        self.settings = settings
        self.logger = logger
        self.stack_dir = stack_dir or default_stack_dir()
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
            raise RunError(f"site stack not found at {self.stack_dir}")
        self.check_version()
        self.workdir = Path(tempfile.mkdtemp(prefix="jwall-engineering-sites-"))
        shutil.copytree(self.stack_dir, self.workdir, dirs_exist_ok=True, symlinks=True)
        tfvars = render_tfvars(self.site)
        backend = render_backend(self.site)
        (self.workdir / "terraform.tfvars.json").write_text(json.dumps(tfvars, indent=2) + "\n")
        (self.workdir / "backend.hcl").write_text(backend)
        self.logger.debug("working directory %s", self.workdir)
        self.logger.debug("terraform.tfvars.json:\n%s", json.dumps(tfvars, indent=2))
        self.logger.debug("backend.hcl:\n%s", backend)
        return self.workdir

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
            raise RunError(f"terraform {args[0]} failed with exit code {returncode}")
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
        return env
