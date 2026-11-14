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
CREDENTIALS_SCRIPT = REPO_ROOT / "scripts" / "databricks-credentials.sh"
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


# --- the credential script --------------------------------------------------


def _run_credentials(tmp_path: Path, *, enabled: bool) -> tuple[str, Path, Path]:
    """Source the script in a step-like shell (`bash -e`) with a fake `aws` that returns
    SENTINEL, then print the environment the step's terraform would see."""
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    config["databricks"]["enabled"] = enabled
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "aws-calls"
    aws = bin_dir / "aws"
    aws.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{calls}"\necho {SENTINEL}\n')
    aws.chmod(0o755)
    github_env = tmp_path / "github-env"
    github_env.touch()
    result = subprocess.run(
        ["bash", "-e", "-c", f'source "{CREDENTIALS_SCRIPT}" config.yaml\necho ---\nenv'],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_ENV": str(github_env),
            "GITHUB_OUTPUT": str(github_env),
        },
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout, calls, github_env


@needs_yq
def test_the_secret_is_masked_before_it_is_used_and_stays_in_the_step(tmp_path: Path) -> None:
    out, calls, github_env = _run_credentials(tmp_path, enabled=True)
    printed, environment = out.split("---\n", 1)
    lines = printed.splitlines()
    assert lines[0] == f"::add-mask::{SENTINEL}"
    assert SENTINEL not in "\n".join(lines[1:])
    assert f"DATABRICKS_CLIENT_SECRET={SENTINEL}" in environment.splitlines()
    assert "DATABRICKS_ACCOUNT_ID=00000000-0000-0000-0000-000000000000" in environment
    assert "DATABRICKS_CLIENT_ID=00000000-0000-0000-0000-000000000000" in environment
    assert "_dbx_" not in environment  # its own variables are unset
    # Nothing reaches GITHUB_ENV or GITHUB_OUTPUT, so no later step sees it.
    assert github_env.read_text() == ""
    call = calls.read_text()
    assert "ssm get-parameter" in call and "--with-decryption" in call
    assert "--name /jwall-engineering/databricks/client-secret" in call


@needs_yq
def test_the_secret_script_does_nothing_while_disabled(tmp_path: Path) -> None:
    out, calls, github_env = _run_credentials(tmp_path, enabled=False)
    assert not calls.exists(), "aws must not be called while databricks.enabled is false"
    assert "DATABRICKS_" not in out
    assert github_env.read_text() == ""


@needs_yq
def test_a_multiline_secret_fails_the_step_without_printing_it(tmp_path: Path) -> None:
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    config["databricks"]["enabled"] = True
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(f"#!/usr/bin/env bash\nprintf '{SENTINEL}\\nline2\\n'\n")
    (bin_dir / "aws").chmod(0o755)
    result = subprocess.run(
        ["bash", "-e", "-c", f'source "{CREDENTIALS_SCRIPT}" config.yaml\necho reached'],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "reached" not in result.stdout
    assert SENTINEL not in result.stdout + result.stderr


_SOURCE = "source ../scripts/databricks-credentials.sh ../config.yaml"


@pytest.mark.parametrize(
    ("workflow", "job", "terraform_steps"),
    [
        ("ci.yml", "terraform-plan", {"terraform plan"}),
        ("release-please.yml", "apply", {"terraform plan", "terraform apply"}),
    ],
)
def test_only_the_terraform_steps_get_the_credential(
    workflow: str, job: str, terraform_steps: set[str]
) -> None:
    data = _yaml(WORKFLOWS / workflow)["jobs"][job]
    steps: list[dict[str, Any]] = data["steps"]
    sourcing = {s.get("name") for s in steps if "databricks-credentials" in str(s)}
    assert sourcing == terraform_steps
    for step in steps:
        if step.get("name") in terraform_steps:
            run = step["run"]
            # Sourced first, in the step's own shell, before terraform runs.
            assert run.splitlines()[0] == _SOURCE
        text = json.dumps(step)
        assert "GITHUB_ENV" not in text or "DATABRICKS" not in text
        assert "DATABRICKS_CLIENT_SECRET" not in json.dumps(step.get("env", {}))
    assert "DATABRICKS" not in json.dumps(data.get("env", {}))
    aws = _index(steps, lambda s: "configure-aws-credentials" in s.get("uses", ""))
    plan = _index(steps, lambda s: s.get("name") == "terraform plan")
    assert aws < plan


def test_no_workflow_exports_a_databricks_secret() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line in path.read_text().splitlines():
            if "GITHUB_ENV" in line:
                assert "DATABRICKS" not in line and "SECRET" not in line.upper(), path.name
    assert not (REPO_ROOT / ".github" / "actions" / "databricks-credentials").exists()
    assert "GITHUB_ENV" not in CREDENTIALS_SCRIPT.read_text().replace(
        "It never writes to GITHUB_ENV", ""
    )


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
    assert not any("databricks-credentials" in str(s) for s in steps)


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
        env={**os.environ, "PROJECT": "not-a-project", "GITHUB_OUTPUT": str(tmp_path / "o")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "not-a-project is not in foundation/databricks.tf" in result.stdout
