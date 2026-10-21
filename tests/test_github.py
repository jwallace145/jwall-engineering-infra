from __future__ import annotations

import pytest

from jwall_engineering_sites import github


def test_commit_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    assert github.commit_url("abc") == "https://github.com/o/r/commit/abc"


def test_commit_url_outside_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    assert github.commit_url("abc") is None
    assert github.commit_url(None) is None


def test_provenance_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "7")
    monkeypatch.setenv("JES_HEAD_SHA", "abc")
    provenance = github.provenance("1.2.3")
    assert provenance.version == "1.2.3"
    assert provenance.sha == "abc"
    assert provenance.commit_url == "https://github.com/o/r/commit/abc"
    assert provenance.run_url == "https://github.com/o/r/actions/runs/7"
