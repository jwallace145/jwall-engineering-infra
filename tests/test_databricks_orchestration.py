"""`plan`, `apply`, `destroy`, `outputs` and `validate` on a Databricks project.

The CLI runs the fake terraform (tests/fixtures/bin/terraform), whose two stacks
are scripted apart with FAKE_TF_ACCOUNT_* and FAKE_TF_WORKSPACE_*; AWS is moto.
The built-in checks are replaced with one passing check per stage.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import boto3
import pytest
import yaml
from moto import mock_aws
from typer.testing import CliRunner

from jwall_engineering_sites import cli, databricks_auth, databricks_destroy
from jwall_engineering_sites.checks import framework
from jwall_engineering_sites.checks.framework import (
    CheckContext,
    CheckResult,
    Outcome,
    Severity,
    Stage,
)
from jwall_engineering_sites.databricks import workspace_plan_stale
from jwall_engineering_sites.databricks_names import derived_names
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.site import load_site_config

from .conftest import FIXTURES

runner = CliRunner()
SENTINEL = "dapi-SENTINEL-s3cr3t-value"
PARAM = "/jwall-engineering/databricks/client-secret"
AUDIT_BUCKET = "jwall-engineering-databricks-audit-logs"
PLAN_ROLE = "arn:aws:sts::650503560686:assumed-role/jwall-engineering-databricks-poc-ci-plan/s"
APPLY_ROLE = "arn:aws:sts::650503560686:assumed-role/jwall-engineering-databricks-poc-ci-apply/s"
PARTIAL = "account stack applied; workspace stack failed; re-running apply is safe"
DEFERRED = "workspace stack is planned after the workspace exists"
WORKSPACE_ADDRESS = "module.workspace.databricks_mws_workspaces.this"

ACCOUNT_OUTPUTS = {
    "workspace_id": 42,
    "workspace_url": "https://dbc-42.cloud.databricks.com",
    "admin_group_name": "databricks-poc-workspace-admins",
    "users_group_name": "databricks-poc-workspace-users",
    "root_bucket_name": "jwall-engineering-databricks-poc-databricks-root",
    "metastore_id": "m-1",
}
WORKSPACE_OUTPUTS = {
    "catalog_name": "databricks_poc",
    "catalog_bucket_name": "jwall-engineering-databricks-poc-databricks-catalog",
    "cluster_policy_ids": {"jobs": "p-1"},
}


class FakeClients:
    instances: ClassVar[list[FakeClients]] = []

    def __init__(self, settings: Any, secret: str, account_outputs: Any = None) -> None:
        self.account_outputs = account_outputs
        FakeClients.instances.append(self)

    @property
    def account(self) -> Any:
        raise AssertionError("not used")

    def workspace(self, host: str) -> Any:
        raise AssertionError("not used")


def _as_role(arn: str) -> AbstractContextManager[Any]:
    return patch("jwall_engineering_sites.aws.caller_identity", return_value=("650503560686", arn))


def _outputs_file(path: Path, values: dict[str, Any]) -> Path:
    path.write_text(json.dumps({k: {"value": v} for k, v in values.items()}))
    return path


@pytest.fixture
def loaded() -> Iterator[MagicMock]:
    """One passing check per stage; returns the patched `load_builtin_checks`."""
    FakeClients.instances = []
    with framework.isolated_registry():
        for number, stage in enumerate(Stage, start=1):

            def make(stage: Stage, number: int) -> framework.CheckFn:
                def fn(ctx: CheckContext) -> Iterable[CheckResult]:
                    if stage in (Stage.BETWEEN, Stage.AFTER):
                        assert ctx.databricks is not None
                        assert ctx.databricks.account_outputs == ACCOUNT_OUTPUTS
                    yield CheckResult(
                        f"DBX-TST-00{number}",
                        f"{stage.value} check",
                        Outcome.PASS,
                        Severity.ERROR,
                        {},
                        None,
                        None,
                    )

                return fn

            framework.check(
                f"DBX-TST-00{number}", f"{stage.value} check", stage, frozenset({"databricks"})
            )(make(stage, number))
        with (
            patch("jwall_engineering_sites.checks.load_builtin_checks") as load,
            patch.object(databricks_auth, "DatabricksClients", FakeClients),
        ):
            yield load


@pytest.fixture
def dbx(
    databricks_workspace: Path,
    fake_terraform: Path,
    loaded: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Both stacks applied once already (outputs present); AWS mocked with the secret stored."""
    monkeypatch.setenv("FAKE_TF_STACK_LOG", str(tmp_path / "stacks.log"))
    monkeypatch.setenv(
        "FAKE_TF_ACCOUNT_OUTPUTS", str(_outputs_file(tmp_path / "account.json", ACCOUNT_OUTPUTS))
    )
    monkeypatch.setenv(
        "FAKE_TF_WORKSPACE_OUTPUTS",
        str(_outputs_file(tmp_path / "workspace.json", WORKSPACE_OUTPUTS)),
    )
    tfvars = tmp_path / "tfvars"
    tfvars.mkdir()
    monkeypatch.setenv("FAKE_TF_TFVARS_DIR", str(tfvars))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    with mock_aws():
        boto3.client("ssm", region_name="us-east-1").put_parameter(
            Name=PARAM, Value=SENTINEL, Type="SecureString"
        )
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=AUDIT_BUCKET)
        yield databricks_workspace


def _stack_calls(tmp_path: Path) -> list[str]:
    """`<stack> <command>` per terraform call, e.g. `account apply`."""
    path = tmp_path / "stacks.log"
    if not path.exists():
        return []
    return [" ".join(line.split()[:2]) for line in path.read_text().splitlines()]


def _reset_calls(tmp_path: Path) -> None:
    (tmp_path / "stacks.log").unlink(missing_ok=True)


def _events() -> list[dict[str, Any]]:
    s3 = boto3.client("s3", region_name="us-east-1")
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=AUDIT_BUCKET).get("Contents", [])]
    records = [json.loads(s3.get_object(Bucket=AUDIT_BUCKET, Key=k)["Body"].read()) for k in keys]
    return sorted(records, key=lambda r: (r["run_id"], r["seq"]))


def _steps(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(e["stage"], e["result"]) for e in events]


def _artifact(root: Path, name: str) -> str:
    return (root / ".jwall-engineering-sites" / name).read_text()


def _project() -> Project:
    from jwall_engineering_sites.settings import load_settings

    settings = load_settings(FIXTURES / "config.yaml", environ={})
    return Project.from_config(
        load_site_config(FIXTURES / "bots" / "databricks-poc.yaml"), settings
    )


def _ok(*stages: str) -> list[tuple[str, str]]:
    return [(s, r) for s in stages for r in ("started", "success")]


# -- apply ----------------------------------------------------------------------


def test_apply_runs_stages_in_order(
    dbx: Path, loaded: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TF_ACCOUNT_PLAN_EXIT", "2")
    monkeypatch.setenv("FAKE_TF_WORKSPACE_PLAN_EXIT", "2")
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["apply"])
        events = _events()
    assert result.exit_code == 0, result.output
    loaded.assert_called()
    assert _steps(events) == _ok(
        "checks-config",
        "checks-live",
        "account-plan",
        "account-apply",
        "readiness",
        "workspace-plan",
        "workspace-apply",
        "verify",
    )
    assert {e["event"] for e in events} == {"apply"}
    assert len({e["run_id"] for e in events}) == 1
    workspace_events = [e for e in events if e["stage"].startswith("workspace-")]
    assert {e.get("workspace_id") for e in workspace_events} == {"42"}
    assert workspace_events[0]["state_key"].endswith("-workspace.tfstate")
    assert _stack_calls(tmp_path) == [
        "account init",
        "account plan",
        "account apply",
        "account output",
        "workspace init",
        "workspace plan",
        "workspace apply",
        "workspace output",
    ]
    tfvars = json.loads((tmp_path / "tfvars" / "WORKSPACE.json").read_text())
    assert tfvars["workspace_url"] == ACCOUNT_OUTPUTS["workspace_url"]
    assert tfvars["metastore_id"] == "m-1"
    merged = json.loads(_artifact(dbx, "outputs.json"))
    assert merged["workspace_url"] == ACCOUNT_OUTPUTS["workspace_url"]
    assert merged["catalog_name"] == "databricks_poc"


def test_apply_resumes_after_workspace_failure(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TF_ACCOUNT_PLAN_EXIT", "2")
    monkeypatch.setenv("FAKE_TF_WORKSPACE_PLAN_EXIT", "2")
    monkeypatch.setenv("FAKE_TF_WORKSPACE_FAIL_ON", "apply")
    monkeypatch.setenv(
        "FAKE_TF_WORKSPACE_ERROR", str(FIXTURES / "errors" / "AWS-IAM-PROPAGATION.txt")
    )
    with _as_role(APPLY_ROLE):
        first = runner.invoke(cli.app, ["apply"])
        events = _events()
    assert first.exit_code == 1, first.output
    summary = (tmp_path / "summary.md").read_text()
    assert PARTIAL in summary
    assert "AWS-IAM-PROPAGATION" in summary and "aws_iam_role.uc" in summary
    assert PARTIAL in first.output
    failed = [e for e in events if e["result"] == "failure"]
    assert [(e["stage"], e["error_class"]) for e in failed] == [
        ("workspace-apply", "AWS-IAM-PROPAGATION")
    ]
    assert "verify" not in {e["stage"] for e in events}
    assert "AWS-IAM-PROPAGATION" in _artifact(dbx, "failure.md")

    # The re-run: the account stack has nothing left to do; the workspace stack applies.
    _reset_calls(tmp_path)
    monkeypatch.setenv("FAKE_TF_ACCOUNT_PLAN_EXIT", "0")
    monkeypatch.delenv("FAKE_TF_WORKSPACE_FAIL_ON")
    with _as_role(APPLY_ROLE):
        second = runner.invoke(cli.app, ["apply"])
        rerun = [e for e in _events() if e["run_id"] != events[0]["run_id"]]
    assert second.exit_code == 0, second.output
    calls = _stack_calls(tmp_path)
    assert "account apply" not in calls
    assert "account plan" in calls and "workspace apply" in calls
    assert ("account-apply", "skipped") in _steps(rerun)
    assert ("verify", "success") in _steps(rerun)


def test_apply_account_failure_is_not_partial(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TF_ACCOUNT_PLAN_EXIT", "2")
    monkeypatch.setenv("FAKE_TF_ACCOUNT_FAIL_ON", "apply")
    monkeypatch.setenv(
        "FAKE_TF_ACCOUNT_ERROR", str(FIXTURES / "errors" / "DBX-CRED-VALIDATION.txt")
    )
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["apply"])
        events = _events()
    assert result.exit_code == 1, result.output
    summary = (tmp_path / "summary.md").read_text()
    assert "DBX-CRED-VALIDATION" in summary and "workspace stack failed" not in summary
    assert not any(c.startswith("workspace") for c in _stack_calls(tmp_path))
    assert ("account-apply", "failure") in _steps(events)
    assert "readiness" not in {e["stage"] for e in events}


def test_live_error_check_stops_before_terraform(
    dbx: Path, fake_terraform: Path, tmp_path: Path
) -> None:
    framework.check("DBX-TST-009", "always fails", Stage.LIVE, frozenset({"databricks"}))(
        lambda ctx: [
            CheckResult(
                "DBX-TST-009", "always fails", Outcome.FAIL, Severity.ERROR, {}, "fix it", None
            )
        ]
    )
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["apply"])
        events = _events()
    assert result.exit_code == 3, result.output
    assert "DBX-TST-009" in result.output and "fix it" in result.output
    assert not fake_terraform.exists()  # not even `terraform version`
    live = [e for e in events if e["stage"] == "checks-live"]
    assert [(e["result"], e["failed_checks"]) for e in live] == [
        ("started", []),
        ("failure", ["DBX-TST-009"]),
    ]
    assert "DBX-TST-009" in (tmp_path / "summary.md").read_text()


def test_readiness_failure_stops_before_the_workspace_stack(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    framework.check("DBX-TST-010", "never ready", Stage.BETWEEN, frozenset({"databricks"}))(
        lambda ctx: [
            CheckResult("DBX-TST-010", "never ready", Outcome.FAIL, Severity.ERROR, {}, None, None)
        ]
    )
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["apply"])
    assert result.exit_code == 3, result.output
    assert not any(c.startswith("workspace") for c in _stack_calls(tmp_path))
    assert "re-running apply is safe" in (tmp_path / "summary.md").read_text()


def test_a_failed_after_check_still_leaves_the_workspace_url(dbx: Path, tmp_path: Path) -> None:
    framework.check("DBX-TST-011", "not verified", Stage.AFTER, frozenset({"databricks"}))(
        lambda ctx: [
            CheckResult("DBX-TST-011", "not verified", Outcome.FAIL, Severity.ERROR, {}, None, None)
        ]
    )
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["apply"])
    assert result.exit_code == 3, result.output
    assert "DBX-TST-011" in result.output
    merged = json.loads(_artifact(dbx, "outputs.json"))
    assert merged["workspace_url"] == ACCOUNT_OUTPUTS["workspace_url"]
    summary = (tmp_path / "summary.md").read_text()
    assert str(ACCOUNT_OUTPUTS["workspace_url"]) in summary


def test_event_store_unreachable_does_not_fail_apply(dbx: Path) -> None:
    boto3.client("s3", region_name="us-east-1").delete_bucket(Bucket=AUDIT_BUCKET)
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["apply"])
    assert result.exit_code == 0, result.output
    assert "was not written" in result.output


# -- plan -----------------------------------------------------------------------


def test_plan_without_workspace_reports_deferred(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TF_ACCOUNT_OUTPUTS", str(_outputs_file(tmp_path / "empty.json", {})))
    monkeypatch.setenv("FAKE_TF_ACCOUNT_PLAN_EXIT", "2")
    monkeypatch.setenv("FAKE_TF_PLAN_SUMMARY", "30 to add, 0 to change, 0 to destroy.")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))
    with _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["plan"])
        events = _events()
    assert result.exit_code == 0, result.output
    assert not any(c.startswith("workspace") for c in _stack_calls(tmp_path))
    comment = _artifact(dbx, "plan-comment.md")
    assert DEFERRED in comment
    assert "30" in comment and "Account stack" in comment and "Workspace stack" in comment
    assert "DBX-TST-002" in comment  # the check table
    assert DEFERRED in result.output
    assert ("workspace-plan", "skipped") in _steps(events)
    assert "result=changes" in (tmp_path / "gh-output").read_text()


def test_plan_plans_both_stacks(dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["plan"])
        events = _events()
    assert result.exit_code == 0, result.output
    assert _stack_calls(tmp_path) == [
        "account init",
        "account plan",
        "account output",
        "workspace init",
        "workspace plan",
    ]
    assert _steps(events) == _ok("checks-config", "checks-live", "account-plan", "workspace-plan")
    comment = _artifact(dbx, "plan-comment.md")
    assert DEFERRED not in comment and "stale" not in comment.lower()
    assert "Databricks apply" in comment


def test_plan_flags_stale_workspace_plan(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = tmp_path / "account-plan.txt"
    plan.write_text(
        f"  # {WORKSPACE_ADDRESS} must be replaced\nPlan: 1 to add, 0 to change, 1 to destroy.\n"
    )
    monkeypatch.setenv("FAKE_TF_ACCOUNT_PLAN", str(plan))
    monkeypatch.setenv("FAKE_TF_ACCOUNT_PLAN_EXIT", "2")
    with _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["plan"])
    assert result.exit_code == 0, result.output
    comment = _artifact(dbx, "plan-comment.md")
    assert "stale" in comment.lower()
    assert "workspace plan is stale" in result.output.lower()


@pytest.mark.parametrize(
    ("text", "stale"),
    [
        (f"  # {WORKSPACE_ADDRESS} must be replaced\n", True),
        (f"  # {WORKSPACE_ADDRESS} will be destroyed\n", True),
        (f"  # {WORKSPACE_ADDRESS} will be updated in-place\n", False),
        ("  # module.network.aws_vpc.this must be replaced\n", False),
        ("No changes.\n", False),
    ],
)
def test_workspace_plan_stale(text: str, stale: bool) -> None:
    assert workspace_plan_stale(text) is stale


def test_plan_account_failure_writes_the_comment_and_fails(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TF_ACCOUNT_FAIL_ON", "plan")
    monkeypatch.setenv("FAKE_TF_ACCOUNT_ERROR", str(FIXTURES / "errors" / "DBX-AUTH.txt"))
    with _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["plan"])
        events = _events()
    assert result.exit_code == 1, result.output
    comment = _artifact(dbx, "plan-comment.md")
    assert "DBX-AUTH" in comment and "Plan failed" in comment
    assert not any(c.startswith("workspace") for c in _stack_calls(tmp_path))
    failed = [e for e in events if e["result"] == "failure"]
    assert [(e["stage"], e["error_class"]) for e in failed] == [("account-plan", "DBX-AUTH")]


# -- the secret ----------------------------------------------------------------


def test_secret_never_in_outputs_or_comment(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider that prints the secret (in its log and its error) leaks it nowhere."""
    monkeypatch.setenv("FAKE_TF_ECHO_SECRET", "1")
    monkeypatch.setenv("FAKE_TF_ENV", str(tmp_path / "terraform-env"))
    monkeypatch.setenv("FAKE_TF_ACCOUNT_PLAN_EXIT", "2")
    monkeypatch.setenv("FAKE_TF_WORKSPACE_PLAN_EXIT", "2")
    error = tmp_path / "leaky-error.txt"
    error.write_text(
        f"Error: cannot create catalog: invalid_client for secret {SENTINEL}\n\n"
        "  with module.catalog.databricks_catalog.this,\n"
    )
    with _as_role(PLAN_ROLE):
        planned = runner.invoke(cli.app, ["plan"])
    monkeypatch.setenv("FAKE_TF_WORKSPACE_FAIL_ON", "apply")
    monkeypatch.setenv("FAKE_TF_WORKSPACE_ERROR", str(error))
    with _as_role(APPLY_ROLE):
        applied = runner.invoke(cli.app, ["apply"])
        events = _events()
    assert planned.exit_code == 0, planned.output
    assert applied.exit_code == 1, applied.output
    # Terraform did get it, and printed it.
    assert SENTINEL in (tmp_path / "terraform-env").read_text()
    assert "provider debug: token request for ***" in planned.output
    artifacts = dbx / ".jwall-engineering-sites"
    texts = {
        "plan console": planned.output,
        "apply console": applied.output,
        "summary": (tmp_path / "summary.md").read_text(),
        "events": json.dumps(events),
        **{p.name: p.read_text() for p in artifacts.rglob("*") if p.is_file()},
        **{p.name: p.read_text() for p in (tmp_path / "tfvars").iterdir()},
    }
    assert "jwall-engineering-sites.log" in texts and "plan-comment.md" in texts
    leaks = [name for name, text in texts.items() if SENTINEL in text]
    assert leaks == []
    assert "invalid_client" in texts["failure.md"]  # the error itself survives, masked


def test_redaction_filter_is_removed_afterwards(dbx: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with _as_role(PLAN_ROLE):
        runner.invoke(cli.app, ["plan"])
    assert logging.getLogger("jwall-engineering-sites").filters == []


# -- destroy --------------------------------------------------------------------


def _catalog_bucket_with_versions() -> str:
    bucket = derived_names(_project()).catalog_bucket
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    for key in ("managed/a.parquet", "managed/b.parquet"):
        s3.put_object(Bucket=bucket, Key=key, Body=b"1")
        s3.put_object(Bucket=bucket, Key=key, Body=b"2")
    s3.delete_object(Bucket=bucket, Key="managed/a.parquet")  # a delete marker
    return bucket


def _versions(bucket: str) -> int:
    page = boto3.client("s3", region_name="us-east-1").list_object_versions(Bucket=bucket)
    return len(page.get("Versions", [])) + len(page.get("DeleteMarkers", []))


def test_destroy_prints_the_deletion_list_and_requires_yes(dbx: Path, tmp_path: Path) -> None:
    names = derived_names(_project())
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy"])
    assert result.exit_code == 3, result.output
    assert "--yes" in result.output
    for item in (names.catalog_bucket, names.root_bucket, "VPC", "NAT", "workspace", "catalog"):
        assert item in result.output
    assert _stack_calls(tmp_path) == []


def test_destroy_order_and_bucket_emptied(dbx: Path, tmp_path: Path) -> None:
    bucket = _catalog_bucket_with_versions()
    assert _versions(bucket) == 5
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy", "--yes"])
        events = _events()
    assert result.exit_code == 0, result.output
    assert bucket in result.output and "NAT" in result.output  # the list, printed again
    assert _versions(bucket) == 0
    steps = _steps(events)
    assert steps == _ok("empty-catalog-bucket", "workspace-destroy", "account-destroy")
    assert {e["event"] for e in events} == {"destroy"}
    calls = _stack_calls(tmp_path)
    assert calls.index("workspace destroy") < calls.index("account destroy")
    tfvars = json.loads((tmp_path / "tfvars" / "WORKSPACE.json").read_text())
    assert tfvars["workspace_url"] == ACCOUNT_OUTPUTS["workspace_url"]
    # Destroy evaluates the providers, so the account stack gets its real inputs too.
    assert json.loads((tmp_path / "tfvars" / "ACCOUNT.json").read_text())["vpc_cidr"]
    assert "5 object versions" in result.output


def test_destroy_rerun_after_partial(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bucket = _catalog_bucket_with_versions()
    monkeypatch.setenv("FAKE_TF_ACCOUNT_FAIL_ON", "destroy")
    monkeypatch.setenv("FAKE_TF_ACCOUNT_ERROR", str(FIXTURES / "errors" / "DBX-AUTH.txt"))
    with _as_role(APPLY_ROLE):
        first = runner.invoke(cli.app, ["destroy", "--yes"])
        events = _events()
    assert first.exit_code == 1, first.output
    assert [(e["stage"], e["error_class"]) for e in events if e["result"] == "failure"] == [
        ("account-destroy", "DBX-AUTH")
    ]
    assert "DBX-AUTH" in (tmp_path / "summary.md").read_text()
    assert "re-running destroy" in first.output.lower()

    # The workspace stack went (and took the bucket); the account stack lost its workspace.
    boto3.client("s3", region_name="us-east-1").delete_bucket(Bucket=bucket)
    monkeypatch.delenv("FAKE_TF_ACCOUNT_FAIL_ON")
    monkeypatch.setenv("FAKE_TF_ACCOUNT_OUTPUTS", str(_outputs_file(tmp_path / "none.json", {})))
    _reset_calls(tmp_path)
    with _as_role(APPLY_ROLE):
        second = runner.invoke(cli.app, ["destroy", "--yes"])
        rerun = [e for e in _events() if e["run_id"] != events[0]["run_id"]]
    assert second.exit_code == 0, second.output
    calls = _stack_calls(tmp_path)
    assert "workspace destroy" not in calls and "workspace state" in calls
    assert "account destroy" in calls
    assert _steps(rerun) == [
        ("empty-catalog-bucket", "started"),
        ("empty-catalog-bucket", "skipped"),
        ("workspace-destroy", "started"),
        ("workspace-destroy", "skipped"),
        ("account-destroy", "started"),
        ("account-destroy", "success"),
    ]


def test_destroy_refuses_a_workspace_stack_it_cannot_reach(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resources left in the workspace stack, but no workspace URL to destroy them with."""
    monkeypatch.setenv("FAKE_TF_ACCOUNT_OUTPUTS", str(_outputs_file(tmp_path / "none.json", {})))
    monkeypatch.setenv("FAKE_TF_WORKSPACE_STATE_LIST", "module.catalog.databricks_catalog.this")
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy", "--yes"])
    assert result.exit_code == 1, result.output
    assert "account destroy" not in _stack_calls(tmp_path)
    assert "workspace_url" in result.output


class _VersionsStub:
    def __init__(self, count: int) -> None:
        self.count = count
        self.batches: list[int] = []

    def get_paginator(self, name: str) -> Any:
        assert name == "list_object_versions"
        stub = self

        class Paginator:
            def paginate(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
                versions = [{"Key": f"k{i}", "VersionId": str(i)} for i in range(stub.count)]
                for start in range(0, len(versions), 1000):
                    yield {"Versions": versions[start : start + 1000]}

        return Paginator()

    def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self.batches.append(len(Delete["Objects"]))
        assert Delete["Quiet"] is True
        return {}


def test_empty_bucket_deletes_in_batches_of_1000(logger: logging.Logger) -> None:
    stub = _VersionsStub(2500)
    assert databricks_destroy.empty_bucket(stub, "b", logger) == 2500
    assert stub.batches == [1000, 1000, 500]


def test_empty_bucket_reports_per_object_errors(logger: logging.Logger) -> None:
    class Failing(_VersionsStub):
        def delete_objects(self, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
            return {"Errors": [{"Key": "k1", "Code": "AccessDenied", "Message": "no"}]}

    from jwall_engineering_sites.errors import RunError

    with pytest.raises(RunError, match="AccessDenied"):
        databricks_destroy.empty_bucket(Failing(3), "b", logger)


# -- validate, outputs, enabled: false -------------------------------------------


def _disable(root: Path) -> None:
    path = root / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["databricks"]["enabled"] = False
    path.write_text(yaml.safe_dump(config))


def test_validate_works_offline_on_both_stacks(
    dbx: Path, loaded: MagicMock, tmp_path: Path
) -> None:
    _disable(dbx)
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 0, result.output
    loaded.assert_called()
    assert _stack_calls(tmp_path) == [
        "account init",
        "account validate",
        "workspace init",
        "workspace validate",
    ]
    log = (tmp_path / "stacks.log").read_text()
    assert log.count("-backend=false") == 2
    assert "DBX-TST-001" in result.output  # the config checks ran
    assert _events() == []  # offline: nothing stored


def test_validate_fails_on_a_config_check(dbx: Path, tmp_path: Path) -> None:
    framework.check("DBX-TST-011", "bad config", Stage.CONFIG, frozenset({"databricks"}))(
        lambda ctx: [
            CheckResult("DBX-TST-011", "bad config", Outcome.FAIL, Severity.ERROR, {}, None, None)
        ]
    )
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 3, result.output
    assert _stack_calls(tmp_path) == []


@pytest.mark.parametrize("command", [["plan"], ["apply"], ["destroy", "--yes"]])
def test_disabled_account_refuses_live_commands(
    dbx: Path, tmp_path: Path, command: list[str]
) -> None:
    _disable(dbx)
    role = PLAN_ROLE if command == ["plan"] else APPLY_ROLE
    with _as_role(role):
        result = runner.invoke(cli.app, command)
    assert result.exit_code == 3, result.output
    assert "docs/databricks.md#one-time-setup" in result.output
    assert _stack_calls(tmp_path) == []


def test_outputs_prints_both_stacks(dbx: Path, tmp_path: Path) -> None:
    with _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["outputs"])
    assert result.exit_code == 0, result.output
    start = result.output.index("{")
    printed = json.loads(result.output[start : result.output.rindex("}") + 1])
    assert printed["workspace_url"] == ACCOUNT_OUTPUTS["workspace_url"]
    assert printed["workspace_id"] == 42
    assert printed["catalog_name"] == "databricks_poc"
    assert _stack_calls(tmp_path) == [
        "account init",
        "account output",
        "workspace init",
        "workspace output",
    ]


def test_outputs_before_any_apply(
    dbx: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_TF_ACCOUNT_OUTPUTS", str(_outputs_file(tmp_path / "none.json", {})))
    with _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["outputs"])
    assert result.exit_code == 0, result.output
    assert not any(c.startswith("workspace") for c in _stack_calls(tmp_path))


def test_plan_refuses_the_apply_role(dbx: Path, tmp_path: Path) -> None:
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["plan"])
    assert result.exit_code == 3, result.output
    assert _stack_calls(tmp_path) == []


# -- R-I5: the bot and task commands refuse a Databricks project -------------------


@pytest.mark.parametrize(
    "command",
    [["task", "list"], ["task", "sweep"], ["bot", "status"], ["bot", "logs"]],
)
def test_bot_and_task_commands_refuse_databricks(dbx: Path, command: list[str]) -> None:
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, command)
    assert result.exit_code == 2, result.output
    assert "databricks project" in result.output
