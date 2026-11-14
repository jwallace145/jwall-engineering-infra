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
_RUN_ID = re.compile(r"^[A-Za-z0-9-]+$")
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
        if not _PROJECT.fullmatch(base.get("project", "")):
            raise ValueError("event base fields need a valid project")
        if not _RUN_ID.fullmatch(base.get("run_id", "")):
            raise ValueError("event run_id must match [A-Za-z0-9-]+")
        self._s3 = s3
        self._bucket = bucket
        self._prefix = prefix
        self._base = dict(base)
        self._logger = logger
        self._seq = 0

    def _valid(self, value: str | None, pattern: re.Pattern[str], field: str) -> str | None:
        if value is None or pattern.fullmatch(value):
            return value
        self._logger.warning("event %s is not valid; recorded as null", field)
        return None

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
        # A bad value is dropped with a warning naming the field, never the value:
        # the log must not fail a deploy or mask the error it is recording.
        clean_extra = {k: v for k, v in (extra or {}).items() if k in EXTRA_KEYS}
        if len(clean_extra) != len(extra or {}):
            self._logger.warning("event extra had fields that are not allowed; dropped them")
        safe_event = self._valid(event, _STAGE, "event")
        safe_stage = self._valid(stage, _STAGE, "stage")
        safe_class = self._valid(error_class, _ERROR_CLASS, "error_class")
        safe_checks = [c for c in failed_checks if _CHECK_ID.fullmatch(c)]
        if len(safe_checks) != len(failed_checks):
            self._logger.warning("event failed_checks had invalid entries; dropped them")
        self._seq += 1
        now = datetime.now(UTC)
        record: dict[str, Any] = {
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "seq": self._seq,
            "event": safe_event,
            "stage": safe_stage,
            "result": result,
            "duration_s": duration_s,
            "error_class": safe_class,
            "failed_checks": safe_checks,
            **self._base,
            **clean_extra,
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
            if code in ("PreconditionFailed", "412"):
                # A retry after a timeout whose first attempt landed: already written.
                self._logger.debug("event %s already exists", key)
                return
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
