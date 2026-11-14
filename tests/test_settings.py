from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jwall_engineering_sites.errors import ConfigError
from jwall_engineering_sites.settings import (
    ProjectGuardrails,
    Settings,
    load_settings,
    parse_duration,
)
from tests.conftest import FIXTURES, REPO_ROOT


def test_repository_config_loads() -> None:
    settings = load_settings(REPO_ROOT / "config.yaml", environ={})
    assert settings.ecr.repository == "jwall-engineering-sites"
    assert settings.state.bucket == "jwall-engineering-tfstate"
    assert settings.dns.zone == "jwall.engineering"


def test_fixture_matches_repository_config() -> None:
    assert (FIXTURES / "config.yaml").read_text() == (REPO_ROOT / "config.yaml").read_text(), (
        "tests/fixtures/config.yaml must be a copy of config.yaml"
    )


def test_state_key_template(settings: Settings) -> None:
    assert settings.state_key("guitar", "prod") == "project=guitar/env=prod/guitar-prod.tfstate"


@pytest.mark.parametrize(("value", "seconds"), [("120s", 120), ("15m", 900), ("1h", 3600)])
def test_parse_duration(value: str, seconds: int) -> None:
    assert parse_duration(value) == seconds


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration("soon")


def _write(tmp_path: Path, mutate: dict[str, object]) -> Path:
    raw = yaml.safe_load((FIXTURES / "config.yaml").read_text())
    for dotted, value in mutate.items():
        section, key = dotted.split(".")
        raw.setdefault(section, {})[key] = value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, {"state.bukcet": "typo"})
    with pytest.raises(ConfigError, match=r"state\.bukcet"):
        load_settings(path, environ={})


def test_key_template_needs_placeholders(tmp_path: Path) -> None:
    path = _write(tmp_path, {"state.key_template": "project={project}/state.tfstate"})
    with pytest.raises(ConfigError, match="environment"):
        load_settings(path, environ={})


def test_bad_duration_is_a_config_error(tmp_path: Path) -> None:
    path = _write(tmp_path, {"terraform.lock_timeout": "soon"})
    with pytest.raises(ConfigError, match="lock_timeout"):
        load_settings(path, environ={})


def test_env_overrides_only_logging(tmp_path: Path) -> None:
    settings = load_settings(
        FIXTURES / "config.yaml",
        environ={"JES_LOG_LEVEL": "debug", "JES_LOG_FORMAT": "json", "JES_STATE_BUCKET": "evil"},
    )
    assert settings.tool.log_level == "debug"
    assert settings.tool.log_format == "json"
    assert settings.state.bucket == "jwall-engineering-tfstate"


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "nope.yaml", environ={})


def test_bot_section_defaults(settings: Settings) -> None:
    assert settings.bot.health_log_pattern == "awaiting challenges"
    assert settings.bot.health_seconds == 90
    assert settings.bot.records_prefix == "records/"
    assert settings.bot.availability_zones == (
        "us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d", "us-east-1f",
    )  # fmt: skip


def test_bot_health_seconds_bounds(tmp_path: Path) -> None:
    raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    raw["bot"]["health_seconds"] = 5
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="health_seconds"):
        load_settings(path, environ={})


def test_aws_account_is_configured(settings: Settings) -> None:
    """The one place the account ID lives: workflows build role ARNs from it."""
    assert settings.aws.account_id == "650503560686"


def test_aws_account_must_be_twelve_digits(tmp_path: Path) -> None:
    raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    raw["aws"]["account_id"] = "65050356068"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="account_id"):
        load_settings(path, environ={})


def test_bot_amis_are_validated(tmp_path: Path) -> None:
    raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    raw["bot"]["amis"] = {"x86_64": "ami-04b938437b69a2c9c"}  # an ID, not a name
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="amis"):
        load_settings(path, environ={})


def test_bot_amis_only_name_known_architectures(tmp_path: Path) -> None:
    raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    raw["bot"]["amis"] = {"riscv64": "ami-0123456789abcdef0"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="amis"):
        load_settings(path, environ={})


def test_bot_ami_name_must_match_its_architecture(tmp_path: Path) -> None:
    raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    raw["bot"]["amis"] = {"x86_64": "al2023-ami-2023.9.20251027.0-kernel-6.1-arm64"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match="must end in -x86_64"):
        load_settings(path, environ={})


def test_bot_asset_wait(settings: Settings) -> None:
    """How long a build waits for a release's assets to be attached."""
    assert settings.bot.asset_wait_seconds == 1800


def test_tasks_section(settings: Settings) -> None:
    assert settings.tasks.rust_toolchain == "1.99.1"
    assert settings.tasks.poll_seconds == 5


@pytest.mark.parametrize("bad", ["", "1.99.1; rm -rf /", "Stable", "1.99.1 "])
def test_tasks_toolchain_is_a_rustup_name(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError, match="rust_toolchain"):
        load_settings(_write(tmp_path, {"tasks.rust_toolchain": bad}), environ={})


@pytest.mark.parametrize("bad", [0, 61])
def test_tasks_poll_bounds(tmp_path: Path, bad: int) -> None:
    with pytest.raises(ConfigError, match="poll_seconds"):
        load_settings(_write(tmp_path, {"tasks.poll_seconds": bad}), environ={})


def test_task_limits_and_default_ceiling(settings: Settings) -> None:
    assert settings.tasks.limits.model_dump() == {
        "max_hours": 6,
        "max_vcpus": 256,
        "root_volume_gb": 200,
    }
    assert settings.tasks.max_run_cost_usd == 50


def test_rusty_chess_guardrails_match_the_previous_lists(settings: Settings) -> None:
    guardrails = settings.projects["rusty-chess"]
    assert guardrails.instance_types == (
        "c8a.xlarge",
        "c8a.8xlarge",
        "c8a.16xlarge",
        "g6.xlarge",
        "g6.2xlarge",
        "g6e.xlarge",
    )
    assert guardrails.task_profiles == ("cpu", "gpu", "sprt", "match", "datagen", "relabel")
    assert list(guardrails.datasets) == ["jwall-engineering-rusty-chess-nnue"]
    retention = guardrails.datasets["jwall-engineering-rusty-chess-nnue"]
    assert retention.write_once == ()
    assert retention.expire_prefixes == {}
    assert retention.expire_noncurrent_days is None
    assert retention.versioning is False
    assert guardrails.max_run_cost_usd is None


def test_run_cost_ceiling_defaults_and_overrides(settings: Settings) -> None:
    assert settings.run_cost_ceiling("rusty-chess") == 50
    assert settings.run_cost_ceiling("not-registered") == 50
    raised = settings.model_copy(
        update={"projects": {"big": ProjectGuardrails(max_run_cost_usd=80)}}
    )
    assert raised.run_cost_ceiling("big") == 80
    assert raised.run_cost_ceiling("rusty-chess") == 50


def _with_projects(tmp_path: Path, projects: dict[str, object]) -> Path:
    raw = yaml.safe_load((FIXTURES / "config.yaml").read_text())
    raw["projects"] = projects
    del raw["databricks"]  # its projects are replaced here, and it needs one
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.mark.parametrize(
    "projects",
    [
        {"p": {"instance_typez": ["c8a.xlarge"]}},
        {"p": {"datasets": {"b": {"write_onse": ["runs/"]}}}},
    ],
)
def test_unknown_guardrail_key_is_refused(tmp_path: Path, projects: dict[str, object]) -> None:
    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        load_settings(_with_projects(tmp_path, projects), environ={})


def test_unknown_limit_key_is_refused(tmp_path: Path) -> None:
    raw = yaml.safe_load((FIXTURES / "config.yaml").read_text())
    raw["tasks"]["limits"]["max_gpus"] = 1
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ConfigError, match=r"tasks\.limits\.max_gpus"):
        load_settings(path, environ={})


def test_dataset_retention_is_typed(tmp_path: Path) -> None:
    path = _with_projects(
        tmp_path,
        {
            "math-prover": {
                "datasets": {
                    "b": {
                        "prefix": "data/",
                        "write_once": ["runs/", "models/"],
                        "expire_prefixes": {"tmp/": 30},
                        "expire_noncurrent_days": 90,
                        "versioning": True,
                    }
                },
                "max_run_cost_usd": 80,
            }
        },
    )
    retention = load_settings(path, environ={}).projects["math-prover"].datasets["b"]
    assert retention.write_once == ("runs/", "models/")
    assert retention.expire_prefixes == {"tmp/": 30}
    assert retention.expire_noncurrent_days == 90
    assert retention.versioning is True
    assert retention.prefix == "data/"


@pytest.mark.parametrize(
    "retention",
    [
        {"write_once": ["runs/"]},
        {"expire_prefixes": {"tmp/": 1}},
        {"expire_noncurrent_days": 30, "versioning": True},
    ],
)
def test_central_retention_must_name_the_dataset_prefix(
    tmp_path: Path, retention: dict[str, object]
) -> None:
    path = _with_projects(tmp_path, {"p": {"datasets": {"b": retention}}})
    with pytest.raises(ConfigError, match=r"prefix"):
        load_settings(path, environ={})
    path = _with_projects(tmp_path, {"p": {"datasets": {"b": {**retention, "prefix": "data/"}}}})
    assert load_settings(path, environ={}).projects["p"].datasets["b"].prefix == "data/"


def test_an_empty_central_entry_needs_no_prefix(settings: Settings) -> None:
    entry = settings.projects["rusty-chess"].datasets["jwall-engineering-rusty-chess-nnue"]
    assert entry.prefix is None


@pytest.mark.parametrize(
    "retention",
    [
        {"prefix": "/data/"},
        {"prefix": "data"},
        {"prefix": "data/", "write_once": ["/runs/"]},
        {"prefix": "data/", "write_once": ["runs"]},
        {"prefix": "data/", "expire_prefixes": {"/tmp/": 1}},
        {"prefix": "data/", "expire_prefixes": {"tmp": 1}},
    ],
)
def test_central_prefixes_have_the_component_format(
    tmp_path: Path, retention: dict[str, object]
) -> None:
    path = _with_projects(tmp_path, {"p": {"datasets": {"b": retention}}})
    with pytest.raises(ConfigError, match=r"prefix|sub-prefix"):
        load_settings(path, environ={})


def _databricks_raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((FIXTURES / "config.yaml").read_text())
    return raw


def _load_raw(tmp_path: Path, raw: dict[str, Any]) -> Settings:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_settings(path, environ={})


def _second_project(raw: dict[str, Any], name: str, cidr: str) -> None:
    projects = raw["projects"]
    entry = yaml.safe_load(yaml.safe_dump(projects["databricks-poc"]))
    entry["databricks"]["vpc_cidr"] = cidr
    projects[name] = entry


def test_databricks_section_parses(settings: Settings) -> None:
    section = settings.databricks
    assert section is not None
    assert section.enabled is False
    assert section.has_placeholder_ids is True
    assert section.secret_parameter == "/jwall-engineering/databricks/client-secret"
    assert section.metastore_name == "jwall-engineering-us-east-1"
    assert section.governance_project == "databricks-poc"
    assert section.audit_bucket == "jwall-engineering-databricks-audit-logs"
    assert section.events_prefix == "events/"
    assert section.readiness_timeout_seconds == 1200
    assert set(section.compute_policies) == {"small-interactive", "jobs"}
    small = section.compute_policies["small-interactive"].definition
    assert small["num_workers"] == {"type": "range", "maxValue": 2}
    assert small["autotermination_minutes"] == {"type": "fixed", "value": 30, "hidden": False}
    assert small["node_type_id"] == {"type": "allowlist", "values": ["m7g.large", "m7g.xlarge"]}
    assert small["aws_attributes.availability"]["value"] == "SPOT_WITH_FALLBACK"
    assert section.compute_policies["jobs"].definition["cluster_type"] == {
        "type": "fixed",
        "value": "job",
    }
    poc = settings.projects["databricks-poc"].databricks
    assert poc is not None
    assert poc.compute_policies == ("small-interactive", "jobs")
    assert poc.cost_center == "cc-0000"
    assert poc.vpc_cidr == "10.40.0.0/16"
    assert poc.users == ()


def test_real_ids_are_not_placeholders(tmp_path: Path) -> None:
    raw = _databricks_raw()
    raw["databricks"]["account_id"] = "11111111-2222-3333-4444-555555555555"
    raw["databricks"]["client_id"] = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    section = _load_raw(tmp_path, raw).databricks
    assert section is not None
    assert section.has_placeholder_ids is False


def test_state_key_with_stack(settings: Settings) -> None:
    assert (
        settings.state_key("databricks-poc", "prod", "account")
        == "project=databricks-poc/env=prod/databricks-poc-prod-account.tfstate"
    )


def test_state_key_without_stack_unchanged(settings: Settings) -> None:
    assert (
        settings.state_key("databricks-poc", "prod")
        == "project=databricks-poc/env=prod/databricks-poc-prod.tfstate"
    )


def test_policy_name_must_be_kebab(tmp_path: Path) -> None:
    raw = _databricks_raw()
    policies = raw["databricks"]["compute_policies"]
    policies["Small_Interactive"] = policies.pop("small-interactive")
    raw["projects"]["databricks-poc"]["databricks"]["compute_policies"] = [
        "Small_Interactive",
        "jobs",
    ]
    with pytest.raises(ConfigError, match="Small_Interactive"):
        _load_raw(tmp_path, raw)


def test_policy_must_set_autotermination(tmp_path: Path) -> None:
    raw = _databricks_raw()
    del raw["databricks"]["compute_policies"]["jobs"]["definition"]["autotermination_minutes"]
    with pytest.raises(ConfigError, match=r"jobs.*autotermination_minutes"):
        _load_raw(tmp_path, raw)


def test_project_policy_must_exist_centrally(tmp_path: Path) -> None:
    raw = _databricks_raw()
    raw["projects"]["databricks-poc"]["databricks"]["compute_policies"] = ["ghost-policy"]
    with pytest.raises(ConfigError, match="ghost-policy"):
        _load_raw(tmp_path, raw)


def test_governance_project_must_have_databricks_guardrails(tmp_path: Path) -> None:
    raw = _databricks_raw()
    raw["databricks"]["governance_project"] = "rusty-chess"
    with pytest.raises(ConfigError, match=r"governance_project.*rusty-chess"):
        _load_raw(tmp_path, raw)


def test_overlapping_vpc_cidrs_rejected(tmp_path: Path) -> None:
    raw = _databricks_raw()
    _second_project(raw, "team-a", "10.40.128.0/17")
    with pytest.raises(ConfigError, match=r"databricks-poc.*team-a|team-a.*databricks-poc"):
        _load_raw(tmp_path, raw)


def test_disjoint_vpc_cidrs_accepted(tmp_path: Path) -> None:
    raw = _databricks_raw()
    _second_project(raw, "team-a", "10.41.0.0/16")
    assert _load_raw(tmp_path, raw).projects["team-a"].databricks is not None


def test_vpc_cidr_overlapping_bot_vpc_rejected(tmp_path: Path) -> None:
    raw = _databricks_raw()
    raw["projects"]["databricks-poc"]["databricks"]["vpc_cidr"] = "10.0.0.0/20"
    with pytest.raises(ConfigError, match=r"10\.0\.0\.0/16"):
        _load_raw(tmp_path, raw)


@pytest.mark.parametrize("cidr", ["10.40.0.0/15", "10.40.0.0/21", "10.40.0.1/16", "nonsense"])
def test_vpc_cidr_must_be_a_16_to_20_network(tmp_path: Path, cidr: str) -> None:
    raw = _databricks_raw()
    raw["projects"]["databricks-poc"]["databricks"]["vpc_cidr"] = cidr
    with pytest.raises(ConfigError, match="vpc_cidr"):
        _load_raw(tmp_path, raw)


def test_databricks_project_name_limited_to_21_characters(tmp_path: Path) -> None:
    raw = _databricks_raw()
    long_name = "a" * 22
    _second_project(raw, long_name, "10.41.0.0/16")
    with pytest.raises(ConfigError, match=rf"{long_name}.*21"):
        _load_raw(tmp_path, raw)


def test_project_databricks_requires_section(tmp_path: Path) -> None:
    raw = _databricks_raw()
    del raw["databricks"]
    with pytest.raises(ConfigError, match=r"databricks-poc.*databricks"):
        _load_raw(tmp_path, raw)


def test_member_emails_must_look_like_emails(tmp_path: Path) -> None:
    raw = _databricks_raw()
    raw["projects"]["databricks-poc"]["databricks"]["admins"] = ["not-an-email"]
    with pytest.raises(ConfigError, match="not-an-email"):
        _load_raw(tmp_path, raw)
