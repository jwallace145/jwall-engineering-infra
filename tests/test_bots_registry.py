"""The foundation's bot registry and the bot project files agree."""

from __future__ import annotations

import re
from typing import Any

from jwall_engineering_sites.site import load_site_config
from tests.conftest import FIXTURES, REPO_ROOT

BOTS_TF = REPO_ROOT / "foundation" / "bots.tf"
_ENTRY = re.compile(
    r'^\s+([a-z0-9-]+)\s*=\s*\{\s*repository\s*=\s*"([^"]+)",\s*repository_id\s*=\s*"(\d+)"\s*\}',
    re.MULTILINE,
)


def registry() -> dict[str, tuple[str, str]]:
    return {m[0]: (m[1], m[2]) for m in _ENTRY.findall(BOTS_TF.read_text())}


def project_file(project: str) -> dict[str, Any]:
    """tests/fixtures/bots/<project>.yaml, the copy of the file onboarding adds."""
    import yaml

    path = FIXTURES / "bots" / f"{project}.yaml"
    assert path.is_file(), f"{project} is registered: add its project file as {path.name}"
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    return raw


def test_registry_lists_rusty_chess() -> None:
    assert registry()["rusty-chess"] == ("jwallace145/rusty-chess-v2", "1435162673")


def test_registry_matches_each_projects_file() -> None:
    for project, (repository, _) in registry().items():
        file = project_file(project)
        assert (file["project"], file["repository"]) == (project, repository)


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


_RESEARCH_CHOICE = re.compile(
    r"^\s*policies\s*=\s*concat\(\s*contains\(local\.research_projects, each\.key\)"
    r"\s*\?\s*\[([^\]]*)\]\s*:\s*\[([^\]]*)\]",
    re.MULTILINE,
)


def _research_policies(block: str) -> list[str]:
    """The base policies when the project is a research project."""
    match = _RESEARCH_CHOICE.search(block)
    assert match, "the role does not choose its base policies by research_projects"
    return re.findall(r'"([^"]+)"', match.group(1))


def _policies(block: str) -> list[str]:
    # A role choosing by kind: what a bot gets.
    choice = _RESEARCH_CHOICE.search(block)
    if choice:
        return re.findall(r'"([^"]+)"', choice.group(2))
    # The first list: the policies every bot gets, before any concat()ed extras.
    match = re.search(r"^\s*policies\s*=\s*(?:concat\(\s*)?\[([^\]]*)\]", block, re.MULTILINE)
    assert match, "no policies list"
    return re.findall(r'"([^"]+)"', match.group(1))


def _projects() -> dict[str, Any]:
    """The reviewed guardrails: config.yaml's `projects`, as foundation decodes it."""
    import yaml

    projects: dict[str, Any] = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())["projects"]
    return projects


def _task_types() -> dict[str, list[str]]:
    return {k: list(p["instance_types"]) for k, p in _projects().items() if p.get("instance_types")}


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


def _task_runners() -> dict[str, dict[str, Any]]:
    """Each registered project's task-runner component, if it has one."""
    runners: dict[str, dict[str, Any]] = {}
    for project in registry():
        for component in project_file(project)["components"]:
            if component["kind"] == "task-runner":
                runners[project] = component
    return runners


def _instance_types(runner: dict[str, Any]) -> list[str]:
    expected = list(runner["instance_types"])
    for profile in runner.get("profiles", {}).values():
        expected += [t for t in profile["instance_types"] if t not in expected]
    return expected


def test_task_instance_types_match_the_project_files() -> None:
    expected = {project: _instance_types(runner) for project, runner in _task_runners().items()}
    assert "rusty-chess" in expected
    assert _task_types() == expected
    assert set(_task_types()) <= set(registry())


def _task_profiles() -> dict[str, list[str]]:
    return {k: list(p["task_profiles"]) for k, p in _projects().items() if p.get("task_profiles")}


def test_task_profiles_match_the_project_files() -> None:
    expected = {
        project: ["cpu", *runner.get("profiles", {})] for project, runner in _task_runners().items()
    }
    assert "rusty-chess" in expected
    assert _task_profiles() == expected
    assert set(_task_profiles()) == set(_task_types())


def test_bots_output_names_the_task_role() -> None:
    outputs = (REPO_ROOT / "foundation" / "outputs.tf").read_text()
    assert "task_role_arn" in outputs and "module.bot_ci_task[key].role_arn" in outputs


def _bot_datasets() -> dict[str, list[str]]:
    return {k: list(p["datasets"]) for k, p in _projects().items() if p.get("datasets")}


def test_dataset_buckets_match_the_project_files() -> None:
    expected = {}
    for project in registry():
        components = project_file(project)["components"]
        buckets = [c["bucket"] for c in components if c["kind"] == "dataset"]
        if buckets:
            expected[project] = buckets
    assert "rusty-chess" in expected
    assert _bot_datasets() == expected
    assert set(_bot_datasets()) <= set(registry())


def test_bot_plan_and_apply_carry_the_dataset_policies() -> None:
    plan, apply = _module("bot_ci_plan"), _module("bot_ci_apply")
    assert '["dataset/read"]' in plan and "dataset/manage" not in plan
    assert '["dataset/read", "dataset/manage"]' in apply
    for block in (plan, apply):
        assert "contains(keys(local.bot_datasets), each.key)" in block
        assert "bucket_names = local.bot_datasets[each.key]" in block


def test_foundation_reads_lists_from_config() -> None:
    text = BOTS_TF.read_text()
    assert not re.search(r"\b(?:c8a|g6e?|p[345])\.\w+", text), "instance types live in config.yaml"
    for name in ("bot_task_types", "bot_task_profiles", "bot_datasets"):
        match = re.search(rf"^\s+{name}\s*=\s*(.*)$", text, re.MULTILINE)
        assert match and "local.tool_config.projects" in match.group(1), name
    assert set(_projects()) <= set(registry())
    assert set(_task_profiles()) == set(_task_types())


def _research_projects() -> set[str]:
    match = re.search(r"^\s+research_projects\s*=\s*toset\(\[(.*)\]\)$", BOTS_TF.read_text(), re.M)
    assert match, "research_projects is not a one-line toset([...])"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_research_projects_are_registered() -> None:
    assert _research_projects() <= set(registry())


def test_research_projects_are_exactly_the_files_of_that_kind() -> None:
    """A file of the wrong kind would get the wrong policies: compute/* or container/*."""
    research = {
        project
        for project in registry()
        if load_site_config(FIXTURES / "bots" / f"{project}.yaml").kind == "research"
    }
    assert _research_projects() == research


def test_foundation_gives_research_projects_no_release_role() -> None:
    match = re.search(r"^\s*for_each\s*=\s*(.+)$", _module("bot_ci_release"), re.MULTILINE)
    assert match, "bot_ci_release has no for_each"
    comprehension = re.fullmatch(
        r"\{\s*for (\w+), (\w+) in local\.bots\s*:\s*\1\s*=>\s*\2\s*"
        r"if !contains\(local\.research_projects,\s*\1\)\s*\}",
        match.group(1).strip(),
    )
    assert comprehension, f"for_each must filter research out, keeping keys: {match.group(1)}"


def test_foundation_gives_research_projects_compute_and_bots_container() -> None:
    plan, apply = _module("bot_ci_plan"), _module("bot_ci_apply")
    # Same order as before research existed: reordering would churn rusty-chess's attachments.
    assert _policies(plan) == ["tfstate/read", "container/read", "task/read"]
    assert _research_policies(plan) == ["tfstate/read", "compute/read", "task/read"]
    assert _policies(apply) == [
        "tfstate/write",
        "container/read",
        "container/manage",
        "task/read",
        "task/manage",
    ]
    assert _research_policies(apply) == [
        "tfstate/write",
        "compute/read",
        "compute/manage",
        "task/read",
        "task/manage",
    ]


def test_outputs_do_not_reach_for_a_research_projects_release_role() -> None:
    outputs = (REPO_ROOT / "foundation" / "outputs.tf").read_text()
    match = re.search(r"^\s*release_role_arn\s*=\s*(.+)$", outputs, re.MULTILINE)
    assert match
    assert match.group(1).strip() == (
        "contains(local.research_projects, key) ? null : module.bot_ci_release[key].role_arn"
    )
    # Plan and apply roles exist for every project, so the state policy needs no change.
    state = (REPO_ROOT / "foundation" / "state.tf").read_text()
    assert "bot_ci_release" not in state
