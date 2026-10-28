"""The lichess-bot image entrypoint: config rendering and the engine probe."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from jwall_engineering_sites.kinds.lichess_bot import entrypoint

TEMPLATE: dict[str, Any] = {
    "token": "LICHESS_TOKEN_PLACEHOLDER",
    "url": "https://lichess.org/",
    "engine": {"dir": "./engines/", "name": "rusty-chess", "protocol": "uci",
               "uci_options": {"Hash": 16}},
    "quit_after_all_games_finish": False,
    "pgn_directory": "game_records",
    "challenge": {"concurrency": 1},
}  # fmt: skip

ENV = {
    "LICHESS_TOKEN": "lip_secret",
    "ENGINE_BINARY": "rusty-chess",
    "ENGINE_HASH_MB": "128",
    "ENGINE_MOVE_OVERHEAD_MS": "500",
}

BOTH = {"Hash", "Move Overhead"}


def test_token_and_engine_options_are_set() -> None:
    config = entrypoint.render_config(TEMPLATE, ENV, BOTH)
    assert config["token"] == "lip_secret"
    assert config["engine"]["uci_options"] == {"Hash": 128, "Move Overhead": 500}
    assert config["engine"]["dir"] == "/opt/engine"
    assert config["engine"]["name"] == "rusty-chess"
    assert config["challenge"] == {"concurrency": 1}
    assert TEMPLATE["token"] == "LICHESS_TOKEN_PLACEHOLDER", "the template is not mutated"


def test_records_and_graceful_stop_are_forced(capsys: pytest.CaptureFixture[str]) -> None:
    config = entrypoint.render_config(TEMPLATE, ENV, BOTH)
    assert config["quit_after_all_games_finish"] is True
    assert config["pgn_directory"] == "/opt/lichess-bot/game_records"
    assert config["pgn_file_grouping"] == "game"
    assert "quit_after_all_games_finish" in capsys.readouterr().err


def test_options_the_engine_does_not_advertise_are_left_out(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = entrypoint.render_config(TEMPLATE, ENV, {"Hash"})
    assert config["engine"]["uci_options"] == {"Hash": 128}
    assert "Move Overhead" in capsys.readouterr().err


def test_template_options_the_engine_lacks_are_dropped() -> None:
    template = dict(TEMPLATE, engine=dict(TEMPLATE["engine"], uci_options={"Threads": 2}))
    config = entrypoint.render_config(template, ENV, BOTH)
    assert "Threads" not in config["engine"]["uci_options"]


def test_missing_token_exits_64() -> None:
    env = {k: v for k, v in ENV.items() if k != "LICHESS_TOKEN"}
    with pytest.raises(SystemExit) as exc:
        entrypoint.render_config(TEMPLATE, env, BOTH)
    assert exc.value.code == 64


def test_placeholder_never_survives() -> None:
    config = entrypoint.render_config(TEMPLATE, ENV, BOTH)
    assert "LICHESS_TOKEN_PLACEHOLDER" not in repr(config)


def _fake_engine(tmp_path: Path, options: list[str]) -> Path:
    script = tmp_path / "engine"
    lines = "".join(f'echo "option name {o} type spin default 1 min 0 max 9"\n' for o in options)
    script.write_text(
        "#!/bin/sh\nread line\necho 'id name fake'\n" + lines + "echo uciok\nread line\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_engine_options_are_probed(tmp_path: Path) -> None:
    engine = _fake_engine(tmp_path, ["Hash", "Move Overhead"])
    assert entrypoint.engine_options(engine) == {"Hash", "Move Overhead"}


def test_engine_probe_failure_means_no_options(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.write_text("#!/bin/sh\nexit 3\n")
    broken.chmod(0o755)
    assert entrypoint.engine_options(broken) == set()


def test_main_writes_a_private_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _fake_engine(tmp_path, ["Hash"])
    template = tmp_path / "config.template.yml"
    entrypoint.write_yaml(template, TEMPLATE)
    target = tmp_path / "config.yml"
    executed: list[list[str]] = []
    monkeypatch.setattr(os, "execvp", lambda f, a: executed.append(a))
    monkeypatch.setattr(entrypoint, "TEMPLATE_PATH", template)
    monkeypatch.setattr(entrypoint, "CONFIG_PATH", target)
    monkeypatch.setattr(entrypoint, "ENGINE_DIR", tmp_path)
    monkeypatch.setattr(os, "environ", {**ENV, "ENGINE_BINARY": engine.name})
    entrypoint.main()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert "lip_secret" in target.read_text()
    assert executed and executed[0][-3:] == ["--config", str(target), "--disable_auto_logging"]
