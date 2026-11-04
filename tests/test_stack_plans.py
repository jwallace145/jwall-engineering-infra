"""The stack plan tests stay wired up, and stay out of the stacks."""

from __future__ import annotations

import os
from typing import Any

import yaml

from jwall_engineering_sites.settings import Settings, load_settings
from tests.conftest import REPO_ROOT
from tests.stack_plans.render import cases

PLANS = REPO_ROOT / "tests" / "stack_plans"
SCRIPT = REPO_ROOT / "scripts" / "test-stack-plans.sh"


def _settings() -> Settings:
    return load_settings(REPO_ROOT / "config.yaml", environ={})


def test_every_rendered_case_has_a_test_file_and_the_reverse() -> None:
    assert set(cases(_settings())) == {p.name.split(".")[0] for p in PLANS.glob("*.tftest.hcl")}


def test_the_cases_cover_a_bot_a_research_project_and_a_dataset_alone() -> None:
    rendered = cases(_settings())
    assert rendered["rusty_chess"]["bot_enabled"] is True
    assert rendered["math_prover"]["bot_enabled"] is False
    assert rendered["math_prover"]["task_runner_enabled"] is True
    assert rendered["dataset_only"]["bot_enabled"] is False
    assert rendered["dataset_only"]["task_runner_enabled"] is False
    assert [d["name"] for d in rendered["dataset_only"]["datasets"]] == ["artifacts"]


def test_no_test_file_sits_in_a_stack_the_image_copies() -> None:
    stacks = REPO_ROOT / "stacks"
    found = [p for p in stacks.rglob("*.tf*.hcl") if ".terraform" not in p.parts]
    assert [p.name for p in found if p.name != ".terraform.lock.hcl"] == []


def test_ci_runs_the_script_with_the_pinned_terraform() -> None:
    ci: dict[Any, Any] = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["stack-plans"]
    assert job["permissions"] == {"contents": "read"}, "the job needs no AWS role"
    runs = [s.get("run") for s in job["steps"]]
    assert "scripts/test-stack-plans.sh" in runs
    setup = next(s for s in job["steps"] if "setup-terraform" in s.get("uses", ""))
    pinned = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())["terraform"]["version"]
    assert setup["with"]["terraform_version"] == pinned
    assert setup["with"]["terraform_wrapper"] is False


def test_the_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)
