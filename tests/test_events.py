"""The event log: one append-only record per CLI stage, never able to fail a run."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from jwall_engineering_sites.events import EventLog, base_fields
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings
from jwall_engineering_sites.site import load_site_config
from tests.conftest import FIXTURES

BUCKET = "audit-logs"
PREFIX = "events/"
SECRET = "dapi-SENTINEL-secret-value-0123456789"
BASE = {
    "run_id": "11111111-2222-3333-4444-555555555555",
    "project": "databricks-poc",
    "environment": "prod",
    "cli_version": "1.2.3",
    "repository": "databricks-poc",
    "commit": "abc123",
    "actor": "octocat",
}
KEY = re.compile(
    r"^events/project=databricks-poc/date=\d{4}-\d{2}-\d{2}/"
    r"\d{8}T\d{6}Z-11111111-2222-3333-4444-555555555555-\d{3}\.json$"
)


@pytest.fixture
def databricks(settings: Settings) -> Project:
    return Project.from_config(
        load_site_config(FIXTURES / "bots" / "databricks-poc.yaml"), settings
    )


@pytest.fixture
def s3() -> Any:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def _log(s3: Any, logger: logging.Logger) -> EventLog:
    return EventLog(s3, BUCKET, PREFIX, BASE, logger)


def _records(s3: Any) -> list[tuple[str, dict[str, Any]]]:
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]
    return [
        (k, json.loads(s3.get_object(Bucket=BUCKET, Key=k)["Body"].read())) for k in sorted(keys)
    ]


class _Recording:
    """Wraps a client, keeping the arguments of every put_object."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> Any:
        self.puts.append(kwargs)
        return self.inner.put_object(**kwargs)


def test_event_written_with_conditional_put(s3: Any, logger: logging.Logger) -> None:
    rec = _Recording(s3)
    log = EventLog(rec, BUCKET, PREFIX, BASE, logger)  # type: ignore[arg-type]
    log.emit("preflight", "preflight", "success", duration_s=1.5, failed_checks=["DBX-NET-001"])
    assert [p["IfNoneMatch"] for p in rec.puts] == ["*"]
    assert rec.puts[0]["ContentType"] == "application/json"
    ((key, record),) = _records(s3)
    assert KEY.match(key)
    assert record["event"] == "preflight"
    assert record["stage"] == "preflight"
    assert record["result"] == "success"
    assert record["duration_s"] == 1.5
    assert record["failed_checks"] == ["DBX-NET-001"]
    assert record["error_class"] is None
    assert record["run_id"] == BASE["run_id"]
    assert record["project"] == "databricks-poc"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", record["timestamp"])


def test_second_write_of_same_key_is_not_overwritten(s3: Any, logger: logging.Logger) -> None:
    s3.put_object(Bucket=BUCKET, Key="k", Body=b"first")
    try:
        s3.put_object(Bucket=BUCKET, Key="k", Body=b"second", IfNoneMatch="*")
    except ClientError as error:
        assert error.response["Error"]["Code"] in {"PreconditionFailed", "412"}
    else:
        pytest.skip("moto does not enforce IfNoneMatch; the parameter is asserted above")
    assert s3.get_object(Bucket=BUCKET, Key="k")["Body"].read() == b"first"


def test_keys_partitioned_by_project_then_date(s3: Any, logger: logging.Logger) -> None:
    log = _log(s3, logger)
    log.emit("plan", "plan", "started")
    log.emit("plan", "plan", "success")
    keys = [k for k, _ in _records(s3)]
    assert len(keys) == 2
    assert all(KEY.match(k) for k in keys)
    assert [k[-9:] for k in keys] == ["-001.json", "-002.json"]


def test_event_write_failure_does_not_fail_run(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    class Broken:
        def put_object(self, **_: Any) -> None:
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutObject")

    log = EventLog(Broken(), BUCKET, PREFIX, BASE, logger)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log.emit("apply", "apply", "failure", error_class="TF-ALREADY-EXISTS")
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_botocore_error_does_not_fail_run(logger: logging.Logger) -> None:
    class Down:
        def put_object(self, **_: Any) -> None:
            raise EndpointConnectionError(endpoint_url="https://s3.example")

    EventLog(Down(), BUCKET, PREFIX, BASE, logger).emit("apply", "apply", "started")  # type: ignore[arg-type]


def test_precondition_failed_is_a_warning(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    class Exists:
        def put_object(self, **_: Any) -> None:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "x"}}, "PutObject"
            )

    log = EventLog(Exists(), BUCKET, PREFIX, BASE, logger)  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger=logger.name):
        log.emit("apply", "apply", "success")
    assert any("PreconditionFailed" in r.getMessage() for r in caplog.records)


def test_no_client_logs_at_debug_only(
    logger: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        EventLog(None, BUCKET, PREFIX, BASE, logger).emit("plan", "plan", "success")
    assert [r.levelno for r in caplog.records] == [logging.DEBUG]


def test_extra_rejects_unknown_keys(s3: Any, logger: logging.Logger) -> None:
    log = _log(s3, logger)
    with pytest.raises(ValueError, match="client_secret"):
        log.emit("plan", "plan", "success", extra={"client_secret": SECRET})
    log.emit("plan", "plan", "success", extra={"workspace_id": "123", "state_key": "a/b.tfstate"})
    ((_, record),) = _records(s3)
    assert record["workspace_id"] == "123"
    assert record["state_key"] == "a/b.tfstate"


def test_base_with_unknown_keys_rejected(s3: Any, logger: logging.Logger) -> None:
    with pytest.raises(ValueError):
        EventLog(s3, BUCKET, PREFIX, {**BASE, "token": SECRET}, logger)


def test_record_has_no_secret_fields(s3: Any, logger: logging.Logger) -> None:
    log = _log(s3, logger)
    for bad in (SECRET, f"DBX-AUTH {SECRET}", "lowercase", ""):
        with pytest.raises(ValueError):
            log.emit("apply", "apply", "failure", error_class=bad)
    for bad_check in (SECRET, "DBX-NET-1"):
        with pytest.raises(ValueError):
            log.emit("preflight", "preflight", "failure", failed_checks=[bad_check])
    log.emit("apply", "apply", "failure", error_class="DBX-AUTH", failed_checks=["DBX-NET-001"])
    log.emit("plan", "plan", "success")
    raw = "".join(json.dumps(r) for _, r in _records(s3))
    assert SECRET not in raw
    allowed = {
        "timestamp", "event", "stage", "result", "duration_s", "error_class", "failed_checks",
        "run_id", "project", "environment", "cli_version", "repository", "commit", "actor",
        "workspace_id", "state_key", "seq",
    }  # fmt: skip
    for _, record in _records(s3):
        assert set(record) <= allowed


def test_base_fields_from_environment(
    databricks: Project, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTOR", "octocat")
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    fields = base_fields(databricks, settings, "9.9.9")
    assert fields["project"] == databricks.project
    assert fields["environment"] == "prod"
    assert fields["cli_version"] == "9.9.9"
    assert fields["repository"] == databricks.config.repository
    assert fields["actor"] == "octocat"
    assert fields["commit"] == "deadbeef"
    assert re.fullmatch(r"[0-9a-f-]{36}", fields["run_id"])


def test_base_fields_local_defaults(
    databricks: Project, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    fields = base_fields(databricks, settings, "9.9.9")
    assert fields["actor"] == "local"
    assert fields["commit"] == "local"
