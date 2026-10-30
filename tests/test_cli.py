from __future__ import annotations

import json
import shutil
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
    with mock_aws(), _as_role(BOT_APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy", "--yes"])
    assert result.exit_code == 0, result.output
    assert "destroy -input=false -no-color -auto-approve -lock-timeout=120s" in (
        fake_terraform.read_text()
    )
    assert "records bucket" in result.output and "secret" in result.output


def test_destroy_releases_the_records_bucket_from_state_first(
    bot_workspace: Path, fake_terraform: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prevent_destroy on the bucket would reject the whole destroy plan."""
    monkeypatch.setenv(
        "FAKE_TF_STATE_LIST",
        "module.records[0].aws_s3_bucket.this module.bot.aws_instance.this",
    )
    with mock_aws(), _as_role(BOT_APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy", "--yes"])
    assert result.exit_code == 0, result.output
    calls = [c for c in fake_terraform.read_text().splitlines() if not c.startswith("version")]
    rm = calls.index("state rm -lock-timeout=120s module.records")
    destroy = next(i for i, c in enumerate(calls) if c.startswith("destroy "))
    assert rm < destroy


def test_destroy_releases_the_task_results_bucket_from_state_first(
    bot_workspace: Path, fake_terraform: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task results bucket also has prevent_destroy; it expires its own contents."""
    monkeypatch.setenv(
        "FAKE_TF_STATE_LIST",
        "module.records[0].aws_s3_bucket.this module.task_results[0].aws_s3_bucket.this "
        "module.tasks[0].aws_launch_template.this",
    )
    with mock_aws(), _as_role(BOT_APPLY_ROLE):
        result = runner.invoke(cli.app, ["destroy", "--yes"])
    assert result.exit_code == 0, result.output
    calls = [c for c in fake_terraform.read_text().splitlines() if not c.startswith("version")]
    rm = calls.index("state rm -lock-timeout=120s module.task_results")
    destroy = next(i for i, c in enumerate(calls) if c.startswith("destroy "))
    assert rm < destroy
    assert "jwall-engineering-rusty-chess-tasks" in result.output


def test_destroy_refuses_while_task_instances_run(
    bot_workspace: Path, fake_terraform: Path
) -> None:
    with mock_aws(), _as_role(BOT_APPLY_ROLE):
        ec2 = boto3.client("ec2", region_name="us-east-1")
        image_id = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
        instance = ec2.run_instances(
            ImageId=image_id, MinCount=1, MaxCount=1, InstanceType="c8a.xlarge",
            TagSpecifications=[{"ResourceType": "instance", "Tags": [
                {"Key": "Project", "Value": "rusty-chess"}, {"Key": "Purpose", "Value": "ci-task"},
            ]}],
        )["Instances"][0]["InstanceId"]  # fmt: skip
        result = runner.invoke(cli.app, ["destroy", "--yes"])
    assert result.exit_code == 3
    assert instance in result.output and "task terminate --all" in result.output
    calls = fake_terraform.read_text().splitlines() if fake_terraform.exists() else []
    assert not any(c.startswith("destroy") for c in calls)


def test_destroy_refuses_while_images_remain(bot_workspace: Path, fake_terraform: Path) -> None:
    with mock_aws(), _as_role(BOT_APPLY_ROLE):
        _ecr_with("v0.14.0")
        result = runner.invoke(cli.app, ["destroy", "--yes"])
    assert result.exit_code == 3
    assert "batch-delete-image" in result.output
    calls = fake_terraform.read_text().splitlines() if fake_terraform.exists() else []
    assert not any(c.startswith("destroy") for c in calls)


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


# -- bot build / push / deploy / reconcile ---------------------------------------

import hashlib  # noqa: E402
import io  # noqa: E402
import tarfile  # noqa: E402

from jwall_engineering_sites import image as image_module  # noqa: E402
from jwall_engineering_sites import releases as releases_module  # noqa: E402
from jwall_engineering_sites import rollout as rollout_module  # noqa: E402

REPO = "jwall-engineering-rusty-chess"
API_RELEASES = "https://api.github.com/repos/jwallace145/rusty-chess-v2/releases?per_page=100"
LINUX = "rusty-chess-{v}-x86_64-unknown-linux-gnu.tar.gz"


def _tarball(version: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"\x7fELF fake engine"
        info = tarfile.TarInfo(f"rusty-chess-{version}-x86_64-unknown-linux-gnu/rusty-chess")
        info.size, info.mode = len(data), 0o755
        tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
def fake_github(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """Releases v0.13.0 and v0.14.0 with their Linux tarball and checksum."""
    files: dict[str, bytes] = {}
    listing = []
    for version in ("v0.14.0", "v0.13.0"):
        name = LINUX.format(v=version)
        tarball = _tarball(version)
        files[f"https://dl/{name}"] = tarball
        files[f"https://dl/{name}.sha256"] = (
            f"{hashlib.sha256(tarball).hexdigest()}  {name}\n".encode()
        )
        listing.append(
            {
                "tag_name": version,
                "draft": False,
                "prerelease": False,
                "assets": [
                    {"name": name, "browser_download_url": f"https://dl/{name}"},
                    {"name": name + ".sha256", "browser_download_url": f"https://dl/{name}.sha256"},
                ],
            }
        )
    files[API_RELEASES] = json.dumps(listing).encode()
    for item in listing:
        files[
            f"https://api.github.com/repos/jwallace145/rusty-chess-v2/releases/tags/{item['tag_name']}"
        ] = json.dumps(item).encode()

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        return files[url]

    monkeypatch.setattr(releases_module, "_http_get", fetch)
    return files


def _ecr_with(*tags: str) -> None:
    ecr = boto3.client("ecr", region_name="us-east-1")
    ecr.create_repository(repositoryName=REPO)
    for tag in tags:
        ecr.put_image(
            repositoryName=REPO,
            imageManifest=json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "config": {"digest": f"sha256:{tag}", "size": 1, "mediaType": "x"},
                    "layers": [],
                }
            ),
            imageTag=tag,
        )


def _bot_instance() -> str:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    image_id = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    instance = ec2.run_instances(
        ImageId=image_id,
        MinCount=1,
        MaxCount=1,
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Project", "Value": "rusty-chess"},
                    {"Key": "Component", "Value": "container-instance"},
                ],
            }
        ],
    )["Instances"][0]
    instance_id: str = instance["InstanceId"]
    return instance_id


def _param(name: str) -> str | None:
    try:
        value: str = boto3.client("ssm", region_name="us-east-1").get_parameter(
            Name=f"/jwall-engineering/rusty-chess/bot/{name}"
        )["Parameter"]["Value"]
    except Exception:
        return None
    return value


def _put_param(name: str, value: str) -> None:
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name=f"/jwall-engineering/rusty-chess/bot/{name}",
        Value=value,
        Type="String",
        Overwrite=True,
    )


def test_bot_build_verifies_and_smoke_tests(
    bot_workspace: Path, fake_github: dict[str, bytes], fake_docker: Path
) -> None:
    result = runner.invoke(cli.app, ["bot", "build", "--version", "v0.14.0"])
    assert result.exit_code == 0, result.output
    record = json.loads((bot_workspace / ".jwall-engineering-sites" / "build.json").read_text())
    assert record["tag"] == "v0.14.0"
    assert record["image"] == f"{REPO}:v0.14.0"
    assert record["nodes"] == "123456"
    assert len(record["sha256"]) == 64
    verbs = [line.split()[0] for line in fake_docker.read_text().splitlines()]
    assert verbs == ["build", "run"]


def test_bot_build_rejects_bad_checksum(
    bot_workspace: Path, fake_github: dict[str, bytes], fake_docker: Path
) -> None:
    name = LINUX.format(v="v0.14.0")
    fake_github[f"https://dl/{name}.sha256"] = f"{'0' * 64}  {name}\n".encode()
    result = runner.invoke(cli.app, ["bot", "build", "--version", "v0.14.0"])
    assert result.exit_code == 1
    assert "checksum mismatch" in result.output
    assert fake_docker.read_text() == ""


def test_bot_build_rejects_a_bad_version(bot_workspace: Path) -> None:
    result = runner.invoke(cli.app, ["bot", "build", "--version", "latest"])
    assert result.exit_code == 2


def test_bot_build_from_source_is_reserved(bot_workspace: Path) -> None:
    result = runner.invoke(cli.app, ["bot", "build", "--version", "v0.14.0", "--from-source"])
    assert result.exit_code == 3
    assert "not implemented" in result.output


def test_bot_push_refuses_existing_tag(bot_workspace: Path, fake_docker: Path) -> None:
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        _ecr_with("v0.14.0")
        result = runner.invoke(cli.app, ["bot", "push", "--version", "v0.14.0"])
    assert result.exit_code == 3
    assert "immutable" in result.output
    assert fake_docker.read_text() == ""


def test_bot_push_pushes_a_new_tag(bot_workspace: Path, fake_docker: Path) -> None:
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        _ecr_with()
        result = runner.invoke(cli.app, ["bot", "push", "--version", "v0.14.0"])
    assert result.exit_code == 0, result.output
    verbs = [line.split()[0] for line in fake_docker.read_text().splitlines()]
    assert verbs == ["login", "tag", "push"]


def test_bot_push_needs_the_release_role(bot_workspace: Path, fake_docker: Path) -> None:
    with mock_aws(), _as_role(BOT_APPLY_ROLE):
        _ecr_with()
        result = runner.invoke(cli.app, ["bot", "push", "--version", "v0.14.0"])
    assert result.exit_code == 3


def test_bot_deploy_without_instance_exits_3(bot_workspace: Path) -> None:
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        _ecr_with("v0.14.0")
        result = runner.invoke(cli.app, ["bot", "deploy", "--version", "v0.14.0"])
    assert result.exit_code == 3
    assert "tag:Project=rusty-chess" in result.output


def test_bot_deploy_refuses_a_tag_not_in_ecr(bot_workspace: Path) -> None:
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        _ecr_with()
        _bot_instance()
        result = runner.invoke(cli.app, ["bot", "deploy", "--version", "v0.14.0"])
    assert result.exit_code == 3
    assert "push it first" in result.output


def test_bot_deploy_success(bot_workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, bool]] = []
    seen: dict[str, str | None] = {}

    def fake_send(ssm: object, instance_id: str, restart: bool = False) -> str:
        sent.append((instance_id, restart))
        seen["desired"], seen["status"] = _param("desired-version"), _param("status")
        return "cmd-1"

    def fake_follow(*args: object, **kwargs: object) -> str:
        _put_param("running-version", "v0.14.0")
        _put_param("status", "ok")
        return "ok"

    monkeypatch.setattr(rollout_module, "send_reconcile", fake_send)
    monkeypatch.setattr(rollout_module, "follow", fake_follow)
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        _ecr_with("v0.14.0")
        instance_id = _bot_instance()
        _put_param("running-version", "v0.13.0")
        result = runner.invoke(cli.app, ["bot", "deploy", "--version", "v0.14.0", "--restart"])
    assert result.exit_code == 0, result.output
    assert sent == [(instance_id, True)]
    assert seen == {"desired": "v0.14.0", "status": "pending:v0.14.0"}
    assert "v0.13.0" in result.output and "v0.14.0" in result.output


def test_bot_reconcile_dry_run_prints_plan(
    bot_workspace: Path, fake_github: dict[str, bytes]
) -> None:
    with mock_aws():
        _ecr_with("v0.13.0")
        _put_param("running-version", "v0.13.0")
        result = runner.invoke(cli.app, ["bot", "reconcile", "--dry-run"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.stdout.strip().splitlines()[-1])
    assert plan["version"] == "v0.14.0"
    assert (plan["build"], plan["push"], plan["deploy"]) == (True, True, True)


def test_reconcile_is_idempotent(
    bot_workspace: Path, fake_github: dict[str, bytes], fake_docker: Path
) -> None:
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        _ecr_with("v0.14.0")
        _put_param("running-version", "v0.14.0")
        result = runner.invoke(cli.app, ["bot", "reconcile", "--version", "v0.14.0"])
    assert result.exit_code == 0, result.output
    assert "already running" in result.output
    assert fake_docker.read_text() == ""


def test_reconcile_builds_pushes_and_deploys_the_newest(
    bot_workspace: Path,
    fake_github: dict[str, bytes],
    fake_docker: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rollout_module, "send_reconcile", lambda *a, **k: "cmd-1")

    def fake_follow(*args: object, **kwargs: object) -> str:
        _put_param("running-version", "v0.14.0")
        _put_param("status", "ok")
        return "ok"

    monkeypatch.setattr(rollout_module, "follow", fake_follow)
    real_push = image_module.push

    def push_and_register(project: object, registry: str, tag: str, *a: object) -> str:
        remote: str = real_push(project, registry, tag, *a)  # type: ignore[arg-type]
        boto3.client("ecr", region_name="us-east-1").put_image(
            repositoryName=REPO,
            imageManifest=json.dumps(
                {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
                    "config": {"digest": f"sha256:{tag}", "size": 1, "mediaType": "x"},
                    "layers": [],
                }
            ),
            imageTag=tag,
        )
        return remote

    monkeypatch.setattr(image_module, "push", push_and_register)
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        _ecr_with("v0.13.0")
        _bot_instance()
        _put_param("running-version", "v0.13.0")
        result = runner.invoke(cli.app, ["bot", "reconcile"])
        running = _param("running-version")
    assert result.exit_code == 0, result.output
    verbs = [line.split()[0] for line in fake_docker.read_text().splitlines()]
    assert verbs == ["build", "run", "login", "tag", "push"]
    assert running == "v0.14.0"


def test_bot_deploy_refuses_a_booting_instance(
    bot_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jwall_engineering_sites import bot as bot_module

    monkeypatch.setattr(
        bot_module,
        "find_instance",
        lambda *a, **k: bot_module.InstanceInfo(id="i-boot", state="pending", public_ip=None),
    )
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        _ecr_with("v0.14.0")
        result = runner.invoke(cli.app, ["bot", "deploy", "--version", "v0.14.0"])
        desired = _param("desired-version")
    assert result.exit_code == 3
    assert "i-boot is pending" in result.output
    assert desired is None, "nothing is written before the instance can take the command"


def test_bot_logs_refuses_the_release_role(bot_workspace: Path) -> None:
    """Only the plan role may read the logs (container/read); the release role ships images."""
    with mock_aws(), _as_role(BOT_RELEASE_ROLE):
        result = runner.invoke(cli.app, ["bot", "logs"])
    assert result.exit_code == 3


def test_bot_build_reads_the_config_next_to_the_project_file(
    bot_workspace: Path,
    fake_github: dict[str, bytes],
    fake_docker: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--file` from another directory: lichess/config.yml is resolved beside the file."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.copy(bot_workspace / "config.yaml", elsewhere / "config.yaml")
    monkeypatch.chdir(elsewhere)
    result = runner.invoke(
        cli.app,
        [
            "--file",
            str(bot_workspace / "jwall-engineering-sites.yaml"),
            "bot",
            "build",
            "--version",
            "v0.14.0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert [line.split()[0] for line in fake_docker.read_text().splitlines()] == ["build", "run"]


def test_plan_bot_comment_names_bot_apply(bot_workspace: Path, fake_terraform: Path) -> None:
    with _as_role(BOT_PLAN_ROLE):
        result = runner.invoke(cli.app, ["plan"])
    assert result.exit_code == 0, result.output
    comment = (bot_workspace / ".jwall-engineering-sites" / "plan-comment.md").read_text()
    assert "Merging will not touch AWS" not in comment
    assert "**Bot apply**" in comment


def test_bot_build_waits_for_assets_attached_after_publishing(
    bot_workspace: Path,
    fake_github: dict[str, bytes],
    fake_docker: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Release Please publishes the release; the assets arrive seconds later."""
    tag_url = "https://api.github.com/repos/jwallace145/rusty-chess-v2/releases/tags/v0.14.0"
    full = next(r for r in json.loads(fake_github[API_RELEASES]) if r["tag_name"] == "v0.14.0")
    bare = dict(full, assets=[])
    answers = [json.dumps(bare).encode(), json.dumps(full).encode()]
    original = releases_module._http_get

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        if url == tag_url:
            return answers.pop(0) if len(answers) > 1 else answers[0]
        return original(url, headers)

    sleeps: list[float] = []
    monkeypatch.setattr(releases_module, "_http_get", fetch)
    import time

    monkeypatch.setattr(time, "sleep", sleeps.append)
    result = runner.invoke(cli.app, ["bot", "build", "--version", "v0.14.0"])
    assert result.exit_code == 0, result.output
    assert sleeps, "the build waited for the assets"
    assert "waiting for" in result.output
