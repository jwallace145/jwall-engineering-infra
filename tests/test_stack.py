from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jwall_engineering_sites.errors import RunError
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings
from jwall_engineering_sites.site import Site
from jwall_engineering_sites.stack import (
    PLAN_CHANGES,
    PLAN_NO_CHANGES,
    Terraform,
    default_stack_dir,
    render_backend,
    render_tfvars,
)
from tests.conftest import REPO_ROOT

#: What each site's infra/ directory produced before the migration, by project.
#: Step 6's "no changes" plan depends on these staying identical.
EXPECTED_TFVARS = {
    "guitar-knowledge-base": {
        "domain_name": "guitar.jwall.engineering",
        "dev_domain_name": None,
        "enable_preview": True,
        "zone_id": "Z029546536VUFGDCPXZ21",
    },
    "cs-survival-guide": {
        "domain_name": "survival-guide.jwall.engineering",
        "dev_domain_name": None,
        "enable_preview": True,
        "zone_id": "Z029546536VUFGDCPXZ21",
    },
    "main-site": {
        "domain_name": "jwall.engineering",
        "dev_domain_name": None,
        "enable_preview": True,
        "zone_id": "Z029546536VUFGDCPXZ21",
    },
}


def test_rendered_tfvars_match_the_old_infra_directories(site: Site) -> None:
    tfvars = render_tfvars(site)
    expected = EXPECTED_TFVARS[site.project]
    assert {key: tfvars[key] for key in expected} == expected
    assert tfvars["project"] == site.project
    assert tfvars["repository"] == site.config.repository
    assert tfvars["tags"]["Project"] == site.project
    assert tfvars["tags"]["Organization"] == "jwall-engineering"


def test_rendered_backend_matches_the_old_backend_files(site: Site) -> None:
    assert render_backend(site) == (
        'bucket       = "jwall-engineering-tfstate"\n'
        f'key          = "project={site.project}/env=prod/{site.project}-prod.tfstate"\n'
        'region       = "us-east-1"\n'
        "use_lockfile = true\n"
        "encrypt      = true\n"
    )


def test_tfvars_are_valid_for_the_stack_variables(site: Site) -> None:
    """Every variable the stack declares is rendered, and nothing else."""
    declared = {
        line.split('"')[1]
        for line in (REPO_ROOT / "stacks" / "site" / "variables.tf").read_text().splitlines()
        if line.startswith('variable "')
    }
    assert set(render_tfvars(site)) == declared


def _declared(stack: str) -> set[str]:
    return {
        line.split('"')[1]
        for line in (REPO_ROOT / "stacks" / stack / "variables.tf").read_text().splitlines()
        if line.startswith('variable "')
    }


def test_render_tfvars_for_bot(bot: Project, settings: Settings) -> None:
    assert render_tfvars(bot, settings) == {
        "project": "rusty-chess",
        "repository": "jwallace145/rusty-chess-v2",
        "tags": dict(bot.config.tags),
        "instance_type": "t3.micro",
        "architecture": "x86_64",
        "secret_parameters": {"LICHESS_TOKEN": "/jwall-engineering/rusty-chess/lichess-token"},
        "container_environment": {"ENGINE_HASH_MB": "128", "ENGINE_MOVE_OVERHEAD_MS": "500"},
        "bot_name": "rusty-chess-bot",
        "records_enabled": True,
        "records_prefix": "records/",
        "records_container_path": "/opt/lichess-bot/game_records",
        "log_retention_days": 30,
        "health_log_pattern": "awaiting challenges",
        "health_seconds": 90,
        "availability_zones": [
            "us-east-1a",
            "us-east-1b",
            "us-east-1c",
            "us-east-1d",
            "us-east-1f",
        ],
        "keep_release_images": 20,
        "expire_untagged_after_days": 7,
        "ami_name": settings.bot.amis["x86_64"],
        "task_runner_enabled": True,
        "task_instance_type": "c8a.16xlarge",
        "task_max_lifetime_hours": 6,
        "task_results_days": 7,
        "task_log_retention_days": 14,
        "task_rust_toolchain": settings.tasks.rust_toolchain,
    }


def test_bot_tfvars_are_valid_for_the_bot_stack(bot: Project, settings: Settings) -> None:
    assert set(render_tfvars(bot, settings)) == _declared("bot")


def test_site_project_renders_the_site_tfvars(site: Site, settings: Settings) -> None:
    project = Project.from_config(site.config, settings)
    assert render_tfvars(project, settings) == render_tfvars(site)


def test_stack_dir_by_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JES_STACK_DIR", raising=False)
    monkeypatch.setenv("JES_STACKS_ROOT", "/somewhere/stacks")
    assert default_stack_dir("bot") == Path("/somewhere/stacks/bot")
    assert default_stack_dir("site") == Path("/somewhere/stacks/site")
    monkeypatch.setenv("JES_STACK_DIR", "/explicit")
    assert default_stack_dir("bot") == Path("/explicit")


def _terraform(site: Site, settings: Settings, logger: logging.Logger) -> Terraform:
    return Terraform(
        site, settings, logger, stack_dir=REPO_ROOT / "stacks" / "site", plugin_dir=None
    )


def test_prepare_writes_inputs_into_a_copy(
    guitar: Site, settings: Settings, logger: logging.Logger, fake_terraform: Path
) -> None:
    terraform = _terraform(guitar, settings, logger)
    workdir = terraform.prepare()
    try:
        assert (workdir / "main.tf").is_file()
        assert (
            json.loads((workdir / "terraform.tfvars.json").read_text())["project"]
            == "guitar-knowledge-base"
        )
        assert (workdir / "backend.hcl").read_text() == render_backend(guitar)
        assert not (REPO_ROOT / "stacks" / "site" / "terraform.tfvars.json").exists()
    finally:
        terraform.cleanup(failed=False)
    assert not workdir.exists()


def test_version_mismatch_fails_fast(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_TF_VERSION", "1.9.0")
    terraform = _terraform(guitar, settings, logger)
    with pytest.raises(RunError, match=r"config\.yaml pins 1\.14\.7"):
        terraform.prepare()


def test_init_plan_and_outputs_invoke_terraform(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_TF_PLAN_EXIT", str(PLAN_CHANGES))
    terraform = _terraform(guitar, settings, logger)
    terraform.prepare()
    try:
        terraform.init()
        result = terraform.plan(detailed_exitcode=True)
        assert result.returncode == PLAN_CHANGES
        assert "Plan:" in result.output
        outputs = terraform.outputs()
        assert outputs["prod_distribution_id"] == "E1PROD"
    finally:
        terraform.cleanup(failed=False)
    calls = [c for c in fake_terraform.read_text().splitlines() if not c.startswith("version")]
    assert calls[0] == "init -input=false -no-color -backend-config=backend.hcl -reconfigure"
    assert (
        calls[1] == "plan -input=false -no-color "
        f"-lock-timeout={settings.terraform.lock_timeout} -detailed-exitcode"
    )
    assert calls[2] == "output -no-color -json"


def test_offline_init_uses_plugin_dir_and_no_backend(
    guitar: Site, settings: Settings, logger: logging.Logger, fake_terraform: Path, tmp_path: Path
) -> None:
    terraform = Terraform(
        guitar, settings, logger, stack_dir=REPO_ROOT / "stacks" / "site", plugin_dir=tmp_path
    )
    terraform.prepare()
    try:
        terraform.init(backend=False)
        terraform.validate()
    finally:
        terraform.cleanup(failed=False)
    calls = [c for c in fake_terraform.read_text().splitlines() if not c.startswith("version")]
    assert calls[0] == f"init -input=false -no-color -plugin-dir={tmp_path} -backend=false"
    assert calls[1] == "validate -no-color"


def test_plan_without_detailed_exitcode_treats_changes_as_success(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_TF_PLAN_EXIT", str(PLAN_NO_CHANGES))
    terraform = _terraform(guitar, settings, logger)
    terraform.prepare()
    try:
        result = terraform.plan(out=Path("tfplan"))
    finally:
        terraform.cleanup(failed=False)
    assert result.returncode == 0
    assert fake_terraform.read_text().splitlines()[-1].endswith("-out=tfplan")


def test_lock_info_is_reported(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("FAKE_TF_LOCKED", "1")
    terraform = _terraform(guitar, settings, logger)
    terraform.prepare()
    try:
        with (
            caplog.at_level(logging.ERROR, logger=logger.name),
            pytest.raises(RunError, match="plan failed"),
        ):
            terraform.plan(detailed_exitcode=True)
    finally:
        terraform.cleanup(failed=False)
    assert "the state is locked" in caplog.text
    assert "someone@host" in caplog.text


def test_missing_binary_is_a_run_error(
    guitar: Site, settings: Settings, logger: logging.Logger
) -> None:
    terraform = Terraform(
        guitar,
        settings,
        logger,
        stack_dir=REPO_ROOT / "stacks" / "site",
        plugin_dir=None,
        binary="no-such-terraform",
    )
    with pytest.raises(RunError, match="not found on PATH"):
        terraform.prepare()


def test_missing_stack_is_a_run_error(
    guitar: Site, settings: Settings, logger: logging.Logger, tmp_path: Path
) -> None:
    terraform = Terraform(guitar, settings, logger, stack_dir=tmp_path / "nope", plugin_dir=None)
    with pytest.raises(RunError, match="site stack not found"):
        terraform.prepare()


def test_failed_run_keeps_workdir_when_asked(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JES_KEEP_WORKDIR", "1")
    terraform = _terraform(guitar, settings, logger)
    workdir = terraform.prepare()
    terraform.cleanup(failed=True)
    assert workdir.exists()
    terraform.cleanup(failed=False)


def test_bot_tfvars_follow_the_latest_image_without_a_pin(bot: Project, settings: Settings) -> None:
    unpinned = settings.model_copy(update={"bot": settings.bot.model_copy(update={"amis": {}})})
    assert render_tfvars(bot, unpinned)["ami_name"] is None


def test_config_pins_the_x86_64_image(settings: Settings) -> None:
    """The bot instance only changes OS when this pin changes; it is pinned by name."""
    name = settings.bot.amis["x86_64"]
    assert name.startswith("al2023-ami-") and name.endswith("-x86_64")


def test_bot_tfvars_without_a_task_runner(tmp_path: Path, bot: Project, settings: Settings) -> None:
    components = tuple(c for c in bot.config.components if c.kind != "task-runner")
    plain = bot.model_copy(
        update={"config": bot.config.model_copy(update={"components": components})}
    )
    tfvars = render_tfvars(plain, settings)
    assert tfvars["task_runner_enabled"] is False
    # The other task variables keep valid values so the stack's validations hold.
    assert tfvars["task_instance_type"] == "c8a.16xlarge"
    assert set(tfvars) == _declared("bot")
