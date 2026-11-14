from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jwall_engineering_sites import stack as stack_module
from jwall_engineering_sites.databricks_auth import terraform_env
from jwall_engineering_sites.errors import ConfigError, RunError
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings
from jwall_engineering_sites.site import Site, load_site_config
from jwall_engineering_sites.stack import (
    PLAN_CHANGES,
    PLAN_NO_CHANGES,
    Terraform,
    _databricks_account_tfvars,
    _databricks_workspace_tfvars,
    dataset_bucket_addresses,
    dataset_names,
    default_stack_dir,
    deployment_name,
    render_backend,
    render_tfvars,
)
from tests.conftest import FIXTURES, REPO_ROOT

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
    assert tfvars["tags"]["ManagedBy"] == "jwall-engineering-infra"


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
        "repository": "jwallace145/braveheart",
        "tags": dict(bot.config.tags),
        "bot_enabled": True,
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
        "task_rust": True,
        "task_rust_toolchain": settings.tasks.rust_toolchain,
        "task_profiles": [
            {
                "name": "gpu",
                "default_instance_type": "g6.xlarge",
                "max_hours": 6,
                "ami_name": (
                    "Deep Learning Base OSS Nvidia Driver GPU AMI (Amazon Linux 2023) 20261027"
                ),
                "root_volume_gb": 200,
                "gpu": True,
                "rust": True,
                "datasets": [
                    {"name": "nnue", "read": ["selfplay/"], "write": ["nets/"], "write_once": []}
                ],
            },
            {
                "name": "sprt",
                "default_instance_type": "c8a.16xlarge",
                "max_hours": 6,
                "ami_name": None,
                "root_volume_gb": 40,
                "gpu": False,
                "rust": True,
                "datasets": [],
            },
            {
                "name": "match",
                "default_instance_type": "c8a.16xlarge",
                "max_hours": 6,
                "ami_name": None,
                "root_volume_gb": 40,
                "gpu": False,
                "rust": True,
                "datasets": [],
            },
            {
                "name": "datagen",
                "default_instance_type": "c8a.16xlarge",
                "max_hours": 6,
                "ami_name": None,
                "root_volume_gb": 40,
                "gpu": False,
                "rust": True,
                "datasets": [
                    {"name": "nnue", "read": [], "write": ["selfplay/"], "write_once": []}
                ],
            },
            {
                "name": "relabel",
                "default_instance_type": "c8a.16xlarge",
                "max_hours": 6,
                "ami_name": None,
                "root_volume_gb": 40,
                "gpu": False,
                "rust": True,
                "datasets": [
                    {
                        "name": "nnue",
                        "read": ["selfplay/"],
                        "write": ["selfplay/relabel/"],
                        "write_once": [],
                    }
                ],
            },
        ],
        "datasets": [
            {
                "name": "nnue",
                "bucket": "jwall-engineering-rusty-chess-nnue",
                "prefix": "data/",
                "writers": ["task-runner"],
                "readers": [],
                "transition_days": 0,
                "versioning": False,
                "expire_noncurrent_days": None,
                "expire_prefixes": {},
            }
        ],
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


def test_bot_tfvars_without_datasets(bot: Project, settings: Settings) -> None:
    components = tuple(c for c in bot.config.components if c.kind != "dataset")
    plain = bot.model_copy(
        update={"config": bot.config.model_copy(update={"components": components})}
    )
    assert render_tfvars(plain, settings)["datasets"] == []


def test_bot_tfvars_without_a_task_runner(tmp_path: Path, bot: Project, settings: Settings) -> None:
    components = tuple(c for c in bot.config.components if c.kind not in ("task-runner", "dataset"))
    plain = bot.model_copy(
        update={"config": bot.config.model_copy(update={"components": components})}
    )
    tfvars = render_tfvars(plain, settings)
    assert tfvars["task_runner_enabled"] is False
    # The other task variables keep valid values so the stack's validations hold.
    assert tfvars["task_instance_type"] == "c8a.16xlarge"
    assert set(tfvars) == _declared("bot")


# -- the bot stack's gating ---------------------------------------------------


def _blocks(path: Path) -> list[tuple[str, list[str], dict[str, str]]]:
    """(type, labels, attributes) of each top-level block of a `terraform fmt`-ed file.

    fmt puts a block's closing brace in column one and indents its own attributes
    by exactly two spaces, so nested blocks' attributes are not mistaken for them.
    An attribute's value is its first line.
    """
    blocks: list[tuple[str, list[str], dict[str, str]]] = []
    attributes: dict[str, str] = {}
    for line in path.read_text().splitlines():
        opening = re.fullmatch(r'(\w+)((?: "[^"]+")*) \{', line)
        attribute = re.fullmatch(r"  (\w+)\s*=\s*(.*)", line)
        if opening:
            attributes = {}
            labels = re.findall(r'"([^"]+)"', opening.group(2))
            blocks.append((opening.group(1), labels, attributes))
        elif attribute:
            attributes[attribute.group(1)] = attribute.group(2)
    return blocks


def _bot_stack(block_type: str, file: str = "main.tf") -> dict[str, dict[str, str]]:
    """Blocks of one type in a stacks/bot file, by first label."""
    return {
        labels[0]: attributes
        for kind, labels, attributes in _blocks(REPO_ROOT / "stacks" / "bot" / file)
        if kind == block_type
    }


def _strip_new_keys(tfvars: dict[str, Any]) -> dict[str, Any]:
    """`tfvars` without the keys stack gating added."""
    old = {k: v for k, v in tfvars.items() if k not in ("bot_enabled", "task_rust")}
    old["task_profiles"] = [
        {
            **{k: v for k, v in profile.items() if k != "rust"},
            "datasets": [
                {k: v for k, v in dataset.items() if k != "write_once"}
                for dataset in profile["datasets"]
            ],
        }
        for profile in tfvars["task_profiles"]
    ]
    old["datasets"] = [
        {
            k: v
            for k, v in dataset.items()
            if k not in ("versioning", "expire_noncurrent_days", "expire_prefixes")
        }
        for dataset in tfvars["datasets"]
    ]
    return old


def test_rusty_chess_tfvars_are_unchanged(bot: Project, settings: Settings) -> None:
    """The rusty-chess fixture's inputs are what they were before gating, plus new keys
    at their defaults. The fixture is a sample, not the live bot's file.

    The snapshot was rendered by the code before this change (commit 76187b0). It
    holds the pinned AMIs and Rust toolchain from config.yaml, so regenerate it when
    `bot.amis`, `tasks.images` or `tasks.rust_toolchain` is bumped.
    """
    snapshot = json.loads((FIXTURES / "bots" / "rusty-chess.tfvars.json").read_text())
    tfvars = render_tfvars(bot, settings)
    assert _strip_new_keys(tfvars) == snapshot

    assert tfvars["bot_enabled"] is True
    assert tfvars["task_rust"] is True
    assert tfvars["task_profiles"], "the snapshot must exercise task profiles"
    for profile in tfvars["task_profiles"]:
        assert profile["rust"] is True
        for dataset in profile["datasets"]:
            assert dataset["write_once"] == []
    assert tfvars["datasets"], "the snapshot must exercise datasets"
    for dataset in tfvars["datasets"]:
        assert dataset["versioning"] is False
        assert dataset["expire_noncurrent_days"] is None
        assert dataset["expire_prefixes"] == {}


def test_research_tfvars(research: Project, settings: Settings) -> None:
    tfvars = render_tfvars(research, settings)
    assert tfvars["bot_enabled"] is False
    assert tfvars["records_enabled"] is False
    assert tfvars["architecture"] == "x86_64"
    assert tfvars["task_runner_enabled"] is True
    assert tfvars["task_rust"] is False
    # A task runner needs the network and the cpu profile's pinned image.
    assert tfvars["availability_zones"] == list(settings.bot.availability_zones)
    assert tfvars["ami_name"] == settings.bot.amis["x86_64"]

    profiles = {p["name"]: p for p in tfvars["task_profiles"]}
    assert set(profiles) == {"train"}
    train = profiles["train"]
    assert train["rust"] is False
    assert train["ami_name"] == settings.tasks.images["deep-learning-gpu"]
    assert train["datasets"] == [
        {
            "name": "artifacts",
            "read": ["datasets/", "models/", "runs/", "tmp/"],
            "write": ["tmp/"],
            "write_once": ["runs/", "models/"],
        }
    ]

    assert tfvars["datasets"] == [
        {
            "name": "artifacts",
            "bucket": "jwall-engineering-math-prover-artifacts",
            "prefix": "data/",
            "writers": [],
            "readers": [],
            "transition_days": 0,
            "versioning": True,
            "expire_noncurrent_days": 90,
            "expire_prefixes": {"tmp/": 30},
        }
    ]


def test_research_tfvars_leave_the_bots_inputs_to_the_stacks_defaults(
    research: Project, settings: Settings
) -> None:
    tfvars = render_tfvars(research, settings)
    bot_only = {
        "instance_type",
        "secret_parameters",
        "container_environment",
        "bot_name",
        "records_prefix",
        "records_container_path",
        "log_retention_days",
        "health_log_pattern",
        "health_seconds",
        "keep_release_images",
        "expire_untagged_after_days",
    }
    assert not bot_only & set(tfvars)
    variables = _bot_stack("variable", "variables.tf")
    assert bot_only <= {name for name, block in variables.items() if "default" in block}


def _dataset_only(research: Project) -> Project:
    components = tuple(c for c in research.config.components if c.kind == "dataset")
    config = research.config.model_copy(update={"components": components})
    return research.model_copy(update={"config": config})


def test_dataset_only_project_disables_network(research: Project, settings: Settings) -> None:
    """A research file with only a dataset creates no VPC and no task resources."""
    tfvars = render_tfvars(_dataset_only(research), settings)
    assert tfvars["bot_enabled"] is False
    assert tfvars["task_runner_enabled"] is False
    assert tfvars["task_profiles"] == []
    assert [d["name"] for d in tfvars["datasets"]] == ["artifacts"]
    assert tfvars["ami_name"] is None, "nothing launches, so no image to resolve"


def test_network_exists_for_a_bot_or_a_task_runner() -> None:
    locals_blocks = [
        attributes
        for kind, _, attributes in _blocks(REPO_ROOT / "stacks" / "bot" / "main.tf")
        if kind == "locals"
    ]
    assert locals_blocks[0]["network_enabled"] == "var.bot_enabled || var.task_runner_enabled"
    modules = _bot_stack("module")
    assert modules["network"]["count"] == "local.network_enabled ? 1 : 0"
    assert modules["image"]["count"] == "var.bot_enabled ? 1 : 0"
    assert modules["bot"]["count"] == "var.bot_enabled ? 1 : 0"
    assert modules["records"]["count"] == "var.records_enabled ? 1 : 0"
    assert modules["tasks"]["count"] == "var.task_runner_enabled ? 1 : 0"
    assert modules["task_results"]["count"] == "var.task_runner_enabled ? 1 : 0"


def test_stack_has_moved_blocks() -> None:
    moved = [
        (attributes["from"], attributes["to"])
        for kind, _, attributes in _blocks(REPO_ROOT / "stacks" / "bot" / "main.tf")
        if kind == "moved"
    ]
    assert sorted(moved) == [
        ("module.bot", "module.bot[0]"),
        ("module.image", "module.image[0]"),
        ("module.network", "module.network[0]"),
    ]


def test_gated_modules_are_only_referenced_by_index() -> None:
    """Outside the moved blocks, an unindexed reference to a counted module is invalid."""
    for path in (REPO_ROOT / "stacks" / "bot").glob("*.tf"):
        for line in path.read_text().splitlines():
            code = line.split("#", 1)[0]
            if re.fullmatch(r"\s*from\s*=\s*module\.(image|bot|network)\s*", code):
                continue
            unindexed = re.findall(r"module\.(?:image|bot|network)\b(?!\[0\])", code)
            assert not unindexed, f"{path.name}: {line.strip()}"


def test_outputs_keep_their_names_and_are_null_safe() -> None:
    outputs = _bot_stack("output", "outputs.tf")
    expected = {
        "image_repository_url": "module.image[0].url",
        "instance_id": "module.bot[0].instance_id",
        "public_ip": "module.bot[0].public_ip",
        "log_group_name": "module.bot[0].log_group_name",
        "parameter_names": "module.bot[0].parameter_names",
        "records_uri": "module.bot[0].records_uri",
        "vpc_id": "module.network[0].vpc_id",
    }
    for name, source in expected.items():
        assert outputs[name]["value"] == f"try({source}, null)"
    assert set(outputs) == {
        *expected,
        "task_launch_template",
        "task_log_group",
        "task_results_uri",
        "dataset_uris",
        "task_profile_launch_templates",
    }


def test_rust_toolchain_follows_the_rust_flags() -> None:
    modules = _bot_stack("module")
    assert modules["tasks"]["rust_toolchain"] == "var.task_rust ? var.task_rust_toolchain : null"
    assert (
        modules["task_profiles"]["rust_toolchain"]
        == "each.value.rust ? var.task_rust_toolchain : null"
    )


def test_datasets_receive_retention_and_write_once() -> None:
    dataset = _bot_stack("module")["datasets"]
    assert dataset["versioning"] == "each.value.versioning"
    assert dataset["expire_noncurrent_days"] == "each.value.expire_noncurrent_days"
    assert dataset["expire_prefixes"] == "each.value.expire_prefixes"
    main_tf = (REPO_ROOT / "stacks" / "bot" / "main.tf").read_text()
    assert re.search(
        r"write_once\s*=\s*one\(\[for d in p\.datasets : d\.write_once if d\.name == each\.key\]\)",
        main_tf,
    )


def _assert_keys_match_stack_variables(tfvars: dict[str, Any]) -> None:
    """Rendered keys are all declared, and every variable without a default is rendered."""
    variables = _bot_stack("variable", "variables.tf")
    assert set(tfvars) <= set(variables)
    assert {name for name, block in variables.items() if "default" not in block} <= set(tfvars)


def test_tfvars_keys_match_stack_variables(
    bot: Project, research: Project, settings: Settings
) -> None:
    _assert_keys_match_stack_variables(render_tfvars(bot, settings))
    _assert_keys_match_stack_variables(render_tfvars(research, settings))
    _assert_keys_match_stack_variables(render_tfvars(_dataset_only(research), settings))


def test_new_variables_are_typed_for_the_rendered_values() -> None:
    """A key added to an object type but not to the tfvars (or back) fails `terraform plan`."""
    source = (REPO_ROOT / "stacks" / "bot" / "variables.tf").read_text()
    profiles = source[
        source.index('variable "task_profiles"') : source.index('variable "datasets"')
    ]
    assert re.search(r"^\s+rust\s+= bool$", profiles, re.MULTILINE)
    assert re.search(r"^\s+write_once = list\(string\)$", profiles, re.MULTILINE)
    datasets = source[source.index('variable "datasets"') :]
    assert re.search(r"^\s+versioning\s+= bool$", datasets, re.MULTILINE)
    assert re.search(r"^\s+expire_noncurrent_days\s+= number$", datasets, re.MULTILINE)
    assert re.search(r"^\s+expire_prefixes\s+= map\(number\)$", datasets, re.MULTILINE)


def test_research_uses_the_bot_stack(
    research: Project,
    settings: Settings,
    logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JES_STACK_DIR", raising=False)
    monkeypatch.setenv("JES_STACKS_ROOT", "/somewhere/stacks")
    terraform = Terraform(research, settings, logger, plugin_dir=None)
    assert terraform.stack_dir == Path("/somewhere/stacks/bot")


def test_prepare_renders_a_research_project_into_the_bot_stack(
    research: Project, settings: Settings, logger: logging.Logger, fake_terraform: Path
) -> None:
    terraform = Terraform(research, settings, logger, plugin_dir=None)
    workdir = terraform.prepare()
    try:
        assert (workdir / "main.tf").read_text() == (
            REPO_ROOT / "stacks" / "bot" / "main.tf"
        ).read_text()
        assert json.loads((workdir / "terraform.tfvars.json").read_text())["bot_enabled"] is False
    finally:
        terraform.cleanup(failed=False)


# -- releasing dataset buckets from state --------------------------------------


def test_dataset_bucket_addresses_keeps_the_buckets_and_leaves_the_grants() -> None:
    bucket = 'module.datasets["nnue"].aws_s3_bucket.this'
    policy = 'module.datasets["nnue"].aws_s3_bucket_policy.this'
    lifecycle = 'module.datasets["nnue"].aws_s3_bucket_lifecycle_configuration.this'
    state = [
        "aws_s3_bucket.outside_the_module",
        "module.records[0].aws_s3_bucket.this",
        'module.datasets_old["nnue"].aws_s3_bucket.this',
        lifecycle,
        'module.datasets["nnue"].aws_iam_role_policy.grant["x"]',
        'module.datasets["nnue"].aws_iam_role_policy.writer["x"]',
        'module.datasets["nnue"].data.aws_s3_bucket.lookup',
        bucket,
        policy,
    ]
    assert dataset_bucket_addresses(state) == [lifecycle, bucket, policy]


@pytest.mark.parametrize(
    "address",
    [
        # A role whose name holds the type, in the instance key.
        'module.datasets["nnue"].aws_iam_role_policy.grant["aws_s3_bucket.this"]',
        # A dataset whose name holds the type, or looks like an address.
        'module.datasets["aws_s3_bucket"].aws_iam_role_policy.grant["x"]',
        'module.datasets["a.aws_s3_bucket.b"].aws_iam_role_policy.writer["x"]',
        'module.datasets["a\\"].aws_s3_bucket.b"].aws_iam_role_policy.grant["x"]',
    ],
)
def test_dataset_bucket_addresses_ignores_names_holding_the_type(address: str) -> None:
    assert dataset_bucket_addresses([address]) == []


def test_dataset_bucket_addresses_finds_a_bucket_of_a_dataset_named_like_one() -> None:
    address = 'module.datasets["aws_s3_bucket_x"].aws_s3_bucket.this'
    assert dataset_bucket_addresses([address]) == [address]


def test_dataset_bucket_addresses_of_nothing() -> None:
    assert dataset_bucket_addresses([]) == []


def test_dataset_names_are_those_with_a_released_bucket() -> None:
    state = [
        'module.datasets["b"].aws_s3_bucket.this',
        'module.datasets["a"].aws_s3_bucket.this',
        'module.datasets["a"].aws_s3_bucket_policy.this',
        'module.datasets["c"].aws_iam_role_policy.grant["x"]',
    ]
    assert dataset_names(dataset_bucket_addresses(state)) == ["a", "b"]


# -- Databricks projects: two stacks, the secret only in the environment -------

DATABRICKS_SENTINEL = "S3CR3T-SENTINEL"

#: What the account stack hands the workspace stack (its outputs, by name).
ACCOUNT_OUTPUTS: dict[str, Any] = {
    "workspace_id": "1234567890123456",
    "workspace_url": "https://jwall-databricks-poc.cloud.databricks.com",
    "admin_group_name": "databricks-poc-workspace-admins",
    "users_group_name": "databricks-poc-workspace-users",
    "metastore_id": "abcdef01-2345-6789-abcd-ef0123456789",
    "root_bucket_name": "jwall-engineering-databricks-poc-databricks-root",
}

ACCOUNT_TFVARS_KEYS = {
    "project",
    "environment",
    "repository",
    "tags",
    "vpc_cidr",
    "nat_ami_name",
    "metastore_name",
    "deployment_name",
    "admin_user_names",
    "user_names",
    "service_principal_application_id",
    "account_id",
}

WORKSPACE_TFVARS_KEYS = {
    "project",
    "environment",
    "repository",
    "tags",
    "workspace_url",
    "workspace_id",
    "admin_group_name",
    "users_group_name",
    "metastore_id",
    "compute_policies",
    "fixed_tags",
    "governance_project",
    "apply_role_arn",
    "management_role_arns",
    "catalog_tags",
}


@pytest.fixture
def databricks(settings: Settings) -> Project:
    return Project.from_config(
        load_site_config(FIXTURES / "bots" / "databricks-poc.yaml"), settings
    )


def test_databricks_account_tfvars(databricks: Project, settings: Settings) -> None:
    tfvars = _databricks_account_tfvars(databricks, settings)
    assert set(tfvars) == ACCOUNT_TFVARS_KEYS
    assert settings.databricks is not None
    assert tfvars == {
        "project": "databricks-poc",
        "environment": "prod",
        "repository": "example-org/databricks-poc",
        "tags": databricks.databricks_tags,
        "vpc_cidr": "10.40.0.0/16",
        "nat_ami_name": settings.databricks.nat_ami,
        "metastore_name": "jwall-engineering-us-east-1",
        "deployment_name": None,  # no prefix configured: Databricks picks the URL
        "admin_user_names": ["owner@example.com"],
        "user_names": [],
        "service_principal_application_id": settings.databricks.client_id,
        "account_id": settings.databricks.account_id,
    }
    assert tfvars["tags"]["CostCenter"] == "cc-0000"
    assert tfvars["tags"]["Workspace"] == "databricks-poc-prod"


def test_databricks_tfvars_are_json_serialisable(databricks: Project, settings: Settings) -> None:
    json.dumps(_databricks_account_tfvars(databricks, settings))
    json.dumps(_databricks_workspace_tfvars(databricks, settings, ACCOUNT_OUTPUTS))


def _with_prefix(settings: Settings, prefix: str | None) -> Settings:
    assert settings.databricks is not None
    section = settings.databricks.model_copy(update={"deployment_name_prefix": prefix})
    return settings.model_copy(update={"databricks": section})


def _with_deployment_name(project: Project, name: str | None) -> Project:
    components = tuple(
        c.model_copy(update={"deployment_name": name}) for c in project.config.components
    )
    return project.model_copy(
        update={"config": project.config.model_copy(update={"components": components})}
    )


def test_config_leaves_the_deployment_name_prefix_unset(settings: Settings) -> None:
    assert settings.databricks is not None
    assert settings.databricks.deployment_name_prefix is None


def test_without_a_prefix_databricks_picks_the_url(databricks: Project, settings: Settings) -> None:
    assert _databricks_account_tfvars(databricks, settings)["deployment_name"] is None
    # The project file's name is ignored too: Databricks would reject it.
    named = _with_deployment_name(databricks, "poc-one")
    assert _databricks_account_tfvars(named, settings)["deployment_name"] is None


def test_with_a_prefix_the_deployment_name_is_the_project(
    databricks: Project, settings: Settings
) -> None:
    name = _databricks_account_tfvars(databricks, _with_prefix(settings, "acme"))["deployment_name"]
    assert name == "databricks-poc"
    assert re.fullmatch(r"^[a-z0-9-]{3,30}$", name)


def test_with_a_prefix_the_project_files_name_wins(databricks: Project, settings: Settings) -> None:
    named = _with_deployment_name(databricks, "poc-one")
    tfvars = _databricks_account_tfvars(named, _with_prefix(settings, "acme"))
    assert tfvars["deployment_name"] == "poc-one"


def test_deployment_name_of_the_longest_project_fits() -> None:
    longest = "a" * 21  # DATABRICKS_PROJECT_NAME_MAX
    assert deployment_name(longest, "acme") == longest


def test_too_long_deployment_name_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="deployment_name"):
        deployment_name("databricks-poc", "acme", "x" * 31)


def test_too_long_url_label_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match="URL label"):
        deployment_name("databricks-poc", "p" * 40, "x" * 30)


def test_prefix_must_be_kebab_case(settings: Settings) -> None:
    raw = settings.model_dump()
    raw["databricks"]["deployment_name_prefix"] = "Acme_Corp"
    with pytest.raises(ValidationError):
        Settings.model_validate(raw)


def test_workspace_tfvars_take_account_outputs(databricks: Project, settings: Settings) -> None:
    tfvars = _databricks_workspace_tfvars(databricks, settings, ACCOUNT_OUTPUTS)
    assert set(tfvars) == WORKSPACE_TFVARS_KEYS
    for key in ("workspace_url", "workspace_id", "admin_group_name", "users_group_name"):
        assert tfvars[key] == ACCOUNT_OUTPUTS[key]
    assert tfvars["metastore_id"] == ACCOUNT_OUTPUTS["metastore_id"]
    assert tfvars["project"] == "databricks-poc"
    assert tfvars["environment"] == "prod"
    assert tfvars["repository"] == "example-org/databricks-poc"
    assert tfvars["tags"] == databricks.databricks_tags
    assert tfvars["fixed_tags"] == databricks.databricks_tags
    assert tfvars["governance_project"] == "databricks-poc"
    assert tfvars["apply_role_arn"] == (
        "arn:aws:iam::650503560686:role/jwall-engineering-databricks-poc-ci-apply"
    )
    assert tfvars["management_role_arns"] == [
        "arn:aws:iam::650503560686:role/jwall-engineering-databricks-poc-ci-plan"
    ]
    assert tfvars["catalog_tags"] == {"Project": "databricks-poc", "Owner": "owner@example.com"}


def test_workspace_tfvars_render_through_render_tfvars(
    databricks: Project, settings: Settings
) -> None:
    assert render_tfvars(
        databricks, settings, stack="workspace", account_outputs=ACCOUNT_OUTPUTS
    ) == _databricks_workspace_tfvars(databricks, settings, ACCOUNT_OUTPUTS)


@pytest.mark.parametrize(
    "missing",
    ["workspace_url", "workspace_id", "admin_group_name", "users_group_name", "metastore_id"],
)
def test_missing_account_output_is_a_config_error_naming_it(
    databricks: Project, settings: Settings, missing: str
) -> None:
    outputs = {k: v for k, v in ACCOUNT_OUTPUTS.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        _databricks_workspace_tfvars(databricks, settings, outputs)


def test_workspace_tfvars_without_outputs_is_a_config_error(
    databricks: Project, settings: Settings
) -> None:
    with pytest.raises(ConfigError, match="account stack"):
        render_tfvars(databricks, settings, stack="workspace")


def test_compute_policies_are_filtered_to_the_projects_file(
    databricks: Project, settings: Settings
) -> None:
    assert settings.databricks is not None
    central = settings.databricks.compute_policies
    extra = central["jobs"].model_copy(update={"description": "Not this project's"})
    widened = settings.model_copy(
        update={
            "databricks": settings.databricks.model_copy(
                update={"compute_policies": {**central, "gpu-large": extra}}
            )
        }
    )
    only_jobs = databricks.model_copy(
        update={
            "config": databricks.config.model_copy(
                update={
                    "components": tuple(
                        c.model_copy(update={"compute_policies": ("jobs",)})
                        for c in databricks.config.components
                    )
                }
            )
        }
    )
    assert only_jobs.databricks.compute_policies == ("jobs",)
    policies = _databricks_workspace_tfvars(only_jobs, widened, ACCOUNT_OUTPUTS)["compute_policies"]
    assert policies == {
        "jobs": {
            "description": central["jobs"].description,
            "definition": central["jobs"].definition,
        }
    }
    full = _databricks_workspace_tfvars(databricks, widened, ACCOUNT_OUTPUTS)["compute_policies"]
    assert set(full) == {"small-interactive", "jobs"}
    assert full["small-interactive"]["definition"]["num_workers"] == {
        "type": "range",
        "maxValue": 2,
    }


def test_databricks_is_routed_away_from_bot_and_research(
    databricks: Project, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any) -> dict[str, Any]:
        raise AssertionError("a databricks project reached the bot stack's tfvars")

    monkeypatch.setattr(stack_module, "_bot_tfvars", refuse)
    monkeypatch.setattr(stack_module, "_research_tfvars", refuse)
    monkeypatch.setattr(stack_module, "_lichess_bot_tfvars", refuse)
    tfvars = render_tfvars(databricks, settings, stack="account")
    assert set(tfvars) == ACCOUNT_TFVARS_KEYS
    assert "bot_enabled" not in tfvars


def test_databricks_needs_a_known_stack(databricks: Project, settings: Settings) -> None:
    with pytest.raises(ValueError, match="stack"):
        render_tfvars(databricks, settings)
    with pytest.raises(ValueError, match="stack"):
        render_tfvars(databricks, settings, stack="bogus")


def test_existing_kinds_ignore_the_new_arguments(
    bot: Project, research: Project, guitar: Site, settings: Settings
) -> None:
    assert render_tfvars(bot, settings) == render_tfvars(bot, settings, stack=None)
    assert render_tfvars(research, settings)["bot_enabled"] is False
    assert render_tfvars(guitar) == render_tfvars(guitar, settings)


def test_per_stack_state_keys(
    databricks: Project, settings: Settings, logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JES_STACK_DIR", raising=False)
    monkeypatch.setenv("JES_STACKS_ROOT", "/somewhere/stacks")
    account = Terraform(databricks, settings, logger, plugin_dir=None, stack="account")
    workspace = Terraform(
        databricks,
        settings,
        logger,
        plugin_dir=None,
        stack="workspace",
        tfvars_override={"project": "databricks-poc"},
    )
    assert account.stack_dir == Path("/somewhere/stacks/databricks-account")
    assert workspace.stack_dir == Path("/somewhere/stacks/databricks-workspace")
    assert account.stack_name == "databricks-account"
    assert workspace.stack_name == "databricks-workspace"
    assert account.state_key == (
        "project=databricks-poc/env=prod/databricks-poc-prod-account.tfstate"
    )
    assert workspace.state_key == (
        "project=databricks-poc/env=prod/databricks-poc-prod-workspace.tfstate"
    )
    assert f'key          = "{account.state_key}"\n' in account.backend()
    assert f'key          = "{workspace.state_key}"\n' in workspace.backend()
    # The project's own key (no stack) is never used for a Databricks stack.
    assert databricks.state_key not in (account.state_key, workspace.state_key)


def test_existing_kinds_keep_their_stack_and_key(
    bot: Project, guitar: Site, settings: Settings, logger: logging.Logger
) -> None:
    terraform = Terraform(bot, settings, logger, plugin_dir=None)
    assert terraform.state_key == bot.state_key
    assert terraform.stack_name == "bot"
    assert terraform.backend() == render_backend(bot)
    site = Terraform(guitar, settings, logger, plugin_dir=None)
    assert site.backend() == render_backend(guitar)


def _databricks_stack_dir(tmp_path: Path) -> Path:
    stack = tmp_path / "stack"
    stack.mkdir()
    (stack / "main.tf").write_text("# empty\n")
    return stack


def test_secret_only_in_env(
    databricks: Project,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env_dump = tmp_path / "child.env"
    monkeypatch.setenv("FAKE_TF_ENV", str(env_dump))
    caplog.set_level(logging.DEBUG)
    secret_env = terraform_env(settings, DATABRICKS_SENTINEL)
    stack_dir = _databricks_stack_dir(tmp_path)
    for stack, override in (
        ("account", None),
        ("workspace", _databricks_workspace_tfvars(databricks, settings, ACCOUNT_OUTPUTS)),
    ):
        terraform = Terraform(
            databricks,
            settings,
            logger,
            stack_dir=stack_dir,
            plugin_dir=None,
            stack=stack,
            extra_env=secret_env,
            tfvars_override=override,
        )
        workdir = terraform.prepare()
        try:
            terraform.init()
            terraform.plan(detailed_exitcode=True)
            tfvars = (workdir / "terraform.tfvars.json").read_text()
            backend = (workdir / "backend.hcl").read_text()
            assert DATABRICKS_SENTINEL not in tfvars
            assert DATABRICKS_SENTINEL not in backend
            assert json.loads(tfvars)["project"] == "databricks-poc"
            if override is not None:
                assert json.loads(tfvars) == override
        finally:
            terraform.cleanup(failed=False)

    assert caplog.records, "the debug log must have been captured"
    assert DATABRICKS_SENTINEL not in caplog.text
    assert all(DATABRICKS_SENTINEL not in repr(r.args) for r in caplog.records)
    child = env_dump.read_text()
    assert f"DATABRICKS_CLIENT_SECRET={DATABRICKS_SENTINEL}" in child
    assert f"DATABRICKS_CLIENT_ID={settings.databricks.client_id}" in child  # type: ignore[union-attr]
    assert "TF_IN_AUTOMATION=1" in child
    assert DATABRICKS_SENTINEL not in fake_terraform.read_text()  # the argv log


def test_extra_env_is_not_in_the_parent_environment(
    databricks: Project, settings: Settings, logger: logging.Logger
) -> None:
    terraform = Terraform(
        databricks,
        settings,
        logger,
        plugin_dir=None,
        stack="account",
        extra_env={"DATABRICKS_CLIENT_SECRET": DATABRICKS_SENTINEL},
    )
    assert terraform._environment()["DATABRICKS_CLIENT_SECRET"] == DATABRICKS_SENTINEL
    assert "DATABRICKS_CLIENT_SECRET" not in os.environ
    assert DATABRICKS_SENTINEL not in repr(terraform)
