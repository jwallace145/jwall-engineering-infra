"""The foundation's bot registry and the bot project files agree."""

from __future__ import annotations

import re
from typing import Any

from tests.conftest import FIXTURES, REPO_ROOT

BOTS_TF = REPO_ROOT / "foundation" / "bots.tf"
_ENTRY = re.compile(
    r'^\s+([a-z0-9-]+)\s*=\s*\{\s*repository\s*=\s*"([^"]+)",\s*repository_id\s*=\s*"(\d+)"\s*\}',
    re.MULTILINE,
)


def registry() -> dict[str, tuple[str, str]]:
    return {m[0]: (m[1], m[2]) for m in _ENTRY.findall(BOTS_TF.read_text())}


def test_registry_lists_rusty_chess() -> None:
    assert registry() == {"rusty-chess": ("jwallace145/rusty-chess-v2", "1435162673")}


def test_registry_matches_the_bot_fixture() -> None:
    fixture = (FIXTURES / "bots" / "rusty-chess.yaml").read_text()
    for project, (repository, _) in registry().items():
        assert f"project: {project}\n" in fixture
        assert f"repository: {repository}\n" in fixture


def test_bot_state_prefixes_are_in_the_state_bucket_policy() -> None:
    state = (REPO_ROOT / "foundation" / "state.tf").read_text()
    assert "local.bots" in state and "project=${key}/" in state
    assert "module.bot_ci_plan[key].role_arn" in state
    assert "module.bot_ci_apply[key].role_arn" in state


def _yaml(path: str) -> dict[str, object]:
    import yaml

    data: dict[str, object] = yaml.safe_load((REPO_ROOT / path).read_text())
    return data


def test_workflow_matrices_list_every_bot() -> None:
    ci: dict[str, Any] = _yaml(".github/workflows/ci.yml")  # type: ignore[assignment]
    plan_projects = {i["project"] for i in ci["jobs"]["bot-plan"]["strategy"]["matrix"]["include"]}
    apply: dict[str, Any] = _yaml(".github/workflows/bot-apply.yml")  # type: ignore[assignment]
    # PyYAML reads the bare `on:` key as True.
    options = set(apply[True]["workflow_dispatch"]["inputs"]["project"]["options"])
    assert plan_projects == set(registry()) == options


def test_bot_plan_and_apply_use_the_bots_roles() -> None:
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    apply = (REPO_ROOT / ".github/workflows/bot-apply.yml").read_text()
    assert "role/jwall-engineering-${{ matrix.project }}-ci-plan" in ci
    assert "role/jwall-engineering-${{ inputs.project }}-ci-apply" in apply
    assert "environment:" in apply and "name: prod" in apply


def test_container_release_runs_this_commit_and_pins_nothing() -> None:
    text = (REPO_ROOT / ".github/workflows/container-release.yml").read_text()
    assert "ref: ${{ github.job_workflow_sha }}" in text
    assert "@v0." not in text and "x-release-please-version" not in text
    assert "bot reconcile --version" in text
    assert "--config .jwall-engineering-infra/config.yaml" in text
    release: dict[str, Any] = _yaml(".github/workflows/container-release.yml")  # type: ignore[assignment]
    assert release["permissions"] == {}
    job = release["jobs"]["release"]
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert release["concurrency"]["cancel-in-progress"] is False
    assert set(release[True]["workflow_call"]["inputs"]) == {
        "project",
        "version",
        "release-role-arn",
        "debug",
    }
