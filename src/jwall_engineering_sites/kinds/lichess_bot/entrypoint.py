"""Entrypoint of the lichess-bot image built by jwall-engineering-sites.

Renders lichess-bot's config.yml from the consumer's versioned template and
the environment, then execs lichess-bot. Runs inside the image as the
non-root `bot` user, with Python and PyYAML from the image (lichess-bot's own
requirement). The token arrives as LICHESS_TOKEN from the instance (read from
SSM at container start) and is only ever written to a 0600 file in the
container.

What it enforces, whatever the template says:
  - token from LICHESS_TOKEN (exit 64 without it);
  - the engine is /opt/engine/$ENGINE_BINARY;
  - UCI options Hash and Move Overhead from ENGINE_HASH_MB and
    ENGINE_MOVE_OVERHEAD_MS, but only those the engine advertises: python-chess
    rejects unknown options, and older engine releases lack Move Overhead;
  - quit_after_all_games_finish: true, so one SIGINT lets the game in
    progress finish before lichess-bot exits (graceful rollouts depend on it);
  - one PGN per game in /opt/lichess-bot/game_records (the instance ships
    them to S3 from there).
"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

TEMPLATE_PATH = Path("/opt/lichess-bot/config.template.yml")
CONFIG_PATH = Path("/opt/lichess-bot/config.yml")
ENGINE_DIR = Path("/opt/engine")
LICHESS_BOT = "lichess-bot.py"
RECORDS_DIR = "/opt/lichess-bot/game_records"
EXIT_CONFIG = 64


def warn(message: str) -> None:
    print(f"entrypoint: {message}", file=sys.stderr, flush=True)


def engine_options(binary: Path, timeout: float = 15.0) -> set[str]:
    """Names of the UCI options the engine advertises (empty if it cannot say)."""
    try:
        completed = subprocess.run(
            [str(binary)],
            input="uci\nquit\n",
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warn(f"could not ask {binary} for its UCI options: {exc}")
        return set()
    names = set()
    for line in completed.stdout.splitlines():
        if line.startswith("option name ") and " type " in line:
            names.add(line[len("option name ") : line.index(" type ")])
    return names


def render_config(
    template: Mapping[str, Any], env: Mapping[str, str], options: set[str]
) -> dict[str, Any]:
    config: dict[str, Any] = copy.deepcopy(dict(template))
    token = env.get("LICHESS_TOKEN", "")
    if not token:
        warn("LICHESS_TOKEN is not set; the instance reads it from SSM at container start")
        raise SystemExit(EXIT_CONFIG)
    config["token"] = token

    engine = dict(config.get("engine") or {})
    engine["dir"] = str(ENGINE_DIR)
    engine["name"] = env.get("ENGINE_BINARY", engine.get("name", ""))
    wanted: dict[str, Any] = dict(engine.get("uci_options") or {})
    if env.get("ENGINE_HASH_MB"):
        wanted["Hash"] = int(env["ENGINE_HASH_MB"])
    if env.get("ENGINE_MOVE_OVERHEAD_MS"):
        wanted["Move Overhead"] = int(env["ENGINE_MOVE_OVERHEAD_MS"])
    kept = {name: value for name, value in wanted.items() if name in options}
    for name in sorted(set(wanted) - set(kept)):
        warn(f"the engine does not advertise UCI option {name!r}; not setting it")
    engine["uci_options"] = kept
    config["engine"] = engine

    if config.get("quit_after_all_games_finish") is not True:
        warn("forcing quit_after_all_games_finish: true (rollouts wait for the game in progress)")
    config["quit_after_all_games_finish"] = True
    config["pgn_directory"] = RECORDS_DIR
    config["pgn_file_grouping"] = "game"
    return config


def write_yaml(path: Path, data: Mapping[str, Any]) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        yaml.safe_dump(dict(data), handle, sort_keys=False)
    os.chmod(path, 0o600)


def main() -> None:
    template = yaml.safe_load(TEMPLATE_PATH.read_text()) or {}
    binary = ENGINE_DIR / os.environ.get("ENGINE_BINARY", "")
    config = render_config(template, os.environ, engine_options(binary))
    write_yaml(CONFIG_PATH, config)
    os.execvp(
        sys.executable,
        [sys.executable, LICHESS_BOT, "--config", str(CONFIG_PATH), "--disable_auto_logging"],
    )


if __name__ == "__main__":
    main()
