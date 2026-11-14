"""The structured event log: one JSON record per CLI stage, appended to S3.

Every platform action writes a record, so history and failures are visible.
The log can never fail a run: a write error is a warning. A record holds only
a fixed set of fields, and `error_class` and `failed_checks` are validated
identifiers, so no secret or free text can enter it.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from logging import Logger
from typing import TYPE_CHECKING, Any, Literal

from botocore.exceptions import BotoCoreError, ClientError

from .project import Project
from .settings import Settings
from .site import ENVIRONMENT

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

Result = Literal["started", "success", "failure", "skipped"]

BASE_KEYS = frozenset(
    {"run_id", "project", "environment", "cli_version", "repository", "commit", "actor"}
)
EXTRA_KEYS = frozenset({"workspace_id", "state_key"})
_ERROR_CLASS = re.compile(r"^[A-Z]{2,5}-[A-Z-]+$")
_CHECK_ID = re.compile(r"^[A-Z]{2,5}-[A-Z]{2,5}-\d{3}$")
_STAGE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class EventLog:
    def __init__(
        self,
        s3: S3Client | None,
        bucket: str,
        prefix: str,
        base: Mapping[str, str],
        logger: Logger,
    ) -> None:
        unknown = set(base) - BASE_KEYS
        if unknown:
            raise ValueError(f"event base fields {sorted(unknown)} are not allowed")
        if not _PROJECT.fullmatch(base.get("project", "")) or "run_id" not in base:
            raise ValueError("event base fields need a valid project and a run_id")
        self._s3 = s3
        self._bucket = bucket
        self._prefix = prefix
        self._base = dict(base)
        self._logger = logger
        self._seq = 0

    def emit(
        self,
        event: str,
        stage: str,
        result: Result,
        duration_s: float | None = None,
        error_class: str | None = None,
        failed_checks: Sequence[str] = (),
        extra: Mapping[str, str] | None = None,
    ) -> None:
        extra = extra or {}
        unknown = set(extra) - EXTRA_KEYS
        if unknown:
            raise ValueError(f"event extra keys {sorted(unknown)} are not allowed")
        if not _STAGE.fullmatch(stage) or not _STAGE.fullmatch(event):
            raise ValueError("event and stage must be short lowercase identifiers")
        if error_class is not None and not _ERROR_CLASS.fullmatch(error_class):
            raise ValueError("error_class must be a catalogue class name")
        for check_id in failed_checks:
            if not _CHECK_ID.fullmatch(check_id):
                raise ValueError("failed_checks must be check ids")
        self._seq += 1
        now = datetime.now(UTC)
        record: dict[str, Any] = {
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "seq": self._seq,
            "event": event,
            "stage": stage,
            "result": result,
            "duration_s": duration_s,
            "error_class": error_class,
            "failed_checks": list(failed_checks),
            **self._base,
            **extra,
        }
        key = (
            f"{self._prefix}project={self._base['project']}/date={now:%Y-%m-%d}/"
            f"{now:%Y%m%dT%H%M%SZ}-{self._base['run_id']}-{self._seq:03d}.json"
        )
        body = json.dumps(record, sort_keys=True)
        if self._s3 is None:
            self._logger.debug("event %s (not stored: no credentials): %s", key, body)
            return
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=body.encode(),
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "unknown")
            self._logger.warning("event %s was not written: %s", key, code)
        except BotoCoreError as error:
            self._logger.warning("event %s was not written: %s", key, type(error).__name__)


def base_fields(project: Project, settings: Settings, tool_version: str) -> dict[str, str]:
    """The fields every record of one run shares. `actor` and `commit` are "local" off CI."""
    return {
        "run_id": str(uuid.uuid4()),
        "project": project.project,
        "environment": ENVIRONMENT,
        "cli_version": tool_version,
        "repository": project.config.repository,
        "commit": os.environ.get("GITHUB_SHA") or "local",
        "actor": os.environ.get("GITHUB_ACTOR") or "local",
    }
