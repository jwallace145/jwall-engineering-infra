"""The foundation's bot registry and the bot project files agree."""

from __future__ import annotations

import re

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
