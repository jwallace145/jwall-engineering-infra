"""The components schema: container-image and lichess-bot projects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jwall_engineering_sites.errors import ConfigError
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings
from jwall_engineering_sites.site import load_site_config
from tests.conftest import FIXTURES

BOT_FILE = FIXTURES / "bots" / "rusty-chess.yaml"


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(BOT_FILE.read_text())
    return raw


def _bot(raw: dict[str, Any]) -> dict[str, Any]:
    component: dict[str, Any] = next(c for c in raw["components"] if c["kind"] == "lichess-bot")
    return component


def _image(raw: dict[str, Any]) -> dict[str, Any]:
    component: dict[str, Any] = next(c for c in raw["components"] if c["kind"] == "container-image")
    return component


def _load(tmp_path: Path, raw: dict[str, Any], settings: Settings) -> Project:
    path = tmp_path / "jwall-engineering-sites.yaml"
    path.write_text(yaml.safe_dump(raw))
    return Project.from_config(load_site_config(path), settings)


def test_bot_fixture_loads(bot: Project) -> None:
    assert bot.kind == "bot"
    assert bot.project == "rusty-chess"
    assert bot.image_repository == "jwall-engineering-rusty-chess"
    assert bot.bot_name == "jwall-engineering-rusty-chess-bot"
    assert bot.records_bucket == "jwall-engineering-rusty-chess-records"
    assert bot.vpc_name == "jwall-engineering-rusty-chess"
    assert bot.log_group == "/jwall-engineering/rusty-chess/bot"
    assert bot.parameter_prefix == "/jwall-engineering/rusty-chess/bot"
    assert bot.parameter("desired-version") == "/jwall-engineering/rusty-chess/bot/desired-version"
    assert bot.state_key == "project=rusty-chess/env=prod/rusty-chess-prod.tfstate"
    assert bot.role_name("release") == "jwall-engineering-rusty-chess-ci-release"
    assert bot.role_name("apply") == "jwall-engineering-rusty-chess-ci-apply"
    assert bot.profile_url == "https://lichess.org/@/rusty-chess-bot"
    assert bot.bot.engine.hash_mb == 128
    assert bot.bot.engine.move_overhead_ms == 500
    assert bot.bot.secrets == {"LICHESS_TOKEN": "/jwall-engineering/rusty-chess/lichess-token"}
    assert bot.image.artifact.binary == "rusty-chess"
    assert bot.image.architecture == "x86_64"
    assert bot.zone_id is None


def test_site_accessors_refuse_on_a_bot(bot: Project) -> None:
    with pytest.raises(ConfigError, match="not a site"):
        _ = bot.site_bucket


def test_bot_accessors_refuse_on_a_site(settings: Settings) -> None:
    site = Project.from_config(
        load_site_config(FIXTURES / "sites" / "guitar-knowledge-base.yaml"), settings
    )
    assert site.kind == "site"
    with pytest.raises(ConfigError, match="not a bot"):
        _ = site.bot


def test_unknown_field_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["extra"] = 1
    with pytest.raises(ConfigError, match="extra"):
        _load(tmp_path, raw, settings)


def test_secret_outside_project_path_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["secrets"] = {"LICHESS_TOKEN": "/other/token"}
    with pytest.raises(ConfigError, match="must start with /jwall-engineering/rusty-chess/"):
        _load(tmp_path, raw, settings)


def test_secret_under_bot_prefix_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["secrets"] = {"LICHESS_TOKEN": "/jwall-engineering/rusty-chess/bot/token"}
    with pytest.raises(ConfigError, match="bot/"):
        _load(tmp_path, raw, settings)


def test_missing_lichess_token_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["secrets"] = {"OTHER": "/jwall-engineering/rusty-chess/other"}
    with pytest.raises(ConfigError, match="LICHESS_TOKEN"):
        _load(tmp_path, raw, settings)


def test_two_bots_fail(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"].append(dict(_bot(raw)))
    with pytest.raises(ConfigError, match="exactly one lichess-bot"):
        _load(tmp_path, raw, settings)


def test_bot_without_image_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [_bot(raw)]
    with pytest.raises(ConfigError, match="exactly one container-image"):
        _load(tmp_path, raw, settings)


def test_components_with_domain_fail(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["domain"] = "chess.jwall.engineering"
    with pytest.raises(ConfigError, match="either"):
        _load(tmp_path, raw, settings)


def test_site_without_site_section_fails(tmp_path: Path, settings: Settings) -> None:
    raw: dict[str, Any] = yaml.safe_load(
        (FIXTURES / "sites" / "guitar-knowledge-base.yaml").read_text()
    )
    del raw["site"]
    with pytest.raises(ConfigError, match="either"):
        _load(tmp_path, raw, settings)


def test_asset_needs_version_placeholder(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _image(raw)["artifact"]["asset"] = "rusty-chess-linux.tar.gz"
    with pytest.raises(ConfigError, match=r"\{version\}"):
        _load(tmp_path, raw, settings)


def test_binary_is_one_path_component(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _image(raw)["artifact"]["binary"] = "../rusty-chess"
    with pytest.raises(ConfigError, match="binary"):
        _load(tmp_path, raw, settings)


def test_config_must_be_a_relative_yaml_path(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["config"] = "/etc/lichess.yml"
    with pytest.raises(ConfigError, match="config"):
        _load(tmp_path, raw, settings)


def test_bad_retention_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["logs"] = {"retention_days": 31}
    with pytest.raises(ConfigError, match="retention_days"):
        _load(tmp_path, raw, settings)


def test_bot_tags_are_cross_checked(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["tags"]["Project"] = "other"
    with pytest.raises(ConfigError, match="Project must be 'rusty-chess'"):
        _load(tmp_path, raw, settings)


def test_bot_organization_tag_is_checked(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["tags"]["Organization"] = "someone-else"
    with pytest.raises(ConfigError, match=r"tags\.Organization"):
        _load(tmp_path, raw, settings)


def _without_task_runner(raw: dict[str, Any]) -> dict[str, Any]:
    raw["components"] = [c for c in raw["components"] if c["kind"] != "task-runner"]
    return raw


def _task_runner(raw: dict[str, Any]) -> dict[str, Any]:
    component: dict[str, Any] = next(c for c in raw["components"] if c["kind"] == "task-runner")
    return component


def test_task_runner_loads_and_names_derive(bot: Project) -> None:
    runner = bot.task_runner
    assert runner is not None
    assert runner.instance_types == ("c8a.xlarge", "c8a.2xlarge")
    assert runner.default_instance_type == "c8a.xlarge"
    assert (runner.max_hours, runner.max_concurrent, runner.results_days) == (6, 4, 7)
    assert runner.log_retention_days == 14
    assert bot.task_name == "jwall-engineering-rusty-chess-task"
    assert bot.task_template == "jwall-engineering-rusty-chess-task"
    assert bot.task_log_group == "/jwall-engineering/rusty-chess/tasks"
    assert bot.task_bucket == "jwall-engineering-rusty-chess-tasks"
    assert bot.task_prefix == "tasks/"
    assert bot.role_name("task") == "jwall-engineering-rusty-chess-ci-task"


def test_task_runner_defaults(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [c for c in raw["components"] if c["kind"] != "task-runner"]
    raw["components"].append({"kind": "task-runner"})
    runner = _load(tmp_path, raw, settings).task_runner
    assert runner is not None
    assert runner.instance_types == ("c8a.xlarge", "c8a.2xlarge")
    assert runner.default_instance_type == "c8a.xlarge"
    assert (runner.max_hours, runner.max_concurrent, runner.results_days) == (6, 4, 7)


def test_project_without_task_runner_loads(tmp_path: Path, settings: Settings) -> None:
    assert _load(tmp_path, _without_task_runner(_raw()), settings).task_runner is None


@pytest.mark.parametrize("bad", ["t3.large", "t4g.xlarge", "c8a xlarge", "C8A.xlarge"])
def test_task_runner_rejects_burstable_or_malformed_types(
    tmp_path: Path, settings: Settings, bad: str
) -> None:
    raw = _raw()
    _task_runner(raw)["instance_types"] = ["c8a.xlarge", bad]
    with pytest.raises(ConfigError, match="instance_types"):
        _load(tmp_path, raw, settings)


def test_task_runner_rejects_empty_types(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _task_runner(raw)["instance_types"] = []
    with pytest.raises(ConfigError, match="instance_types"):
        _load(tmp_path, raw, settings)


def test_task_runner_default_must_be_allowed(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _task_runner(raw)["default_instance_type"] = "c8a.4xlarge"
    with pytest.raises(ConfigError, match="default_instance_type"):
        _load(tmp_path, raw, settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_hours", 0), ("max_hours", 25), ("max_concurrent", 0), ("max_concurrent", 17),
     ("results_days", 0), ("results_days", 31), ("log_retention_days", 10)],
)  # fmt: skip
def test_task_runner_bounds(tmp_path: Path, settings: Settings, field: str, value: int) -> None:
    raw = _raw()
    _task_runner(raw)[field] = value
    with pytest.raises(ConfigError, match=field):
        _load(tmp_path, raw, settings)


def test_two_task_runners_fail(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"].append(dict(_task_runner(raw)))
    with pytest.raises(ConfigError, match="at most one task-runner"):
        _load(tmp_path, raw, settings)


def test_task_runner_alone_is_not_a_bot_project(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [_task_runner(raw)]
    with pytest.raises(ConfigError, match="exactly one container-image"):
        _load(tmp_path, raw, settings)
