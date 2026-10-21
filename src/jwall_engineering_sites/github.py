"""GitHub Actions outputs and the step summary. No-ops outside Actions."""

from __future__ import annotations

import os
from pathlib import Path

from .report import Provenance


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


def commit_url(sha: str | None) -> str | None:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    if sha and server and repository:
        return f"{server}/{repository}/commit/{sha}"
    return None


def provenance(version: str) -> Provenance:
    """What the comments' footer says about this run."""
    sha = head_sha()
    return Provenance(version=version, sha=sha, commit_url=commit_url(sha), run_url=run_url())
