from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from jwall_engineering_sites import cli, databricks_auth
from jwall_engineering_sites.checks import framework
from jwall_engineering_sites.checks.framework import (
    CheckContext,
    CheckResult,
    Outcome,
    Severity,
    Stage,
)

runner = CliRunner()
SENTINEL = "dapi-SENTINEL-s3cr3t-value"
PARAM = "/jwall-engineering/databricks/client-secret"
PLAN_ROLE = "arn:aws:sts::650503560686:assumed-role/jwall-engineering-databricks-poc-ci-plan/s"
APPLY_ROLE = "arn:aws:sts::650503560686:assumed-role/jwall-engineering-databricks-poc-ci-apply/s"
OWNER = "arn:aws:sts::650503560686:assumed-role/owner/s"


def _as_role(arn: str) -> AbstractContextManager[Any]:
    return patch("jwall_engineering_sites.aws.caller_identity", return_value=("650503560686", arn))


def _put_secret(value: str = SENTINEL) -> None:
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name=PARAM, Value=value, Type="SecureString"
    )


#: The account outputs each stage's checks saw, by stage.
seen: dict[Stage, Any] = {}


class FakeClients:
    instances: ClassVar[list[FakeClients]] = []

    def __init__(self, settings: Any, secret: str, account_outputs: Any = None) -> None:
        self.secret = secret
        self.account_outputs = account_outputs
        FakeClients.instances.append(self)

    @property
    def account(self) -> Any:
        raise AssertionError("not used")

    def workspace(self, host: str) -> Any:
        raise AssertionError("not used")


@pytest.fixture
def recorded() -> Iterator[list[tuple[Stage, CheckContext]]]:
    seen.clear()
    """Replace the built-in checks with one recording check per stage."""
    calls: list[tuple[Stage, CheckContext]] = []
    FakeClients.instances = []
    with framework.isolated_registry():
        for number, stage in enumerate(Stage, start=1):

            def make(stage: Stage, number: int) -> framework.CheckFn:
                def fn(ctx: CheckContext) -> Iterable[CheckResult]:
                    calls.append((stage, ctx))
                    seen[stage] = None if ctx.databricks is None else ctx.databricks.account_outputs
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
            patch("jwall_engineering_sites.checks.load_builtin_checks"),
            patch.object(databricks_auth, "DatabricksClients", FakeClients),
        ):
            yield calls


def _account_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "account-outputs.json"
    out.write_text(json.dumps({"workspace_id": {"value": 42}, "workspace_url": {"value": "u"}}))
    monkeypatch.setenv("FAKE_TF_OUTPUTS", str(out))


def test_doctor_runs_all_read_only_stages(
    databricks_workspace: Path,
    fake_terraform: Path,
    recorded: list[tuple[Stage, CheckContext]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _account_outputs(tmp_path, monkeypatch)
    with mock_aws(), _as_role(PLAN_ROLE):
        _put_secret()
        result = runner.invoke(cli.app, ["databricks", "doctor"])
    assert result.exit_code == 0, result.output
    assert [s for s, _ in recorded] == [Stage.CONFIG, Stage.LIVE, Stage.BETWEEN, Stage.AFTER]
    config_ctx, live_ctx, between_ctx, _ = (c for _, c in recorded)
    assert config_ctx.session is None and config_ctx.databricks is None
    assert live_ctx.session is not None and live_ctx.databricks is not None
    assert between_ctx.databricks is not None
    assert seen[Stage.LIVE] is None
    assert seen[Stage.BETWEEN] == {"workspace_id": 42, "workspace_url": "u"}
    assert seen[Stage.AFTER] == seen[Stage.BETWEEN]
    assert FakeClients.instances[0].secret == SENTINEL
    assert "DBX-TST-001" in result.output and "DBX-TST-004" in result.output
    written = json.loads(
        (databricks_workspace / ".jwall-engineering-sites" / "checks.json").read_text()
    )
    assert [r["check_id"] for r in written] == [f"DBX-TST-00{n}" for n in (1, 2, 3, 4)]
    # Read-only: init and output of the account stack, nothing else.
    calls = [c for c in fake_terraform.read_text().splitlines() if not c.startswith("version")]
    assert [c.split()[0] for c in calls] == ["init", "output"]
    assert SENTINEL not in result.output


def test_doctor_stage_option_selects_stages(
    databricks_workspace: Path,
    fake_terraform: Path,
    recorded: list[tuple[Stage, CheckContext]],
) -> None:
    with mock_aws(), _as_role(PLAN_ROLE):
        _put_secret()
        result = runner.invoke(
            cli.app, ["databricks", "doctor", "--stage", "config", "--stage", "live"]
        )
    assert result.exit_code == 0, result.output
    assert [s for s, _ in recorded] == [Stage.CONFIG, Stage.LIVE]
    assert not fake_terraform.exists()  # no terraform at all without a between/after stage


def test_doctor_skips_between_and_after_without_account_state(
    databricks_workspace: Path,
    fake_terraform: Path,
    recorded: list[tuple[Stage, CheckContext]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty.json"
    empty.write_text("{}")
    monkeypatch.setenv("FAKE_TF_OUTPUTS", str(empty))
    with mock_aws(), _as_role(PLAN_ROLE):
        _put_secret()
        result = runner.invoke(cli.app, ["databricks", "doctor"])
    assert result.exit_code == 0, result.output
    assert [s for s, _ in recorded] == [Stage.CONFIG, Stage.LIVE]
    written = json.loads(
        (databricks_workspace / ".jwall-engineering-sites" / "checks.json").read_text()
    )
    skipped = [r for r in written if r["outcome"] == "skip"]
    assert [r["check_id"] for r in skipped] == ["DBX-TST-003", "DBX-TST-004"]
    assert "apply the account stack first" in result.output


def test_doctor_exit_3_on_error(
    databricks_workspace: Path,
    fake_terraform: Path,
    recorded: list[tuple[Stage, CheckContext]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _account_outputs(tmp_path, monkeypatch)
    framework.check("DBX-TST-009", "always fails", Stage.LIVE, frozenset({"databricks"}))(
        lambda ctx: [
            CheckResult(
                "DBX-TST-009", "always fails", Outcome.FAIL, Severity.ERROR, {}, "fix it", None
            )
        ]
    )
    framework.check(
        "DBX-TST-008", "only warns", Stage.LIVE, frozenset({"databricks"}), Severity.WARNING
    )(
        lambda ctx: [
            CheckResult("DBX-TST-008", "only warns", Outcome.FAIL, Severity.WARNING, {}, None, None)
        ]
    )
    with mock_aws(), _as_role(PLAN_ROLE):
        _put_secret()
        result = runner.invoke(cli.app, ["databricks", "doctor"])
    assert result.exit_code == 3, result.output
    assert "DBX-TST-009" in result.output and "fix it" in result.output
    assert (databricks_workspace / ".jwall-engineering-sites" / "checks.json").is_file()


def test_doctor_warnings_alone_do_not_fail(
    databricks_workspace: Path,
    fake_terraform: Path,
    recorded: list[tuple[Stage, CheckContext]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _account_outputs(tmp_path, monkeypatch)
    framework.check(
        "DBX-TST-008", "only warns", Stage.LIVE, frozenset({"databricks"}), Severity.WARNING
    )(
        lambda ctx: [
            CheckResult("DBX-TST-008", "only warns", Outcome.FAIL, Severity.WARNING, {}, None, None)
        ]
    )
    with mock_aws(), _as_role(PLAN_ROLE):
        _put_secret()
        result = runner.invoke(cli.app, ["databricks", "doctor"])
    assert result.exit_code == 0, result.output


def test_doctor_refuses_the_wrong_role(
    databricks_workspace: Path, fake_terraform: Path, recorded: list[tuple[Stage, CheckContext]]
) -> None:
    with mock_aws(), _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["databricks", "doctor"])
    assert result.exit_code == 3
    assert "ci-plan" in result.output
    assert recorded == []


def test_doctor_refuses_non_databricks_projects(bot_workspace: Path, fake_terraform: Path) -> None:
    with mock_aws():
        result = runner.invoke(cli.app, ["databricks", "doctor"])
    assert result.exit_code == 2
    assert "databricks" in result.output


def test_doctor_missing_secret_exits_3_without_the_value(
    databricks_workspace: Path, fake_terraform: Path, recorded: list[tuple[Stage, CheckContext]]
) -> None:
    with mock_aws(), _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["databricks", "doctor", "--stage", "live"])
    assert result.exit_code == 3
    assert PARAM in result.output


def test_doctor_with_real_checks_skips_while_disabled(
    databricks_workspace: Path, fake_terraform: Path
) -> None:
    """The built-in live/between/after checks wait for `enabled: true`; no secret is needed."""
    import yaml

    path = databricks_workspace / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["databricks"]["enabled"] = False
    path.write_text(yaml.safe_dump(config))
    with mock_aws(), _as_role(PLAN_ROLE):
        result = runner.invoke(
            cli.app, ["databricks", "doctor", "--stage", "live", "--stage", "between"]
        )
    assert result.exit_code == 0, result.output
    assert "skip" in result.output.lower()


def test_credentials_set_no_echo(
    databricks_workspace: Path,
) -> None:
    with mock_aws(), _as_role(OWNER):
        result = runner.invoke(cli.app, ["databricks", "credentials", "set"], input=f"{SENTINEL}\n")
        assert result.exit_code == 0, result.output
        ssm = boto3.client("ssm", region_name="us-east-1")
        stored = ssm.get_parameter(Name=PARAM, WithDecryption=True)["Parameter"]
        assert stored["Value"] == SENTINEL
        assert stored["Type"] == "SecureString"
        # A second set overwrites.
        again = runner.invoke(
            cli.app, ["databricks", "credentials", "set"], input="rotated-value\n"
        )
        assert again.exit_code == 0, again.output
        assert ssm.get_parameter(Name=PARAM, WithDecryption=True)["Parameter"]["Value"] == (
            "rotated-value"
        )
    assert SENTINEL not in result.output
    log = databricks_workspace / ".jwall-engineering-sites" / "jwall-engineering-sites.log"
    assert SENTINEL not in log.read_text()
    assert PARAM in result.output


def test_credentials_set_refuses_an_empty_value(databricks_workspace: Path) -> None:
    with mock_aws(), _as_role(OWNER):
        result = runner.invoke(cli.app, ["databricks", "credentials", "set"], input="  \n")
        assert result.exit_code == 3
        ssm = boto3.client("ssm", region_name="us-east-1")
        assert ssm.describe_parameters()["Parameters"] == []


def test_credentials_check_hides_value(
    databricks_workspace: Path,
) -> None:
    with mock_aws(), _as_role(OWNER):
        absent = runner.invoke(cli.app, ["databricks", "credentials", "check"])
        _put_secret()
        present = runner.invoke(cli.app, ["databricks", "credentials", "check"])
    assert absent.exit_code == 3 and "not set" in absent.output
    assert present.exit_code == 0, present.output
    assert PARAM in present.output and "last modified" in present.output
    assert SENTINEL not in present.output
    log = databricks_workspace / ".jwall-engineering-sites" / "jwall-engineering-sites.log"
    assert SENTINEL not in log.read_text()
