"""The Databricks workflows: the credential step, the project plan and the apply."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CREDENTIALS_ACTION = REPO_ROOT / ".github" / "actions" / "databricks-credentials" / "action.yml"
PROJECTS_SCRIPT = REPO_ROOT / "scripts" / "databricks-projects.sh"
SENTINEL = "dbx-SENTINEL-secret-0123"

needs_yq = pytest.mark.skipif(
    shutil.which("yq") is None or shutil.which("jq") is None, reason="yq and jq are on runners"
)


def _yaml(path: Path) -> dict[Any, Any]:
    """A workflow or action as data. PyYAML reads the bare `on:` key as True."""
    data: dict[Any, Any] = yaml.safe_load(path.read_text())
    return data


def _steps(workflow: str, job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _yaml(WORKFLOWS / workflow)["jobs"][job]["steps"]
    return steps


def _index(steps: list[dict[str, Any]], predicate: Any) -> int:
    return next(i for i, s in enumerate(steps) if predicate(s))


# --- the credential step -----------------------------------------------------


def _credentials_script() -> str:
    steps = _yaml(CREDENTIALS_ACTION)["runs"]["steps"]
    assert len(steps) == 1
    script: str = steps[0]["run"]
    return script


def _run_credentials(tmp_path: Path, *, enabled: bool) -> tuple[str, Path]:
    """Run the action's script with a fake `aws` that returns SENTINEL.

    GITHUB_ENV is the script's own stdout, so what it exports and what it
    prints land in one stream, in order.
    """
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    config["databricks"]["enabled"] = enabled
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "aws-calls"
    aws = bin_dir / "aws"
    aws.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{calls}"\necho {SENTINEL}\n')
    aws.chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", _credentials_script()],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "CONFIG": "config.yaml",
            "GITHUB_ENV": "/dev/stdout",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout, calls


@needs_yq
def test_the_secret_is_masked_before_it_is_exported(tmp_path: Path) -> None:
    out, calls = _run_credentials(tmp_path, enabled=True)
    lines = out.splitlines()
    mask = lines.index(f"::add-mask::{SENTINEL}")
    export = lines.index(f"DATABRICKS_CLIENT_SECRET={SENTINEL}")
    assert mask < export
    assert SENTINEL not in "\n".join(lines[:mask])
    assert "DATABRICKS_ACCOUNT_ID=00000000-0000-0000-0000-000000000000" in lines
    assert "DATABRICKS_CLIENT_ID=00000000-0000-0000-0000-000000000000" in lines
    call = calls.read_text()
    assert "ssm get-parameter" in call and "--with-decryption" in call
    assert "--name /jwall-engineering/databricks/client-secret" in call


@needs_yq
def test_the_secret_step_does_nothing_while_disabled(tmp_path: Path) -> None:
    out, calls = _run_credentials(tmp_path, enabled=False)
    assert not calls.exists(), "aws must not be called while databricks.enabled is false"
    assert "DATABRICKS_" not in out


def _uses_credentials(step: dict[str, Any]) -> bool:
    return step.get("uses") == "./.github/actions/databricks-credentials"


@pytest.mark.parametrize(
    ("workflow", "job"), [("ci.yml", "terraform-plan"), ("release-please.yml", "apply")]
)
def test_the_foundation_jobs_get_the_credential_after_aws_and_before_plan(
    workflow: str, job: str
) -> None:
    steps = _steps(workflow, job)
    credentials = _index(steps, _uses_credentials)
    aws = _index(steps, lambda s: "configure-aws-credentials" in s.get("uses", ""))
    plan = _index(steps, lambda s: s.get("name") == "terraform plan")
    assert aws < credentials < plan


# --- the project registry ----------------------------------------------------


def _projects(path: Path | None = None) -> subprocess.CompletedProcess[str]:
    args = [str(PROJECTS_SCRIPT)] + ([str(path)] if path else [])
    return subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True, check=False)


@needs_yq
def test_the_registry_script_reads_databricks_tf(tmp_path: Path) -> None:
    assert json.loads(_projects().stdout) == []
    text = (REPO_ROOT / "foundation" / "databricks.tf").read_text()
    entry = (
        "  databricks_projects = {\n"
        '    databricks-poc = { repository = "jwallace145/databricks-poc", repository_id = "1" }\n'
        '    other          = { repository = "jwallace145/other", repository_id = "2" }\n'
    )
    filled = tmp_path / "databricks.tf"
    filled.write_text(text.replace("  databricks_projects = {\n", entry))
    assert json.loads(_projects(filled).stdout) == [
        {"project": "databricks-poc", "repository": "jwallace145/databricks-poc"},
        {"project": "other", "repository": "jwallace145/other"},
    ]
    missing = tmp_path / "missing.tf"
    missing.write_text("locals {}\n")
    assert _projects(missing).returncode != 0


@needs_yq
def test_the_setup_job_outputs_no_projects_while_disabled(tmp_path: Path) -> None:
    steps = _steps("ci.yml", "databricks-projects")
    step = steps[_index(steps, lambda s: s.get("id") == "list")]
    output = tmp_path / "out"
    subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=REPO_ROOT,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    assert output.read_text().splitlines() == ["enabled=false", "projects=[]"]


# --- the plan job ------------------------------------------------------------

_NOT_A_FORK = "github.event.pull_request.head.repo.full_name == github.repository"


def test_the_plan_job_never_runs_for_forks_and_waits_for_projects() -> None:
    jobs = _yaml(WORKFLOWS / "ci.yml")["jobs"]
    setup, plan = jobs["databricks-projects"], jobs["databricks-plan"]
    assert _NOT_A_FORK in setup["if"]
    for condition in (
        _NOT_A_FORK,
        "needs.databricks-projects.outputs.enabled == 'true'",
        "needs.databricks-projects.outputs.projects != '[]'",
    ):
        assert condition in plan["if"]
    assert plan["needs"] == ["terraform-static", "databricks-projects"]
    matrix = plan["strategy"]["matrix"]
    assert matrix == {"include": "${{ fromJSON(needs.databricks-projects.outputs.projects) }}"}
    assert plan["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "pull-requests": "write",
    }


def test_the_plan_job_uses_the_projects_plan_role_and_skips_until_it_exists() -> None:
    steps = _steps("ci.yml", "databricks-plan")
    role = steps[_index(steps, lambda s: s.get("id") == "role")]
    assert ".aws.account_id" in role["run"] and ".context.organization" in role["run"]
    assert ':role/${organization}-${PROJECT}-ci-plan"' in role["run"]
    assume = steps[_index(steps, lambda s: s.get("id") == "assume")]
    assert assume["continue-on-error"] is True
    assert assume["with"]["role-to-assume"] == "${{ steps.role.outputs.arn }}"
    plan = steps[_index(steps, lambda s: "jwall-engineering-sites" in str(s.get("run", "")))]
    assert plan["if"] == "steps.assume.outcome == 'success'"
    assert plan["run"].endswith("--file .consumer/jwall-engineering-sites.yaml plan")
    # The CLI reads the secret itself, with the project's plan role.
    assert not any(_uses_credentials(s) for s in steps)


# --- the apply workflow ------------------------------------------------------


def test_databricks_apply_is_a_manual_prod_run() -> None:
    apply = _yaml(WORKFLOWS / "databricks-apply.yml")
    inputs = apply[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"project", "destroy"}
    assert inputs["project"]["required"] is True
    assert inputs["destroy"] == {
        "description": inputs["destroy"]["description"],
        "required": False,
        "type": "boolean",
        "default": False,
    }
    assert apply["permissions"] == {}
    assert apply["concurrency"]["cancel-in-progress"] is False
    job = apply["jobs"]["apply"]
    assert job["environment"] == {"name": "prod"}
    assert job["permissions"] == {"contents": "read", "id-token": "write"}


def test_databricks_apply_runs_the_projects_file_with_its_apply_role() -> None:
    steps = _steps("databricks-apply.yml", "apply")
    lookup = steps[_index(steps, lambda s: s.get("id") == "project")]
    assert "scripts/databricks-projects.sh" in lookup["run"]
    checkout = steps[_index(steps, lambda s: s.get("with", {}).get("path") == ".consumer")]
    assert checkout["with"]["repository"] == "${{ steps.project.outputs.repository }}"
    role = steps[_index(steps, lambda s: s.get("id") == "role")]
    assert ':role/${organization}-${PROJECT}-ci-apply"' in role["run"]
    run = steps[_index(steps, lambda s: "jwall-engineering-sites" in str(s.get("run", "")))]
    assert run["env"]["COMMAND"] == "${{ inputs.destroy && 'destroy --yes' || 'apply' }}"
    assert "--file .consumer/jwall-engineering-sites.yaml $COMMAND" in run["run"]
    upload = steps[-1]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == ".jwall-engineering-sites/"


@needs_yq
def test_databricks_apply_refuses_an_unregistered_project(tmp_path: Path) -> None:
    steps = _steps("databricks-apply.yml", "apply")
    lookup = steps[_index(steps, lambda s: s.get("id") == "project")]
    result = subprocess.run(
        ["bash", "-e", "-c", lookup["run"]],
        cwd=REPO_ROOT,
        env={**os.environ, "PROJECT": "databricks-poc", "GITHUB_OUTPUT": str(tmp_path / "o")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "databricks-poc is not in foundation/databricks.tf" in result.stdout
