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
    assert (
        (artifacts / "plan-comment.md")
        .read_text()
        .startswith("### Terraform plan: changes pending")
    )
    assert "result=changes\nchanges=true\n" in github_output.read_text()


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
        assert head["CacheControl"] == "public, max-age=300"
        comment = (workspace / ".jwall-engineering-sites" / "preview-comment.md").read_text()
        assert comment.startswith("**https://pr-12.preview.guitar.jwall.engineering/**")
        assert (
            "preview_url=https://pr-12.preview.guitar.jwall.engineering/"
            in github_output.read_text()
        )

        result = runner.invoke(cli.app, ["preview", "remove", "--pr", "12"])
        assert result.exit_code == 0, result.output
        assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET, Prefix="previews/pr-12/")
        assert (
            "Removed" in (workspace / ".jwall-engineering-sites" / "preview-comment.md").read_text()
        )


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
