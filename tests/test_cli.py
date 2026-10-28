from __future__ import annotations

import json
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from jwall_engineering_sites import cli
from jwall_engineering_sites.stack import PLAN_CHANGES

runner = CliRunner()
PLAN_ROLE = (
    "arn:aws:sts::650503560686:assumed-role/jwall-engineering-guitar-knowledge-base-ci-plan/s"
)
PREVIEW_ROLE = (
    "arn:aws:sts::650503560686:assumed-role/jwall-engineering-guitar-knowledge-base-ci-preview/s"
)
APPLY_ROLE = (
    "arn:aws:sts::650503560686:assumed-role/jwall-engineering-guitar-knowledge-base-ci-apply/s"
)
BUCKET = "jwall-engineering-guitar-knowledge-base"
BOT_PLAN_ROLE = "arn:aws:sts::650503560686:assumed-role/jwall-engineering-rusty-chess-ci-plan/s"
BOT_APPLY_ROLE = "arn:aws:sts::650503560686:assumed-role/jwall-engineering-rusty-chess-ci-apply/s"


def _as_role(arn: str) -> AbstractContextManager[Any]:
    return patch("jwall_engineering_sites.aws.caller_identity", return_value=("650503560686", arn))


def test_config_show(workspace: Path) -> None:
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["ecr"]["repository"] == "jwall-engineering-sites"


def test_validate_runs_terraform_offline(workspace: Path, fake_terraform: Path) -> None:
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 0, result.output
    calls = [c for c in fake_terraform.read_text().splitlines() if not c.startswith("version")]
    assert calls == ["init -input=false -no-color -backend=false", "validate -no-color"]
    assert "stack validates" in result.output
    assert (workspace / ".jwall-engineering-sites" / "jwall-engineering-sites.log").is_file()


def test_invalid_site_file_exits_2(workspace: Path, fake_terraform: Path) -> None:
    (workspace / "jwall-engineering-sites.yaml").write_text("version: 1\nproject: x\n")
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 2
    assert "jwall-engineering-sites.yaml" in result.output


def test_missing_config_exits_2(workspace: Path) -> None:
    result = runner.invoke(cli.app, ["--config", "nope.yaml", "config", "show"])
    assert result.exit_code == 2


def test_plan_writes_comment_and_outputs(
    workspace: Path, fake_terraform: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FAKE_TF_PLAN_EXIT", str(PLAN_CHANGES))
    monkeypatch.setenv("FAKE_TF_PLAN_SUMMARY", "1 to add, 0 to change, 0 to destroy.")
    github_output = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    with mock_aws(), _as_role(PLAN_ROLE):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        result = runner.invoke(cli.app, ["plan"])
    assert result.exit_code == 0, result.output
    artifacts = workspace / ".jwall-engineering-sites"
    assert "Plan: 1 to add" in (artifacts / "plan.txt").read_text()
    comment = (artifacts / "plan-comment.md").read_text()
    assert "JWall Engineering Sites" in comment
    assert "> [!WARNING]" in comment and "Changes pending" in comment
    assert "`guitar.jwall.engineering`" in comment
    assert "result=changes\nchanges=true\n" in github_output.read_text()


def test_report_plan_comment_renders_a_plan_log(workspace: Path, tmp_path: Path) -> None:
    plan_log = tmp_path / "plan.log"
    plan_log.write_text("No changes. Your infrastructure matches the configuration.\n")
    out = tmp_path / "plan-comment.md"
    result = runner.invoke(
        cli.app,
        [
            "report",
            "plan-comment",
            "--exit-code",
            "0",
            "--plan",
            str(plan_log),
            "--subject",
            "the foundation stack",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    comment = out.read_text()
    assert "JWall Engineering Sites" in comment
    assert "> [!NOTE]" in comment and "No changes" in comment
    assert "the foundation stack" in comment


def test_plan_refuses_the_wrong_role(workspace: Path, fake_terraform: Path) -> None:
    with mock_aws(), _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["plan"])
    assert result.exit_code == 3
    assert "must run as the jwall-engineering-guitar-knowledge-base-ci-plan role" in result.output
    assert not fake_terraform.exists(), "terraform must not run after a failed preflight"


def test_preview_deploy_and_remove(
    workspace: Path, dist: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    github_output = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    with mock_aws(), _as_role(PREVIEW_ROLE):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        result = runner.invoke(cli.app, ["preview", "deploy", "--pr", "12", "--dist", str(dist)])
        assert result.exit_code == 0, result.output
        keys = sorted(
            o["Key"]
            for o in s3.list_objects_v2(Bucket=BUCKET, Prefix="previews/pr-12/")["Contents"]
        )
        assert "previews/pr-12/index.html" in keys
        head = s3.head_object(Bucket=BUCKET, Key="previews/pr-12/index.html")
        assert head["CacheControl"] == "public, max-age=0, must-revalidate"
        asset = s3.head_object(Bucket=BUCKET, Key="previews/pr-12/robots.txt")
        assert asset["CacheControl"] == "public, max-age=300"
        comment = (workspace / ".jwall-engineering-sites" / "preview-comment.md").read_text()
        assert "JWall Engineering Sites" in comment
        assert "**https://pr-12.preview.guitar.jwall.engineering/**" in comment
        assert "#12" in comment
        assert (
            "preview_url=https://pr-12.preview.guitar.jwall.engineering/"
            in github_output.read_text()
        )

        result = runner.invoke(cli.app, ["preview", "remove", "--pr", "12"])
        assert result.exit_code == 0, result.output
        assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix="previews/pr-12/")
        removed = (workspace / ".jwall-engineering-sites" / "preview-comment.md").read_text()
        assert "JWall Engineering Sites" in removed
        assert "removed" in removed.lower() and "#12" in removed


def test_preview_deploy_needs_a_built_site(workspace: Path, tmp_path: Path) -> None:
    with mock_aws(), _as_role(PREVIEW_ROLE):
        result = runner.invoke(
            cli.app, ["preview", "deploy", "--pr", "1", "--dist", str(tmp_path / "nope")]
        )
    assert result.exit_code == 3
    assert "built site not found" in result.output


def test_release_applies_then_publishes(workspace: Path, dist: Path, fake_terraform: Path) -> None:
    with (
        mock_aws(),
        _as_role(APPLY_ROLE),
        patch(
            "jwall_engineering_sites.aws.invalidate_distribution", return_value="I123"
        ) as invalidate,
    ):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        result = runner.invoke(cli.app, ["release", "--dist", str(dist)])
        assert result.exit_code == 0, result.output
        assert (
            s3.head_object(Bucket=BUCKET, Key="prod/index.html")["CacheControl"]
            == "public, max-age=0, must-revalidate"
        )
        assert (
            s3.head_object(Bucket=BUCKET, Key="prod/robots.txt")["CacheControl"]
            == "public, max-age=3600"
        )
    calls = fake_terraform.read_text().splitlines()
    assert [c.split()[0] for c in calls] == ["version", "init", "plan", "apply", "output"]
    assert invalidate.call_args.args[1] == "E1PROD"
    outputs = json.loads((workspace / ".jwall-engineering-sites" / "outputs.json").read_text())
    assert outputs["bucket_name"] == BUCKET


def test_publish_dev_without_dev_environment_fails_fast(
    workspace: Path, dist: Path, fake_terraform: Path
) -> None:
    with mock_aws(), _as_role(APPLY_ROLE):
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        result = runner.invoke(cli.app, ["publish", "--dist", str(dist), "--environment", "dev"])
    assert result.exit_code == 3
    assert "no dev distribution" in result.output


def test_outputs(workspace: Path, fake_terraform: Path) -> None:
    with mock_aws(), _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["outputs"])
    assert result.exit_code == 0, result.output
    payload = result.output[result.output.index("{") :]
    assert json.loads(payload)["prod_distribution_id"] == "E1PROD"


def test_unwritable_workspace_exits_2(workspace: Path) -> None:
    (workspace / ".jwall-engineering-sites").write_text("not a directory")
    result = runner.invoke(cli.app, ["config", "show"])
    assert result.exit_code == 2
    assert "must be writable" in result.output


# -- bot projects -----------------------------------------------------------------


def test_validate_bot_file(bot_workspace: Path, fake_terraform: Path, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, ["validate"])
    assert result.exit_code == 0, result.output
    assert "kind bot" in result.output
    tfvars = json.loads((tmp_path / "rendered.tfvars.json").read_text())
    assert tfvars["records_container_path"] == "/opt/lichess-bot/game_records"


def test_plan_bot_uses_the_bot_stack(
    bot_workspace: Path, fake_terraform: Path, tmp_path: Path
) -> None:
    with _as_role(BOT_PLAN_ROLE):
        result = runner.invoke(cli.app, ["plan"])
    assert result.exit_code == 0, result.output
    calls = fake_terraform.read_text()
    assert "init -input=false -no-color -backend-config=backend.hcl -reconfigure" in calls
    tfvars = json.loads((tmp_path / "rendered.tfvars.json").read_text())
    assert tfvars["project"] == "rusty-chess"
    assert "records_enabled" in tfvars
    comment = (bot_workspace / ".jwall-engineering-sites" / "plan-comment.md").read_text()
    assert "rusty-chess" in comment


def test_plan_bot_refuses_the_site_role(bot_workspace: Path, fake_terraform: Path) -> None:
    with _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["plan"])
    assert result.exit_code == 3


def test_destroy_requires_yes(bot_workspace: Path, fake_terraform: Path) -> None:
    with _as_role(BOT_APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy"])
    assert result.exit_code == 3
    assert "--yes" in result.output


def test_destroy_bot_calls_terraform_destroy(bot_workspace: Path, fake_terraform: Path) -> None:
    with _as_role(BOT_APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy", "--yes"])
    assert result.exit_code == 0, result.output
    assert "destroy -input=false -no-color -auto-approve -lock-timeout=120s" in (
        fake_terraform.read_text()
    )
    assert "records bucket" in result.output and "secret" in result.output


def test_destroy_refuses_sites(workspace: Path, fake_terraform: Path) -> None:
    with _as_role(APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy", "--yes"])
    assert result.exit_code == 3
    assert "not supported for sites" in result.output


BOT_RELEASE_ROLE = (
    "arn:aws:sts::650503560686:assumed-role/jwall-engineering-rusty-chess-ci-release/s"
)
TOKEN_PARAM = "/jwall-engineering/rusty-chess/lichess-token"


def test_secrets_set_prompts_hidden(bot_workspace: Path) -> None:
    with mock_aws():
        result = runner.invoke(
            cli.app, ["bot", "secrets", "set", "LICHESS_TOKEN"], input="lip_t0k\n"
        )
        assert result.exit_code == 0, result.output
        value = boto3.client("ssm", region_name="us-east-1").get_parameter(
            Name=TOKEN_PARAM, WithDecryption=True
        )["Parameter"]["Value"]
    assert value == "lip_t0k"
    assert "lip_t0k" not in result.output
    assert TOKEN_PARAM in result.output


def test_secrets_set_refuses_an_undeclared_name(bot_workspace: Path) -> None:
    with mock_aws():
        result = runner.invoke(cli.app, ["bot", "secrets", "set", "OTHER"], input="x\n")
    assert result.exit_code == 2
    assert "LICHESS_TOKEN" in result.output


def test_secrets_check_exit_codes(bot_workspace: Path) -> None:
    with mock_aws():
        missing = runner.invoke(cli.app, ["bot", "secrets", "check", "LICHESS_TOKEN"])
        boto3.client("ssm", region_name="us-east-1").put_parameter(
            Name=TOKEN_PARAM, Value="x", Type="SecureString"
        )
        present = runner.invoke(cli.app, ["bot", "secrets", "check", "LICHESS_TOKEN"])
    assert missing.exit_code == 3 and "not set" in missing.output
    assert present.exit_code == 0 and "set" in present.output
    assert "x\n" not in present.output


def test_bot_status_output(bot_workspace: Path) -> None:
    with mock_aws(), _as_role(BOT_PLAN_ROLE):
        boto3.client("ssm", region_name="us-east-1").put_parameter(
            Name="/jwall-engineering/rusty-chess/bot/status", Value="ok", Type="String"
        )
        result = runner.invoke(cli.app, ["bot", "status"])
    assert result.exit_code == 0, result.output
    assert "status:            ok" in result.output
    assert "instance:          none" in result.output


def test_bot_status_accepts_the_release_role(bot_workspace: Path) -> None:
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        result = runner.invoke(cli.app, ["bot", "status"])
    assert result.exit_code == 0, result.output


def test_bot_status_refuses_other_roles(bot_workspace: Path) -> None:
    with mock_aws(), _as_role(PLAN_ROLE):
        result = runner.invoke(cli.app, ["bot", "status"])
    assert result.exit_code == 3


def test_bot_logs_prints_events(bot_workspace: Path) -> None:
    with mock_aws(), _as_role(BOT_PLAN_ROLE):
        logs = boto3.client("logs", region_name="us-east-1")
        group = "/jwall-engineering/rusty-chess/bot"
        logs.create_log_group(logGroupName=group)
        logs.create_log_stream(logGroupName=group, logStreamName="app-v0.14.0")
        import time

        logs.put_log_events(
            logGroupName=group,
            logStreamName="app-v0.14.0",
            logEvents=[{"timestamp": int(time.time() * 1000), "message": "awaiting challenges"}],
        )
        result = runner.invoke(cli.app, ["bot", "logs", "--since", "10m"])
    assert result.exit_code == 0, result.output
    assert "awaiting challenges" in result.output
    assert "app-v0.14.0" in result.output
