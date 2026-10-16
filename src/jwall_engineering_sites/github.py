"""GitHub Actions outputs and the step summary. No-ops outside Actions."""

from __future__ import annotations

import os
from pathlib import Path


def write_output(name: str, value: str) -> None:
    """Append `name=value` to GITHUB_OUTPUT when it is set."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    if "\n" in value:
        raise ValueError(f"output {name} must be a single line")
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def append_summary(markdown: str) -> None:
    """Append Markdown to GITHUB_STEP_SUMMARY when it is set."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(markdown.rstrip("\n") + "\n\n")


def run_url() -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return None


def head_sha() -> str | None:
    return os.environ.get("JES_HEAD_SHA") or os.environ.get("GITHUB_SHA")
