"""Running a task: the script, streaming, collecting, and termination on every path."""

from __future__ import annotations

import base64
import logging
import os
import shutil
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from jwall_engineering_sites import tasks
from jwall_engineering_sites.errors import ConfigError, PreflightError, RunError
from jwall_engineering_sites.project import Project
from tests.test_tasks_launch import _spec

LOG = logging.getLogger("test")


def _any(fake: object) -> Any:
    """A fake passed where a boto3 client is typed."""
    return fake


LAUNCHED = datetime(2026, 10, 29, tzinfo=UTC)
HOSTILE = "; rm -rf / $(reboot) `id` 'quoted' \"double\"\nTASK_RUNNER_EOF"


def _script(bot: Project, **overrides: Any) -> str:
    return tasks.task_script(
        _spec(bot, **overrides), "jwall-engineering-rusty-chess-tasks", "tasks/"
    )


# --- the script --------------------------------------------------------------


def test_commands_carry_the_script_encoded(bot: Project) -> None:
    spec = _spec(bot, args=("--games", HOSTILE))
    commands = tasks.task_commands(spec, "bucket", "tasks/")
    assert len(commands) == 2
    encoded = commands[0].split()[1]
    assert commands[0] == f"echo {encoded} | base64 -d > /root/task-runner.sh"
    assert commands[1] == "exec bash /root/task-runner.sh"
    assert base64.b64decode(encoded).decode() == tasks.task_script(spec, "bucket", "tasks/")


def _run_tail(script: str, task_code: int, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the script from `status=0` to its exit, with the task (`bash`) and
    `aws` replaced by functions that print their arguments."""
    tail = script[script.index("status=0") :]
    probe = (
        f"bash() {{ printf 'task<%s>\\n' \"$@\"; return {task_code}; }}\n"
        "aws() { printf 'aws<%s>\\n' \"$@\"; }\n"
        "shutdown() { printf 'shutdown<%s>\\n' \"$@\"; }\n" + tail
    )
    return subprocess.run(["/bin/bash", "-c", probe], capture_output=True, text=True, cwd=cwd)


def test_script_passes_hostile_arguments_verbatim(bot: Project, tmp_path: Path) -> None:
    result = _run_tail(_script(bot, args=("--games", "200", HOSTILE)), 0, tmp_path)
    assert result.returncode == 0, result.stderr
    task_lines = [ln for ln in result.stdout.split("\n") if ln.startswith("task<")]
    assert result.stdout.startswith(
        f"task<scripts/sprt.sh>\ntask<--games>\ntask<200>\ntask<{HOSTILE}>\n"
    )
    assert len(task_lines) == 4
    assert not (tmp_path / "reboot").exists()


def test_script_uploads_and_exits_with_the_task_code(bot: Project, tmp_path: Path) -> None:
    result = _run_tail(_script(bot), 7, tmp_path)
    assert result.returncode == 7
    assert "aws<s3>\naws<cp>" in result.stdout
    assert "aws<s3://jwall-engineering-rusty-chess-tasks/tasks/123-1/sprt-0/>" in result.stdout


def test_script_ends_with_the_marker_and_shuts_the_instance_down(
    bot: Project, tmp_path: Path
) -> None:
    out = _run_tail(_script(bot), 3, tmp_path).stdout
    # The instance ends itself a minute after the task: the agent still reports
    # the status and flushes the output; a shutdown terminates it.
    assert out.index("aws<cp>") < out.index("shutdown<-h>\nshutdown<+1>")
    assert out.rstrip("\n").endswith(tasks.END_MARKER)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_script_is_shellcheck_clean(bot: Project) -> None:
    script = _script(bot, args=("--games", HOSTILE))
    result = subprocess.run(
        ["shellcheck", "--shell=bash", "-"], input=script, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout


def test_script_steps_in_order(bot: Project) -> None:
    script = _script(bot)
    steps = [
        "cloud-init status --wait",
        "/etc/task-runner/ready",
        "shutdown -c",
        "shutdown -h +360",
        "git -C /work/repo fetch --quiet --depth 1 https://github.com/jwallace145/rusty-chess-v2"
        " 0123456789abcdef0123456789abcdef01234567",
        "git -C /work/repo checkout --quiet FETCH_HEAD",
        "OUT_DIR=/work/out bash scripts/sprt.sh --games 200",
        "aws s3 cp --recursive --only-show-errors --region us-east-1 /work/out"
        " s3://jwall-engineering-rusty-chess-tasks/tasks/123-1/sprt-0/",
        'exit "$status"',
    ]
    positions = [script.index(step) for step in steps]
    assert positions == sorted(positions)


def test_setup_is_judged_by_the_ready_file(bot: Project) -> None:
    script = _script(bot)
    assert "cloud-init status --wait >/dev/null 2>&1 || true" in script
    assert "if [ ! -f /etc/task-runner/ready ]; then" in script


def test_script_environment_and_no_credentials(bot: Project) -> None:
    script = _script(bot)
    assert "export HOME=/root" in script
    assert "/root/.cargo/bin" in script
    assert "GITHUB_TOKEN" not in script and "x-access-token" not in script
    assert "set -uo pipefail" in script


@pytest.mark.parametrize(
    ("field", "value"),
    [("script", "/etc/passwd"), ("script", "../x.sh"), ("script", ""), ("ref", "main"),
     ("ref", "0123; rm"), ("repository", "evil/repo; rm"), ("run_id", "a/b"), ("task_id", "x y")],
)  # fmt: skip
def test_script_rejects_unsafe_inputs(bot: Project, field: str, value: str) -> None:
    with pytest.raises(ConfigError, match=field):
        _script(bot, **{field: value})


# --- sending ------------------------------------------------------------------


class FakeSSM:
    def __init__(
        self, statuses: list[tuple[str, int]] | None = None, online_after: int = 0
    ) -> None:
        self.statuses = list(statuses or [])
        self.online_after = online_after
        self.describe_calls = 0
        self.sent: list[dict[str, Any]] = []

    def describe_instance_information(self, **kwargs: Any) -> dict[str, Any]:
        self.describe_calls += 1
        if self.describe_calls <= self.online_after:
            return {"InstanceInformationList": []}
        return {"InstanceInformationList": [{"InstanceId": "i-1", "PingStatus": "Online"}]}

    def send_command(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append(kwargs)
        return {"Command": {"CommandId": "cmd-1"}}

    def get_command_invocation(self, **kwargs: Any) -> dict[str, Any]:
        if not self.statuses:
            raise AssertionError("polled after the command finished")
        status, code = self.statuses.pop(0)
        if status == "Missing":
            raise ClientError({"Error": {"Code": "InvocationDoesNotExist"}}, "GetCommandInvocation")
        return {"Status": status, "ResponseCode": code}


def test_send(bot: Project) -> None:
    ssm: Any = FakeSSM()
    command_id = tasks.send(ssm, "i-1", ["echo hi"], "/jwall-engineering/rusty-chess/tasks", 6)
    assert command_id == "cmd-1"
    (call,) = ssm.sent
    assert call["InstanceIds"] == ["i-1"]
    assert call["DocumentName"] == "AWS-RunShellScript"
    assert call["Parameters"] == {"commands": ["echo hi"], "executionTimeout": ["21600"]}
    assert call["CloudWatchOutputConfig"] == {
        "CloudWatchLogGroupName": "/jwall-engineering/rusty-chess/tasks",
        "CloudWatchOutputEnabled": True,
    }


def test_wait_for_agent() -> None:
    ssm: Any = FakeSSM(online_after=2)
    sleeps: list[float] = []
    tasks.wait_for_agent(ssm, "i-1", timeout=600, sleep=sleeps.append, clock=lambda: 0.0)
    assert ssm.describe_calls == 3 and len(sleeps) == 2


def test_wait_for_agent_times_out() -> None:
    ticks = iter(range(0, 10_000, 100))
    with pytest.raises(RunError, match="SSM agent"):
        tasks.wait_for_agent(
            _any(FakeSSM(online_after=10**6)), "i-1", timeout=600, sleep=lambda _: None,
            clock=lambda: float(next(ticks)),
        )  # fmt: skip


# --- streaming ----------------------------------------------------------------


class FakeLogs:
    """Each poll returns the next batch; batches may repeat earlier events."""

    def __init__(self, batches: list[list[tuple[str, int, str]]]) -> None:
        self.batches = batches
        self.calls: list[dict[str, Any]] = []

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        batch = self.batches.pop(0) if self.batches else []
        return {"events": [{"eventId": e, "timestamp": t, "message": m} for e, t, m in batch]}


def test_stream_prints_each_line_once_in_order() -> None:
    logs: Any = FakeLogs([
        [("e1", 1, "one\n"), ("e2", 2, "two\n")],
        [("e2", 2, "two\n"), ("e3", 3, "three")],
        [],
        [("e4", 4, "four\n")],
    ])  # fmt: skip
    ssm: Any = FakeSSM([("Missing", -1), ("InProgress", -1), ("Success", 0)])
    lines: list[str] = []
    code = tasks.stream(logs, ssm, "/g", "cmd-1", "i-1", lines.append, poll=5, sleep=lambda _: None)
    assert code == 0
    assert lines == ["one", "two", "three", "four"]
    assert all(c["logStreamNamePrefix"] == "cmd-1/i-1/" for c in logs.calls)
    assert all(c["logGroupName"] == "/g" for c in logs.calls)


def test_stream_waits_for_the_end_marker() -> None:
    """The agent uploads output on its own schedule: the verdict may land after the status."""
    logs = FakeLogs([
        [("e1", 1, "running\n")],
        [],
        [],
        [("e2", 2, "verdict: H1 accepted\n"), ("e3", 3, tasks.END_MARKER + "\n")],
    ])  # fmt: skip
    ssm: Any = FakeSSM([("Success", 0)])
    lines: list[str] = []
    code = tasks.stream(logs, ssm, "/g", "c", "i", lines.append, poll=5, sleep=lambda _: None)  # type: ignore[arg-type]
    assert code == 0
    assert lines == ["running", "verdict: H1 accepted"]


def test_stream_gives_up_on_the_marker_after_a_bound() -> None:
    logs: Any = FakeLogs([[("e1", 1, "x\n")]])
    ssm: Any = FakeSSM([("Failed", 2)])
    sleeps: list[float] = []
    code = tasks.stream(logs, ssm, "/g", "c", "i", lambda _: None, poll=5, sleep=sleeps.append)
    assert code == 2
    assert sum(sleeps) <= 120 and len(logs.calls) > 3


class FlakyLogs(FakeLogs):
    def __init__(self, error: Exception, batches: list[list[tuple[str, int, str]]]) -> None:
        super().__init__(batches)
        self.error: Exception | None = error

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return super().filter_log_events(**kwargs)


@pytest.mark.parametrize(
    "error",
    [ClientError({"Error": {"Code": "ThrottlingException"}}, "FilterLogEvents"),
     ClientError({"Error": {"Code": "ServiceUnavailableException"}}, "FilterLogEvents"),
     EndpointConnectionError(endpoint_url="https://logs")],
)  # fmt: skip
def test_stream_survives_transient_errors(error: Exception) -> None:
    logs: Any = FlakyLogs(error, [[("e1", 1, "one\n"), ("e2", 2, tasks.END_MARKER)]])
    ssm: Any = FakeSSM([("Success", 0)])
    lines: list[str] = []
    assert tasks.stream(logs, ssm, "/g", "c", "i", lines.append, poll=5, sleep=lambda _: None) == 0
    assert lines == ["one"]


def test_stream_stops_on_lost_credentials() -> None:
    error = ClientError({"Error": {"Code": "ExpiredTokenException"}}, "FilterLogEvents")
    logs: Any = FlakyLogs(error, [])
    with pytest.raises(ClientError):
        tasks.stream(
            logs, _any(FakeSSM([("Success", 0)])), "/g", "c", "i", print, poll=5,
            sleep=lambda _: None,
        )  # fmt: skip


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [("Failed", 7, 7), ("TimedOut", -1, 124), ("Cancelled", -1, 130), ("Terminated", -1, 1),
     ("Undeliverable", -1, 1), ("Failed", -1, 1)],
)  # fmt: skip
def test_stream_maps_the_outcome(status: str, code: int, expected: int) -> None:
    ssm: Any = FakeSSM([(status, code)])
    result = tasks.stream(
        _any(FakeLogs([])), ssm, "/g", "c", "i", lambda _: None, poll=5, sleep=lambda _: None
    )
    assert result == expected


# --- collecting and terminating -----------------------------------------------


def test_collect(tmp_path: Path) -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="task-results")
        s3.put_object(Bucket="task-results", Key="tasks/1/a/hello.txt", Body=b"hi")
        s3.put_object(Bucket="task-results", Key="tasks/1/a/deep/r.json", Body=b"{}")
        s3.put_object(Bucket="task-results", Key="tasks/1/b/other.txt", Body=b"no")
        s3.put_object(Bucket="task-results", Key="tasks/1/a/../../escape.txt", Body=b"no")
        paths = tasks.collect(s3, "task-results", "tasks/1/a/", tmp_path / "out")
    assert sorted(p.relative_to(tmp_path / "out").as_posix() for p in paths) == [
        "deep/r.json",
        "hello.txt",
    ]
    assert (tmp_path / "out" / "hello.txt").read_bytes() == b"hi"
    assert not (tmp_path / "escape.txt").exists()


class FakeEC2:
    def __init__(self, error: str | None = None) -> None:
        self.error = error
        self.terminated: list[str] = []

    def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:  # noqa: N803
        self.terminated.extend(InstanceIds)
        if self.error:
            raise ClientError({"Error": {"Code": self.error}}, "TerminateInstances")
        return {}


class EventuallyEC2(FakeEC2):
    """InvalidInstanceID.NotFound for the first `misses` calls: EC2's eventual consistency."""

    def __init__(self, misses: int) -> None:
        super().__init__()
        self.misses = misses

    def terminate_instances(self, InstanceIds: list[str]) -> dict[str, Any]:  # noqa: N803
        self.terminated.extend(InstanceIds)
        if self.misses:
            self.misses -= 1
            raise ClientError(
                {"Error": {"Code": "InvalidInstanceID.NotFound"}}, "TerminateInstances"
            )
        return {}


def test_terminate_retries_a_not_yet_visible_instance() -> None:
    ec2: Any = EventuallyEC2(misses=2)
    sleeps: list[float] = []
    assert tasks.terminate(ec2, "i-1", LOG, sleep=sleeps.append, clock=lambda: 0.0)
    assert ec2.terminated == ["i-1", "i-1", "i-1"] and len(sleeps) == 2


def test_terminate_gives_up_on_a_missing_instance_after_a_minute() -> None:
    ec2: Any = EventuallyEC2(misses=10**6)
    ticks = iter(range(0, 10_000, 10))
    found = tasks.terminate(ec2, "i-1", LOG, sleep=lambda _: None, clock=lambda: float(next(ticks)))
    assert found is False and 3 < len(ec2.terminated) < 10


def test_terminate_reports_other_failures() -> None:
    with pytest.raises(RunError, match="i-1"):
        tasks.terminate(_any(FakeEC2("UnauthorizedOperation")), "i-1", LOG)


# --- the orchestration ----------------------------------------------------------


class Recorder:
    """Stands in for the AWS steps of run_task and records what ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, **behaviour: Any) -> None:
        self.calls: list[str] = []
        self.behaviour = behaviour
        self.terminated: list[str] = []
        for name in ("instance_types_info", "wait_for_slot", "public_subnets", "price_plan",
                     "launch", "wait_for_agent", "send", "stream", "collect", "terminate",
                     "running_tasks"):  # fmt: skip
            monkeypatch.setattr(tasks, name, self._step(name))

    def _step(self, name: str) -> Any:
        def step(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            action = self.behaviour.get(name)
            if callable(action):
                return action(*args, **kwargs)
            if isinstance(action, BaseException):
                raise action
            if name == "terminate":
                self.terminated.append(args[1])
            subnets = [tasks.Subnet("subnet-a", "us-east-1a", "use1-az6")]
            defaults: dict[str, Any] = {
                "running_tasks": [],
                "instance_types_info": {"c8a.16xlarge": (64, 1)},
                "public_subnets": subnets,
                "price_plan": tasks.PricePlan(subnets, "2.0002", 3.44864, {}, {}),
                "launch": tasks.Launched("i-1", "us-east-1a", "ami-1", "spot"),
                "send": "cmd-1",
                "stream": 0,
                "collect": [],
            }
            return action if action is not None else defaults.get(name)

        return step


class FakeSession:
    """The steps are replaced, so the clients are never used."""

    def client(self, name: str, **kwargs: Any) -> object:
        return object()


def _run(bot: Project, tmp_path: Path) -> int:
    session: Any = FakeSession()
    return tasks.run_task(session, _spec(bot), tmp_path / "out", LOG, write=lambda _: None)


def test_run_task_success(bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder(monkeypatch)
    assert _run(bot, tmp_path) == 0
    assert rec.calls == ["instance_types_info", "wait_for_slot", "public_subnets", "price_plan",
                         "launch",
                         "wait_for_agent", "send", "stream", "collect", "terminate"]  # fmt: skip
    assert rec.terminated == ["i-1"]


def test_run_task_failure_still_collects(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(monkeypatch, stream=7)
    assert _run(bot, tmp_path) == 7
    assert "collect" in rec.calls and rec.terminated == ["i-1"]


def test_run_task_collect_failure_after_success_is_a_runtime_failure(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(monkeypatch, collect=RunError("download failed"))
    assert _run(bot, tmp_path) == 1
    assert rec.terminated == ["i-1"]


def test_run_task_sigterm_during_streaming_terminates(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stream(*args: Any, **kwargs: Any) -> int:
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("the handler should have interrupted the stream")

    before = signal.getsignal(signal.SIGTERM)
    rec = Recorder(monkeypatch, stream=stream)
    assert _run(bot, tmp_path) == 130
    assert rec.terminated == ["i-1"]
    assert "collect" not in rec.calls
    assert signal.getsignal(signal.SIGTERM) == before


def test_run_task_second_signal_cannot_abort_termination(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    terminated: list[str] = []

    def terminate(ec2: Any, instance_id: str, logger: Any) -> None:
        os.kill(os.getpid(), signal.SIGTERM)  # GitHub's SIGTERM 7.5 s after SIGINT
        terminated.append(instance_id)

    def stream(*args: Any, **kwargs: Any) -> int:
        os.kill(os.getpid(), signal.SIGINT)
        raise AssertionError("unreachable")

    Recorder(monkeypatch, stream=stream, terminate=terminate)
    assert _run(bot, tmp_path) == 130
    assert terminated == ["i-1"]


def test_run_task_interrupt_during_launch_finds_the_instance_by_tags(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal between RunInstances and recording the ID must not leak the instance."""
    found = [
        tasks.TaskInstance("i-9", "c8a.xlarge", "pending", "123-1", "sprt-0", LAUNCHED, 6),
        tasks.TaskInstance("i-8", "c8a.xlarge", "running", "123-1", "other", LAUNCHED, 6),
    ]
    calls = iter([found])

    def launch(*args: Any, **kwargs: Any) -> str:
        os.kill(os.getpid(), signal.SIGINT)
        raise AssertionError("unreachable")

    rec = Recorder(monkeypatch, launch=launch, running_tasks=lambda *a: next(calls))
    assert _run(bot, tmp_path) == 130
    assert rec.terminated == ["i-9"]


def test_run_task_launch_failure_has_nothing_to_terminate(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(monkeypatch, launch=PreflightError("no on-demand capacity"))
    assert _run(bot, tmp_path) == 3
    assert rec.terminated == []


def test_run_task_refuses_before_launching_when_no_slot_frees(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(monkeypatch, wait_for_slot=PreflightError("no free slot after 30 minutes"))
    assert _run(bot, tmp_path) == 3
    assert "launch" not in rec.calls and rec.terminated == []


def test_run_task_refuses_an_smt_type(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(monkeypatch, instance_types_info={"c8a.16xlarge": (64, 2)})
    assert _run(bot, tmp_path) == 2
    assert "launch" not in rec.calls


def test_run_task_agent_timeout_terminates(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(monkeypatch, wait_for_agent=RunError("SSM agent not online"))
    assert _run(bot, tmp_path) == 1
    assert rec.terminated == ["i-1"]


def test_run_task_aws_error_terminates(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = ClientError({"Error": {"Code": "AccessDenied"}}, "SendCommand")
    rec = Recorder(monkeypatch, send=error)
    assert _run(bot, tmp_path) == 1
    assert rec.terminated == ["i-1"]


def test_run_task_needs_a_task_runner(bot: Project, tmp_path: Path) -> None:
    components = tuple(c for c in bot.config.components if c.kind != "task-runner")
    plain = bot.model_copy(
        update={"config": bot.config.model_copy(update={"components": components})}
    )
    spec = _spec(plain)
    session: Any = FakeSession()
    assert tasks.run_task(session, spec, tmp_path, LOG, write=lambda _: None) == 2


def test_run_task_connection_error_terminates(
    bot: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = Recorder(monkeypatch, wait_for_agent=EndpointConnectionError(endpoint_url="https://ssm"))
    assert _run(bot, tmp_path) == 1
    assert rec.terminated == ["i-1"]
