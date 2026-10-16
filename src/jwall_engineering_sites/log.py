"""Logging: console at the configured level, a debug log file always, and
GitHub Actions groups and annotations when running there.

The job log is the primary log. The debug file is for "what did the tool
actually run" after the fact; the reusable workflows upload it as an artifact.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

LOGGER_NAME = "jwall-engineering-sites"
ARTIFACTS_DIR = Path(".jwall-engineering-sites")
LOG_FILE_NAME = "jwall-engineering-sites.log"


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


class _ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        prefix = "" if record.levelno == logging.INFO else f"{record.levelname.lower()}: "
        return f"{prefix}{record.getMessage()}"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }
        phase = getattr(record, "phase", None)
        if phase:
            payload["phase"] = phase
        return json.dumps(payload)


class _FileFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(message)s")


def artifacts_dir(base: Path | None = None) -> Path:
    """Where run artifacts (plan text, comment files, the debug log) go."""
    directory = (base or Path.cwd()) / ARTIFACTS_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def setup_logging(
    level: Literal["debug", "info", "warning", "error"],
    fmt: Literal["console", "json"],
    debug_file: Path | None,
) -> logging.Logger:
    """Configure the tool logger. Safe to call more than once (tests)."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper()))
    console.setFormatter(_JsonFormatter() if fmt == "json" else _ConsoleFormatter())
    logger.addHandler(console)

    if debug_file is not None:
        debug_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(debug_file, mode="w", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(_FileFormatter())
        logger.addHandler(file_handler)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def _emit_workflow_command(command: str, message: str = "", **params: str) -> None:
    """Print a GitHub Actions workflow command, bypassing the logger so the
    runner sees it on its own line."""
    sys.stdout.flush()
    props = ",".join(f"{key}={value}" for key, value in params.items() if value)
    header = f"::{command} {props}" if props else f"::{command}"
    print(f"{header}::{message}", flush=True)


@contextmanager
def phase(name: str) -> Iterator[None]:
    """A log group per phase: preflight, init, plan, apply, sync, invalidate."""
    logger = get_logger()
    if in_github_actions():
        _emit_workflow_command("group", name)
    else:
        logger.info("== %s", name)
    logger.debug("phase %s started", name)
    try:
        yield
    finally:
        logger.debug("phase %s finished", name)
        if in_github_actions():
            _emit_workflow_command("endgroup")


def annotate_error(message: str, title: str = "jwall-engineering-sites") -> None:
    """An `::error::` annotation so the failure shows on the job summary."""
    if in_github_actions():
        _emit_workflow_command("error", message.replace("\n", "%0A"), title=title)


def annotate_warning(message: str, title: str = "jwall-engineering-sites") -> None:
    if in_github_actions():
        _emit_workflow_command("warning", message.replace("\n", "%0A"), title=title)
