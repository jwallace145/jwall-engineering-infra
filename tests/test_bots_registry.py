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


def _yaml(path: str) -> dict[Any, Any]:
    """A workflow as data. PyYAML reads the bare `on:` key as True, hence Any keys."""
    import yaml

    data: dict[Any, Any] = yaml.safe_load((REPO_ROOT / path).read_text())
    return data


def test_workflow_matrices_list_every_bot() -> None:
    ci: dict[Any, Any] = _yaml(".github/workflows/ci.yml")
    plan_projects = {i["project"] for i in ci["jobs"]["bot-plan"]["strategy"]["matrix"]["include"]}
    apply: dict[Any, Any] = _yaml(".github/workflows/bot-apply.yml")
    # PyYAML reads the bare `on:` key as True.
    options = set(apply[True]["workflow_dispatch"]["inputs"]["project"]["options"])
    assert plan_projects == set(registry()) == options


def test_bot_plan_and_apply_use_the_bots_roles() -> None:
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    apply = (REPO_ROOT / ".github/workflows/bot-apply.yml").read_text()
    assert ':role/${organization}-${PROJECT}-ci-plan"' in ci
    assert ':role/${organization}-${PROJECT}-ci-apply"' in apply
    assert "environment:" in apply and "name: prod" in apply


def test_container_release_runs_this_commit_and_pins_nothing() -> None:
    text = (REPO_ROOT / ".github/workflows/container-release.yml").read_text()
    assert "ref: ${{ github.job_workflow_sha }}" in text
    assert "@v0." not in text and "x-release-please-version" not in text
    assert "bot reconcile --version" in text
    assert "--config .jwall-engineering-infra/config.yaml" in text
    release: dict[Any, Any] = _yaml(".github/workflows/container-release.yml")
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


WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def test_no_workflow_needs_account_or_role_variables() -> None:
    """Role ARNs are derived from config.yaml's account and the project name."""
    for path in WORKFLOWS_DIR.glob("*.yml"):
        text = path.read_text()
        assert "vars.AWS_ACCOUNT_ID" not in text, path.name
        assert "AWS_RELEASE_ROLE_ARN" not in text, path.name


def test_bot_workflows_read_the_account_from_config() -> None:
    for name in ("ci.yml", "bot-apply.yml", "container-release.yml"):
        assert ".aws.account_id" in (WORKFLOWS_DIR / name).read_text(), name


def test_container_release_role_is_optional() -> None:
    release: dict[Any, Any] = _yaml(".github/workflows/container-release.yml")
    role = release[True]["workflow_call"]["inputs"]["release-role-arn"]
    assert role["required"] is False
    assert role["default"] == ""


def test_bot_plan_runs_without_a_variable_and_skips_until_roles_exist() -> None:
    ci: dict[Any, Any] = _yaml(".github/workflows/ci.yml")
    job = ci["jobs"]["bot-plan"]
    assert "if" not in job, "no repository variable gates the job any more"
    steps = {s.get("id"): s for s in job["steps"] if "id" in s}
    assert steps["assume"]["continue-on-error"] is True
    gated = [s for s in job["steps"] if "jwall-engineering-sites" in str(s.get("run", ""))]
    assert gated and all("steps.assume.outcome == 'success'" in s["if"] for s in gated)


def test_foundation_takes_the_account_from_config() -> None:
    state = (REPO_ROOT / "foundation" / "state.tf").read_text()
    assert "650503560686" not in state
    assert "local.account_id" in state


def _module(name: str) -> str:
    """The text of one module block in foundation/bots.tf."""
    text = BOTS_TF.read_text()
    match = re.search(rf'^module "{name}" \{{\n(.*?)^\}}', text, re.MULTILINE | re.DOTALL)
    assert match, f"module {name} not found in bots.tf"
    return match.group(1)


def _policies(block: str) -> list[str]:
    # The first list: the policies every bot gets, before any concat()ed extras.
    match = re.search(r"^\s*policies\s*=\s*(?:concat\(\s*)?\[([^\]]*)\]", block, re.MULTILINE)
    assert match, "no policies list"
    return re.findall(r'"([^"]+)"', match.group(1))


def _task_types() -> dict[str, list[str]]:
    text = BOTS_TF.read_text()
    match = re.search(r"bot_task_types\s*=\s*\{(.*?)\n  \}", text, re.DOTALL)
    assert match, "local.bot_task_types not found"
    return {
        key: re.findall(r'"([^"]+)"', types)
        for key, types in re.findall(r"^\s+([a-z0-9-]+)\s*=\s*\[([^\]]*)\]", match.group(1), re.M)
    }


def test_bot_plan_and_apply_carry_the_task_policies() -> None:
    assert "task/read" in _policies(_module("bot_ci_plan"))
    assert "task/manage" not in _policies(_module("bot_ci_plan"))
    assert {"task/read", "task/manage"} <= set(_policies(_module("bot_ci_apply")))


def test_task_role_is_the_launch_role_for_the_bot_repository() -> None:
    block = _module("bot_ci_task")
    assert 'source   = "jwallace145/jwall-engineering/aws//modules/ci-task-role"' in block
    assert "for_each = local.bot_task_types" in block
    assert "repository        = local.bots[each.key].repository" in block
    assert "repository_id     = local.bots[each.key].repository_id" in block
    assert _policies(block) == ["task/launch"]
    assert "instance_types = each.value, profiles = local.bot_task_profiles[each.key]" in block
    # No subjects override: the module's default trusts pull_request and main only.
    assert "subjects" not in block


def _task_runner_fixture() -> dict[str, Any]:
    import yaml

    fixture = yaml.safe_load((FIXTURES / "bots" / "rusty-chess.yaml").read_text())
    runner: dict[str, Any] = next(c for c in fixture["components"] if c["kind"] == "task-runner")
    return runner


def test_task_instance_types_match_the_project_file() -> None:
    runner = _task_runner_fixture()
    expected = list(runner["instance_types"])
    for profile in runner.get("profiles", {}).values():
        expected += [t for t in profile["instance_types"] if t not in expected]
    assert _task_types() == {"rusty-chess": expected}
    assert set(_task_types()) <= set(registry())


def _task_profiles() -> dict[str, list[str]]:
    text = BOTS_TF.read_text()
    match = re.search(r"bot_task_profiles\s*=\s*\{(.*?)\n  \}", text, re.DOTALL)
    assert match, "local.bot_task_profiles not found"
    return {
        key: re.findall(r'"([^"]+)"', names)
        for key, names in re.findall(r"^\s+([a-z0-9-]+)\s*=\s*\[([^\]]*)\]", match.group(1), re.M)
    }


def test_task_profiles_match_the_project_file() -> None:
    runner = _task_runner_fixture()
    assert _task_profiles() == {"rusty-chess": ["cpu", *runner.get("profiles", {})]}
    assert set(_task_profiles()) == set(_task_types())


def test_bots_output_names_the_task_role() -> None:
    outputs = (REPO_ROOT / "foundation" / "outputs.tf").read_text()
    assert "task_role_arn" in outputs and "module.bot_ci_task[key].role_arn" in outputs


def _bot_datasets() -> dict[str, list[str]]:
    text = BOTS_TF.read_text()
    match = re.search(r"bot_datasets\s*=\s*\{(.*?)\n  \}", text, re.DOTALL)
    assert match, "local.bot_datasets not found"
    return {
        key: re.findall(r'"([^"]+)"', buckets)
        for key, buckets in re.findall(r"^\s+([a-z0-9-]+)\s*=\s*\[([^\]]*)\]", match.group(1), re.M)
    }


def test_dataset_buckets_match_the_project_file() -> None:
    import yaml

    fixture = yaml.safe_load((FIXTURES / "bots" / "rusty-chess.yaml").read_text())
    buckets = [c["bucket"] for c in fixture["components"] if c["kind"] == "dataset"]
    assert _bot_datasets() == {"rusty-chess": buckets}
    assert set(_bot_datasets()) <= set(registry())


def test_bot_plan_and_apply_carry_the_dataset_policies() -> None:
    plan, apply = _module("bot_ci_plan"), _module("bot_ci_apply")
    assert '["dataset/read"]' in plan and "dataset/manage" not in plan
    assert '["dataset/read", "dataset/manage"]' in apply
    for block in (plan, apply):
        assert "contains(keys(local.bot_datasets), each.key)" in block
        assert "bucket_names = local.bot_datasets[each.key]" in block
