"""The reusable remote-task workflow: who may run it and how it cleans up."""

from __future__ import annotations

import re
from typing import Any

import yaml

from tests.conftest import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "remote-task.yml"


def _workflow() -> dict[Any, Any]:
    data: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text())
    return data


def _steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = _workflow()["jobs"]["task"]["steps"]
    return steps


def _index(predicate: Any) -> int:
    return next(i for i, s in enumerate(_steps()) if predicate(s))


def test_it_is_reusable_with_the_planned_inputs() -> None:
    call = _workflow()[True]["workflow_call"]
    assert set(call["inputs"]) == {
        "project", "script", "args", "profile", "instance-type", "market", "max-hours",
        "task-id", "artifact", "artifact-path", "debug",
    }  # fmt: skip
    assert call["inputs"]["project"]["required"] and call["inputs"]["script"]["required"]
    assert call["inputs"]["args"]["default"] == "[]"
    assert call["inputs"]["profile"]["default"] == "cpu"
    assert call["inputs"]["artifact-path"]["default"] == "out"


def test_permissions_and_timeout() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {}
    job = workflow["jobs"]["task"]
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["timeout-minutes"] == 360  # GitHub-hosted jobs stop at six hours


def test_the_first_step_refuses_forks_and_other_events() -> None:
    first = _steps()[0]
    script = first["run"]
    assert "fork pull requests cannot use the standardized task runners" in script
    assert "github.event.pull_request.head.repo.full_name" in str(first["env"])
    # ref:refs/heads/main is also the subject of pull_request_target, issue_comment
    # and workflow_run runs on main, which can carry fork code: refuse them here.
    assert re.search(r"push\|workflow_dispatch\|schedule\)", script)
    assert "pull_request)" in script and "*)" in script
    credentials = _index(lambda s: "configure-aws-credentials" in s.get("uses", ""))
    assert credentials > 0


def test_the_role_is_built_from_config_with_a_long_enough_session() -> None:
    role = _steps()[_index(lambda s: s.get("id") == "role")]
    assert ".aws.account_id" in role["run"] and ".context.organization" in role["run"]
    assert '-${PROJECT}-ci-task"' in role["run"]
    assert "3600" in role["run"]
    creds = _steps()[_index(lambda s: "configure-aws-credentials" in s.get("uses", ""))]
    assert creds["with"]["role-to-assume"] == "${{ steps.role.outputs.arn }}"
    assert creds["with"]["role-duration-seconds"] == "${{ steps.role.outputs.duration }}"


def test_the_task_step_runs_the_pr_head_and_gets_no_github_token() -> None:
    step = _steps()[_index(lambda s: s.get("name") == "jwall-engineering-sites task run")]
    assert "GITHUB_TOKEN" not in str(step.get("env", {}))
    assert step["env"]["REF"] == "${{ github.event.pull_request.head.sha || github.sha }}"
    assert '--run-id "$RUN_ID"' in step["run"] and '--task-id "$TASK_ID"' in step["run"]
    assert '-- "${argv[@]}"' in step["run"]
    # GitHub signals the step's own process on cancel: exec the CLI so that is
    # the CLI itself, not bash or uv, and its handlers terminate the instance.
    assert "exec .jwall-engineering-infra/.venv/bin/jwall-engineering-sites" in step["run"]
    env = _workflow()["jobs"]["task"]["env"]
    assert env["RUN_ID"] == "${{ github.run_id }}-${{ github.run_attempt }}"
    # Matrix legs already need distinct artifact names: they name the task too.
    assert env["TASK_ID"] == "${{ inputs.task-id || inputs.artifact }}"
    assert _workflow()[True]["workflow_call"]["inputs"]["task-id"]["default"] == ""


def test_no_step_hands_out_the_github_token() -> None:
    assert "github.token" not in WORKFLOW.read_text()
    assert "secrets." not in WORKFLOW.read_text()
    for step in _steps():
        if step.get("uses", "").startswith("actions/checkout"):
            assert step["with"]["persist-credentials"] is False


def test_upload_and_cleanup_always_run() -> None:
    upload = _steps()[_index(lambda s: s.get("name") == "Upload the task's outputs")]
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ inputs.artifact-path }}"
    cleanup = _steps()[-1]
    assert cleanup["if"].startswith("always()")
    assert 'task terminate --run-id "$RUN_ID" --task-id "$TASK_ID"' in cleanup["run"]


def test_runs_the_cli_from_this_workflow_commit() -> None:
    text = WORKFLOW.read_text()
    assert "ref: ${{ github.job_workflow_sha }}" in text
    assert "--config .jwall-engineering-infra/config.yaml" in text


def test_outcome_and_interruption_are_outputs() -> None:
    workflow = _workflow()
    outputs = workflow[True]["workflow_call"]["outputs"]
    assert outputs["outcome"]["value"] == "${{ jobs.task.outputs.outcome }}"
    assert outputs["interrupted-at"]["value"] == "${{ jobs.task.outputs.interrupted-at }}"
    job = workflow["jobs"]["task"]
    assert job["outputs"] == {
        "outcome": "${{ steps.task.outputs.outcome }}",
        "interrupted-at": "${{ steps.task.outputs.interrupted-at }}",
    }
    assert (
        _steps()[_index(lambda s: s.get("id") == "task")]["name"]
        == "jwall-engineering-sites task run"
    )


def test_an_interrupted_tasks_artifact_says_so() -> None:
    upload = _steps()[_index(lambda s: s.get("name") == "Upload the task's outputs")]
    suffix = "${{ steps.task.outputs.outcome == 'interrupted' && '-interrupted' || '' }}"
    assert upload["with"]["name"] == "${{ inputs.artifact }}" + suffix


def test_the_task_step_passes_the_market_and_the_pull_request() -> None:
    step = _steps()[_index(lambda s: s.get("id") == "task")]
    assert step["env"]["MARKET"] == "${{ inputs.market }}"
    assert step["env"]["PULL_REQUEST"] == "${{ github.event.pull_request.number }}"
    assert 'flags+=(--market "$MARKET")' in step["run"]
    assert 'flags+=(--pull-request "$PULL_REQUEST")' in step["run"]
