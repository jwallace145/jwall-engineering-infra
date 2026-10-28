"""The container image of a bot project: build context, build, smoke test, push.

`build` needs only Docker: the image is tagged locally as
`<image_repository>:<tag>`. `push` logs in to ECR, retags it with the
registry and pushes. Tags are immutable in the repository; callers check
`tag_exists` first so an existing tag is refused with a clear message.
"""

from __future__ import annotations

import base64
import re
import shutil
import subprocess
from importlib import resources
from logging import Logger
from pathlib import Path
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from .errors import PreflightError, RunError
from .project import Project

if TYPE_CHECKING:
    from mypy_boto3_ecr import ECRClient

PLACEHOLDER = "LICHESS_TOKEN_PLACEHOLDER"
KIND_PACKAGE = "jwall_engineering_sites.kinds.lichess_bot"
PLATFORMS = {"x86_64": "linux/amd64", "arm64": "linux/arm64"}
_NODES = re.compile(r"^Nodes searched\s*:?\s*(\d+)", re.MULTILINE)


def local_tag(project: Project, tag: str) -> str:
    return f"{project.image_repository}:{tag}"


def remote_tag(project: Project, registry: str, tag: str) -> str:
    return f"{registry}/{project.image_repository}:{tag}"


def _docker(args: list[str], logger: Logger, docker: str, stdin: str | None = None) -> str:
    logger.debug("$ %s %s", docker, " ".join(args))
    try:
        completed = subprocess.run(
            [docker, *args], input=stdin, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise RunError(f"{docker} not found on PATH") from exc
    if completed.returncode != 0:
        raise RunError(
            f"docker {args[0]} failed with exit code {completed.returncode}: "
            f"{(completed.stderr or completed.stdout).strip()[-2000:]}"
        )
    return completed.stdout


def prepare_context(
    project: Project, tag: str, binary: Path, workdir: Path, consumer_root: Path
) -> Path:
    """Copy the Dockerfile, the entrypoint, the engine and the consumer's config
    into a fresh build context."""
    config = consumer_root / project.bot.config
    if not config.is_file():
        raise PreflightError(f"lichess-bot config {project.bot.config} not found in the repository")
    if PLACEHOLDER not in config.read_text():
        raise PreflightError(
            f"{project.bot.config} must hold the token as {PLACEHOLDER}; "
            "the real token is injected at container start and never committed"
        )
    if workdir.exists():
        shutil.rmtree(workdir)
    (workdir / "engine").mkdir(parents=True)
    kind = resources.files(KIND_PACKAGE)
    if project.image.dockerfile:
        dockerfile = consumer_root / project.image.dockerfile
        if not dockerfile.is_file():
            raise PreflightError(f"dockerfile {project.image.dockerfile} not found")
        shutil.copyfile(dockerfile, workdir / "Dockerfile")
    else:
        (workdir / "Dockerfile").write_text(kind.joinpath("Dockerfile").read_text())
    (workdir / "entrypoint.py").write_text(kind.joinpath("entrypoint.py").read_text())
    engine = workdir / "engine" / project.image.artifact.binary
    shutil.copyfile(binary, engine)
    engine.chmod(0o755)
    shutil.copyfile(config, workdir / "config.yml")
    return workdir


def build(context: Path, project: Project, tag: str, logger: Logger, docker: str = "docker") -> str:
    name = local_tag(project, tag)
    _docker(
        [
            "build",
            "--platform",
            PLATFORMS[project.image.architecture],
            "--build-arg",
            f"ENGINE_BINARY={project.image.artifact.binary}",
            "--build-arg",
            f"VERSION={tag}",
            "--build-arg",
            f"SOURCE_REPOSITORY=https://github.com/{project.config.repository}",
            "--tag",
            name,
            str(context),
        ],
        logger,
        docker,
    )
    logger.info("built %s", name)
    return name


def smoke_test(name: str, project: Project, logger: Logger, docker: str = "docker") -> str:
    """Run the engine's bench inside the image, offline; return the node count."""
    binary = project.image.artifact.binary
    try:
        output = _docker(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--platform",
                PLATFORMS[project.image.architecture],
                "--entrypoint",
                f"/opt/engine/{binary}",
                name,
                "bench",
                "--depth",
                "6",
            ],
            logger,
            docker,
        )
    except RunError as exc:
        raise RunError(f"smoke test of {name} failed: {exc}") from exc
    match = _NODES.search(output)
    if not match:
        raise RunError(f"smoke test of {name}: `bench --depth 6` printed no 'Nodes searched' line")
    logger.info("smoke test: %s searched %s nodes at depth 6", name, match.group(1))
    return match.group(1)


def tag_exists(ecr: ECRClient, repository: str, tag: str) -> bool:
    try:
        ecr.describe_images(repositoryName=repository, imageIds=[{"imageTag": tag}])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ImageNotFoundException":
            return False
        raise RunError(f"cannot check {repository}:{tag}: {exc}") from exc
    return True


def login(ecr: ECRClient, logger: Logger, docker: str = "docker") -> str:
    """docker login to the account's registry; returns the registry host."""
    data = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(data["authorizationToken"]).decode().split(":", 1)
    registry = data["proxyEndpoint"].removeprefix("https://")
    _docker(["login", "--username", user, "--password-stdin", registry], logger, docker, password)
    logger.info("logged in to %s", registry)
    return registry


def push(project: Project, registry: str, tag: str, logger: Logger, docker: str = "docker") -> str:
    remote = remote_tag(project, registry, tag)
    _docker(["tag", local_tag(project, tag), remote], logger, docker)
    _docker(["push", remote], logger, docker)
    logger.info("pushed %s", remote)
    return remote
