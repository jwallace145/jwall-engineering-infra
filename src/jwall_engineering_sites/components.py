"""Components of a bot project: the container image and the Lichess bot.

A project file with `components` (instead of `domain` and `site`) describes a
container image built from the repository's GitHub releases and a Lichess bot
that runs it on one EC2 instance. Names, roles and paths are derived in
`project.py`; this module only validates what the file says.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


Component = Annotated[ContainerImage | LichessBot, Field(discriminator="kind")]
