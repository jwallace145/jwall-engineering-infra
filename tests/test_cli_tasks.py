"""The task commands: run, list, terminate, sweep."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from jwall_engineering_sites import cli, tasks
from tests.test_cli import BOT_PLAN_ROLE, _as_role

runner = CliRunner()
TASK_ROLE = "arn:aws:sts::650503560686:assumed-role/jwall-engineering-rusty-chess-ci-task/s"
SHA = "0123456789abcdef0123456789abcdef01234567"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch, bot_workspace: Path) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def run_task(session: Any, spec: tasks.TaskSpec, out_dir: Path, logger: Any, **kw: Any) -> int:
        seen["spec"], seen["out_dir"], seen["kw"] = spec, out_dir, kw
        return int(seen.get("code", 0))

    monkeypatch.setattr(tasks, "run_task", run_task)
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB", "GITHUB_WORKFLOW"):
        monkeypatch.delenv(name, raising=False)
    return seen


def _run(*args: str) -> Any:
    with _as_role(TASK_ROLE):
        return runner.invoke(
            cli.app, ["task", "run", "--script", "scripts/sprt.sh", "--ref", SHA, *args]
        )


def test_run_passes_the_task_and_its_exit_code(captured: dict[str, Any]) -> None:
    captured["code"] = 7
    result = _run("--", "--games", "200", "--flag")
    assert result.exit_code == 7, result.output
    spec: tasks.TaskSpec = captured["spec"]
    assert spec.args == ("--games", "200", "--flag")
    assert spec.script == "scripts/sprt.sh" and spec.ref == SHA
    assert spec.instance_type == "c8a.xlarge" and spec.max_hours == 6
    assert spec.repository == "jwallace145/rusty-chess-v2"
    assert spec.task_id == "local" and spec.workflow == "local"
    assert captured["out_dir"] == Path("out")
    assert captured["kw"]["poll"] == 5


def test_run_ids_follow_github(captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_RUN_ID", "555")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("GITHUB_JOB", "sprt")
    monkeypatch.setenv("GITHUB_WORKFLOW", "SPRT")
    assert _run().exit_code == 0
    spec: tasks.TaskSpec = captured["spec"]
    assert (spec.run_id, spec.task_id, spec.workflow) == ("555-2", "sprt", "SPRT")


def test_run_explicit_options(captured: dict[str, Any]) -> None:
    result = _run("--instance-type", "c8a.2xlarge", "--max-hours", "2", "--out", "results",
                  "--run-id", "r1", "--task-id", "t1")  # fmt: skip
    assert result.exit_code == 0, result.output
    spec: tasks.TaskSpec = captured["spec"]
    assert (spec.instance_type, spec.max_hours, spec.run_id, spec.task_id) == (
        "c8a.2xlarge",
        2,
        "r1",
        "t1",
    )
    assert captured["out_dir"] == Path("results")


def test_run_refuses_a_type_outside_the_allow_list(captured: dict[str, Any]) -> None:
    result = _run("--instance-type", "c8a.48xlarge")
    assert result.exit_code == 2
    assert "c8a.xlarge, c8a.2xlarge" in result.output
    assert "spec" not in captured


def test_run_refuses_more_hours_than_the_project_allows(captured: dict[str, Any]) -> None:
    result = _run("--max-hours", "7")
    assert result.exit_code == 2
    assert "max_hours" in result.output and "spec" not in captured


def test_run_needs_the_task_role(captured: dict[str, Any]) -> None:
    with _as_role(BOT_PLAN_ROLE):
        result = runner.invoke(cli.app, ["task", "run", "--script", "s.sh", "--ref", SHA])
    assert result.exit_code == 3
    assert "ci-task" in result.output and "spec" not in captured


def _task(id_: str, run: str | None, task: str | None, age: int, hours: int | None) -> Any:
    return tasks.TaskInstance(
        id_, "c8a.xlarge", "running", run, task, NOW - timedelta(hours=age), hours
    )


def _instances() -> list[tasks.TaskInstance]:
    return [
        _task("i-old", "1-1", "a", 7, 6),
        _task("i-run", "2-1", "a", 1, 6),
        _task("i-run2", "2-1", "b", 1, 6),
        _task("i-untagged", None, None, 8, None),
    ]


@pytest.fixture
def fleet(monkeypatch: pytest.MonkeyPatch, bot_workspace: Path) -> list[str]:
    terminated: list[str] = []
    monkeypatch.setattr(tasks, "running_tasks", lambda ec2, project: _instances())
    monkeypatch.setattr(tasks, "terminate", lambda ec2, i, logger: terminated.append(i))
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    return terminated


def test_list(fleet: list[str]) -> None:
    with _as_role(BOT_PLAN_ROLE):
        result = runner.invoke(cli.app, ["task", "list"])
    assert result.exit_code == 0, result.output
    for instance in ("i-old", "i-run", "i-run2", "i-untagged"):
        assert instance in result.output
    assert fleet == []


def test_terminate_by_run_id(fleet: list[str]) -> None:
    with _as_role(TASK_ROLE):
        result = runner.invoke(cli.app, ["task", "terminate", "--run-id", "2-1"])
    assert result.exit_code == 0, result.output
    assert fleet == ["i-run", "i-run2"]


def test_terminate_by_run_and_task(fleet: list[str]) -> None:
    with _as_role(TASK_ROLE):
        result = runner.invoke(cli.app, ["task", "terminate", "--run-id", "2-1", "--task-id", "b"])
    assert result.exit_code == 0, result.output
    assert fleet == ["i-run2"]


def test_terminate_one_instance_only_if_it_is_a_task(fleet: list[str]) -> None:
    with _as_role(TASK_ROLE):
        ok = runner.invoke(cli.app, ["task", "terminate", "--instance", "i-old"])
        other = runner.invoke(cli.app, ["task", "terminate", "--instance", "i-bot"])
    assert ok.exit_code == 0 and fleet == ["i-old"]
    assert other.exit_code == 3 and "not a running task instance" in other.output


def test_terminate_all(fleet: list[str]) -> None:
    with _as_role(TASK_ROLE):
        result = runner.invoke(cli.app, ["task", "terminate", "--all"])
    assert result.exit_code == 0 and fleet == ["i-old", "i-run", "i-run2", "i-untagged"]


@pytest.mark.parametrize("args", [[], ["--all", "--run-id", "1"], ["--task-id", "a"]])
def test_terminate_needs_exactly_one_selector(fleet: list[str], args: list[str]) -> None:
    with _as_role(TASK_ROLE):
        result = runner.invoke(cli.app, ["task", "terminate", *args])
    assert result.exit_code == 2 and fleet == []


def test_terminate_with_nothing_matching_succeeds(fleet: list[str]) -> None:
    """The workflow's always() cleanup runs after a task that already terminated."""
    with _as_role(TASK_ROLE):
        result = runner.invoke(cli.app, ["task", "terminate", "--run-id", "9-9"])
    assert result.exit_code == 0 and fleet == []


def test_sweep_terminates_instances_past_their_lifetime(fleet: list[str]) -> None:
    with _as_role(TASK_ROLE):
        result = runner.invoke(cli.app, ["task", "sweep"])
    assert result.exit_code == 0, result.output
    # i-untagged falls back to the project's max_hours (6).
    assert fleet == ["i-old", "i-untagged"]


def test_sweep_dry_run_changes_nothing(fleet: list[str]) -> None:
    with _as_role(TASK_ROLE):
        result = runner.invoke(cli.app, ["task", "sweep", "--dry-run"])
    assert result.exit_code == 0 and fleet == []
    assert "i-old" in result.output and "i-untagged" in result.output
    assert "i-run" not in result.output  # neither i-run nor i-run2 is past its lifetime
