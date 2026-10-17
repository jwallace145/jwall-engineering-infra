from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jwall_engineering_sites.errors import ConfigError
from jwall_engineering_sites.settings import Settings, load_settings, parse_duration
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
