"""Remote CI tasks: one task on one fresh, dedicated, on-demand EC2 instance.

`task run` launches an instance from the project's task launch template
(module task-runner), runs one script from the consumer repository on it
through SSM Run Command, streams its output from CloudWatch, copies its
outputs from S3 and terminates it, on every exit path. Instances are
on-demand only: a spot interruption would lose a long benchmark.
"""

from __future__ import annotations

import base64
import re
import shlex
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from logging import Logger
from pathlib import Path, PurePosixPath
from types import FrameType
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from .errors import ConfigError, PreflightError, RunError, SitesError
from .project import Project

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ec2.type_defs import TagSpecificationTypeDef, TagTypeDef
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ssm import SSMClient

#: Tags the task-runner launch template puts on task instances and volumes,
#: which the task launch role requires and scopes termination by.
TASK_COMPONENT = "task-runner"
TASK_PURPOSE = "ci-task"

#: RunInstances errors that mean "no capacity here": try the next zone.
CAPACITY_ERRORS = frozenset({"InsufficientInstanceCapacity", "Unsupported"})

_LIVE_STATES = ["pending", "running", "stopping"]
_TAG_VALUE_UNSAFE = re.compile(r"[^A-Za-z0-9 +\-=._:/@]")


@dataclass(frozen=True)
class TaskSpec:
    """One task: what to run, where it comes from, and how it is labelled."""

    project: Project
    instance_type: str
    max_hours: int
    run_id: str
    task_id: str
    workflow: str
    repository: str
    ref: str
    script: str
    args: tuple[str, ...]


@dataclass(frozen=True)
class TaskInstance:
    id: str
    type: str
    state: str
    run_id: str | None
    task_id: str | None
    launched_at: datetime
    max_hours: int | None


@dataclass(frozen=True)
class Launched:
    """A just-launched instance, from the RunInstances response itself: EC2 is
    eventually consistent, so a describe right after launch may not find it."""

    id: str
    zone: str
    image: str


@dataclass(frozen=True)
class Subnet:
    id: str
    zone: str


def _tag_value(value: str) -> str:
    """EC2 tag values allow letters, digits, spaces and + - = . _ : / @."""
    return _TAG_VALUE_UNSAFE.sub("-", value)[:256]


def task_tags(spec: TaskSpec) -> dict[str, str]:
    """Every tag a task instance and its volume carry: the project file's six
    tags (RunInstances does not apply the provider's default tags), the
    launch template's two, and the run's."""
    project = spec.project
    return {
        **project.config.tags,
        "Component": TASK_COMPONENT,
        "Purpose": TASK_PURPOSE,
        "Name": _tag_value(f"{project.task_name}-{spec.run_id}-{spec.task_id}"),
        "Workflow": _tag_value(spec.workflow),
        "RunId": _tag_value(spec.run_id),
        "TaskId": _tag_value(spec.task_id),
        "MaxLifetimeHours": str(spec.max_hours),
    }


def _tags(raw: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in raw or []}


def running_tasks(ec2: EC2Client, project: str) -> list[TaskInstance]:
    """The project's live task instances (never the bot: it has no Purpose tag)."""
    found: list[TaskInstance] = []
    paginator = ec2.get_paginator("describe_instances")
    pages = paginator.paginate(
        Filters=[
            {"Name": "tag:Project", "Values": [project]},
            {"Name": "tag:Purpose", "Values": [TASK_PURPOSE]},
            {"Name": "instance-state-name", "Values": _LIVE_STATES},
        ]
    )
    for page in pages:
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                tags = _tags(instance.get("Tags"))  # type: ignore[arg-type]
                hours = tags.get("MaxLifetimeHours", "")
                found.append(
                    TaskInstance(
                        id=instance["InstanceId"],
                        type=instance["InstanceType"],
                        state=instance["State"]["Name"],
                        run_id=tags.get("RunId"),
                        task_id=tags.get("TaskId"),
                        launched_at=instance["LaunchTime"],
                        max_hours=int(hours) if hours.isdigit() else None,
                    )
                )
    return sorted(found, key=lambda t: (t.launched_at, t.id))


def public_subnets(ec2: EC2Client, project: str) -> list[Subnet]:
    """The project's public subnets, in zone order: the launch fallback order."""
    response = ec2.describe_subnets(
        Filters=[
            {"Name": "tag:Project", "Values": [project]},
            {"Name": "map-public-ip-on-launch", "Values": ["true"]},
        ]
    )
    subnets = [Subnet(s["SubnetId"], s["AvailabilityZone"]) for s in response["Subnets"]]
    return sorted(subnets, key=lambda s: (s.zone, s.id))


def check_capacity_cap(running: list[TaskInstance], max_concurrent: int) -> None:
    """Refuse to launch when the project already runs max_concurrent tasks."""
    if len(running) < max_concurrent:
        return
    listing = ", ".join(f"{t.id} (run {t.run_id}, task {t.task_id})" for t in running)
    raise PreflightError(
        f"{len(running)} task instances already running (max_concurrent {max_concurrent}): "
        f"{listing}; wait for them or stop them with `task terminate`"
    )


def launch(
    ec2: EC2Client, spec: TaskSpec, template: str, subnets: list[Subnet], logger: Logger
) -> Launched:
    """Launch one on-demand instance from the template's default version,
    trying each subnet's zone in turn when one has no capacity."""
    if not subnets:
        raise PreflightError(
            f"no public subnet tagged Project={spec.project.project}; apply the bot stack first"
        )
    tags: list[TagTypeDef] = [{"Key": k, "Value": v} for k, v in sorted(task_tags(spec).items())]
    specifications: list[TagSpecificationTypeDef] = [
        {"ResourceType": "instance", "Tags": tags},
        {"ResourceType": "volume", "Tags": tags},
    ]
    for subnet in subnets:
        try:
            response = ec2.run_instances(
                LaunchTemplate={"LaunchTemplateName": template, "Version": "$Default"},
                InstanceType=spec.instance_type,  # type: ignore[arg-type]
                SubnetId=subnet.id,
                MinCount=1,
                MaxCount=1,
                TagSpecifications=specifications,
            )
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in CAPACITY_ERRORS:
                raise
            logger.warning(
                "no on-demand %s capacity in %s (%s); trying the next zone",
                spec.instance_type,
                subnet.zone,
                code,
            )
            continue
        instance = response["Instances"][0]
        return Launched(
            id=instance["InstanceId"],
            zone=instance.get("Placement", {}).get("AvailabilityZone", subnet.zone),
            image=instance.get("ImageId", "unknown"),
        )
    zones = ", ".join(s.zone for s in subnets)
    raise PreflightError(f"no on-demand {spec.instance_type} capacity in {zones}; re-run later")


# --- running one task -----------------------------------------------------------

#: Where the task script lands on the instance; delivered base64-encoded so no
#: argument can break out of it.
SCRIPT_PATH = "/root/task-runner.sh"
AGENT_TIMEOUT_SECONDS = 600
#: The script's last line. `stream` keeps reading until it sees it, because
#: the agent uploads output on its own schedule, after reporting the status.
END_MARKER = "== task-runner: end of output"
#: How long `stream` waits for the end marker after the command finished.
END_WAIT_SECONDS = 90
#: How long a just-launched instance may stay invisible to TerminateInstances.
NOT_FOUND_GRACE_SECONDS = 60
#: Consecutive transient errors `stream` rides out before giving up.
STREAM_RETRIES = 10
#: Errors that mean the credentials are gone or denied: retrying cannot help.
_FATAL = frozenset({
    "AccessDenied", "AccessDeniedException", "ExpiredToken", "ExpiredTokenException",
    "UnrecognizedClientException", "InvalidClientTokenId", "UnauthorizedOperation",
})  # fmt: skip
#: Run Command statuses after which nothing more happens.
_TERMINAL = frozenset({"Success", "Failed", "TimedOut", "Cancelled", "Terminated", "Undeliverable"})
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+$")
_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class TaskInterrupted(SitesError):  # noqa: N818 (a signal, not a fault)
    """SIGINT or SIGTERM arrived (GitHub cancelled the job, or Ctrl-C)."""

    exit_code = 130


def _check(spec: TaskSpec) -> None:
    script = PurePosixPath(spec.script)
    if not spec.script or script.is_absolute() or ".." in script.parts:
        raise ConfigError(f"script must be a relative path inside the repository: {spec.script!r}")
    if not _SHA.match(spec.ref):
        raise ConfigError(f"ref must be a full 40-character commit SHA: {spec.ref!r}")
    if not _REPOSITORY.match(spec.repository):
        raise ConfigError(f"repository must be owner/name: {spec.repository!r}")
    for field in ("run_id", "task_id"):
        if not _ID.match(getattr(spec, field)):
            raise ConfigError(
                f"{field} must be letters, digits, . _ and -: {getattr(spec, field)!r}"
            )


def results_prefix(spec: TaskSpec, prefix: str) -> str:
    return f"{prefix}{spec.run_id}/{spec.task_id}/"


def task_script(spec: TaskSpec, bucket: str, prefix: str) -> str:
    """The bash script the instance runs: wait for setup, reset the lifetime
    guard, fetch the commit anonymously, run the task, upload its outputs
    whatever its result, and exit with its code. No credential is involved:
    the repository is fetched over public HTTPS."""
    _check(spec)
    q = shlex.quote
    task = " ".join(q(part) for part in (spec.script, *spec.args))
    destination = f"s3://{bucket}/{results_prefix(spec, prefix)}"
    minutes = spec.max_hours * 60
    region = spec.project.region
    url = f"https://github.com/{spec.repository}"
    return f"""set -uo pipefail
exec 2>&1
export HOME=/root
export PATH=/root/.cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin

# cloud-init exits 2 on mere warnings; the ready file, written last by the
# setup script under set -e, is what says setup finished.
cloud-init status --wait >/dev/null 2>&1 || true
if [ ! -f /etc/task-runner/ready ]; then
  echo "task-runner: instance setup failed; the end of /var/log/task-runner-setup.log:"
  tail -n 40 /var/log/task-runner-setup.log || true
  exit 1
fi

# The lifetime guard now counts from the start of the task.
shutdown -c 2>/dev/null || true
shutdown -h +{minutes} 2>/dev/null

echo "== task-runner environment"
cat /etc/task-runner/ready
echo "=="

rm -rf /work
mkdir -p /work/repo /work/out
git init --quiet /work/repo
if ! git -C /work/repo fetch --quiet --depth 1 {q(url)} {q(spec.ref)}; then
  echo "task-runner: could not fetch {spec.ref} from {spec.repository}"
  exit 1
fi
git -C /work/repo checkout --quiet FETCH_HEAD
cd /work/repo || exit 1

status=0
OUT_DIR=/work/out bash {task} || status=$?

echo "== task-runner: the task exited with $status; uploading its outputs"
aws s3 cp --recursive --only-show-errors --region {q(region)} /work/out {q(destination)} \
  || echo "task-runner: uploading the outputs failed"
# The instance ends itself a minute from now (a shutdown terminates it), in
# case nothing else does; the minute lets the agent report and flush.
shutdown -h +1 2>/dev/null
echo "{END_MARKER}"
exit "$status"
"""


def task_commands(spec: TaskSpec, bucket: str, prefix: str) -> list[str]:
    """AWS-RunShellScript commands that write the task script and run it with bash."""
    encoded = base64.b64encode(task_script(spec, bucket, prefix).encode()).decode()
    return [f"echo {encoded} | base64 -d > {SCRIPT_PATH}", f"exec bash {SCRIPT_PATH}"]


def wait_for_agent(
    ssm: SSMClient,
    instance_id: str,
    timeout: float = AGENT_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll: float = 5.0,
) -> None:
    """Wait until the instance's SSM agent is online and can take a command."""
    deadline = clock() + timeout
    while True:
        response = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        if any(i.get("PingStatus") == "Online" for i in response["InstanceInformationList"]):
            return
        if clock() >= deadline:
            raise RunError(f"the SSM agent on {instance_id} was not online after {timeout:.0f}s")
        sleep(poll)


def send(
    ssm: SSMClient, instance_id: str, commands: list[str], log_group: str, max_hours: int
) -> str:
    """Start the task through Run Command, with its output streamed to CloudWatch."""
    response = ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Comment="jwall-engineering-sites task run",
        Parameters={"commands": commands, "executionTimeout": [str(max_hours * 3600)]},
        CloudWatchOutputConfig={
            "CloudWatchLogGroupName": log_group,
            "CloudWatchOutputEnabled": True,
        },
    )
    command_id: str = response["Command"]["CommandId"]
    return command_id


def _outcome(status: str, code: int) -> int:
    if status == "TimedOut":
        return 124
    if status == "Cancelled":
        return 130
    if status in ("Success", "Failed") and code >= 0:
        return code
    return 1


def _transient(exc: Exception) -> bool:
    if isinstance(exc, BotoCoreError):
        return True
    if isinstance(exc, ClientError):
        return exc.response["Error"]["Code"] not in _FATAL
    return False


def stream(
    logs: CloudWatchLogsClient,
    ssm: SSMClient,
    log_group: str,
    command_id: str,
    instance_id: str,
    write: Callable[[str], None],
    poll: float,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Print the task's output as it arrives, once per line, until the command
    finishes and its end marker arrives; return the task's exit code (124 timed
    out, 130 cancelled). Transient AWS errors are ridden out: a multi-hour task
    is not killed by one throttled poll."""
    seen: set[str] = set()
    since = 0
    ended = False
    failures = 0

    def drain() -> None:
        nonlocal since, ended
        kwargs: dict[str, Any] = {
            "logGroupName": log_group,
            "logStreamNamePrefix": f"{command_id}/{instance_id}/",
            "startTime": since,
        }
        while True:
            try:
                page = logs.filter_log_events(**kwargs)
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                    return
                raise
            for event in sorted(page.get("events", []), key=lambda e: e.get("timestamp", 0)):
                if event["eventId"] in seen:
                    continue
                if event.get("timestamp", 0) > since:
                    # Only events at the newest timestamp can come back.
                    seen.clear()
                    since = event.get("timestamp", 0)
                seen.add(event["eventId"])
                for line in event.get("message", "").rstrip("\n").split("\n"):
                    if line == END_MARKER:
                        ended = True
                    else:
                        write(line)
            token = page.get("nextToken")
            if not token:
                return
            kwargs["nextToken"] = token

    def attempt(step: Callable[[], Any]) -> tuple[bool, Any]:
        """Run one poll; on a transient error, count it and report no result."""
        nonlocal failures
        try:
            result = step()
        except (ClientError, BotoCoreError) as exc:
            if not _transient(exc):
                raise
            failures += 1
            if failures > STREAM_RETRIES:
                raise
            sleep(poll * min(failures, 6))
            return False, None
        failures = 0
        return True, result

    def invocation() -> dict[str, Any] | None:
        try:
            response: dict[str, Any] = dict(
                ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
            )
            return response
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvocationDoesNotExist":
                return None
            raise

    while True:
        ok, _ = attempt(drain)
        if not ok:
            continue
        ok, found = attempt(invocation)
        if not ok:
            continue
        if found is not None and found["Status"] in _TERMINAL:
            break
        sleep(poll)
    # The output may still be on its way: read until the end marker arrives.
    waited = 0.0
    while not ended and waited < END_WAIT_SECONDS:
        sleep(poll)
        waited += poll
        attempt(drain)
    return _outcome(found["Status"], found.get("ResponseCode", -1))


def collect(s3: S3Client, bucket: str, key_prefix: str, out_dir: Path) -> list[Path]:
    """Download everything the task wrote under key_prefix into out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    paths: list[Path] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=key_prefix):
        for obj in page.get("Contents", []):
            relative = obj["Key"][len(key_prefix) :]
            parts = PurePosixPath(relative).parts
            if not relative or relative.endswith("/") or ".." in parts:
                continue
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, obj["Key"], str(target))
            paths.append(target)
    return sorted(paths)


def terminate(
    ec2: EC2Client,
    instance_id: str,
    logger: Logger,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Terminate the instance. InvalidInstanceID.NotFound is retried for a
    minute (a just-launched instance can be invisible for seconds) before it
    is believed; returns whether the instance was found."""
    deadline = clock() + NOT_FOUND_GRACE_SECONDS
    while True:
        try:
            ec2.terminate_instances(InstanceIds=[instance_id])
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "InvalidInstanceID.NotFound":
                if clock() < deadline:
                    sleep(5)
                    continue
                logger.warning("%s was not found; it is already gone", instance_id)
                return False
            raise RunError(
                f"could not terminate {instance_id} ({code}); it shuts itself down within its "
                "maximum lifetime, or run `task terminate --instance` / `task sweep`"
            ) from exc
        logger.info("terminated %s", instance_id)
        return True


@contextmanager
def _interruptible() -> Iterator[Callable[[], None]]:
    """Turn the first SIGINT or SIGTERM into TaskInterrupted. The yielded
    function shields what follows (the cleanup) from later signals."""
    raised = False

    def handler(signum: int, frame: FrameType | None) -> None:
        nonlocal raised
        if raised:
            return
        raised = True
        raise TaskInterrupted(f"interrupted by {signal.Signals(signum).name}")

    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {s: signal.getsignal(s) for s in signals}

    def shield() -> None:
        for s in signals:
            signal.signal(s, signal.SIG_IGN)

    for s in signals:
        signal.signal(s, handler)
    try:
        yield shield
    finally:
        for s, h in previous.items():
            signal.signal(s, h)


def run_task(
    session: boto3.session.Session,
    spec: TaskSpec,
    out_dir: Path,
    logger: Logger,
    *,
    write: Callable[[str], None],
    poll: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run one task end to end and return its exit code. The instance is
    terminated on every path: success, task failure, AWS errors, timeouts
    and SIGINT/SIGTERM."""
    project = spec.project
    runner = project.task_runner
    if runner is None:
        logger.error("%s has no task-runner component", project.project)
        return ConfigError.exit_code
    try:
        script = task_commands(spec, project.task_bucket, project.task_prefix)
    except ConfigError as exc:
        logger.error("%s", exc)
        return exc.exit_code

    ec2 = session.client("ec2")
    ssm = session.client("ssm")
    logs = session.client("logs")
    s3 = session.client("s3")

    instance_id: str | None = None
    launching = False
    with _interruptible() as shield:
        try:
            running = running_tasks(ec2, project.project)
            twins = [t.id for t in running if (t.run_id, t.task_id) == (spec.run_id, spec.task_id)]
            if twins:
                raise PreflightError(
                    f"task {spec.task_id} of run {spec.run_id} is already running on "
                    f"{', '.join(twins)}; give each matrix leg its own task id"
                )
            check_capacity_cap(running, runner.max_concurrent)
            subnets = public_subnets(ec2, project.project)
            launching = True
            launched = launch(ec2, spec, project.task_template, subnets, logger)
            instance_id = launched.id
            logger.info(
                "launched %s: %s on-demand in %s, image %s; output follows",
                instance_id, spec.instance_type, launched.zone, launched.image,
            )  # fmt: skip
            wait_for_agent(ssm, instance_id, sleep=sleep)
            command_id = send(ssm, instance_id, script, project.task_log_group, spec.max_hours)
            code = stream(
                logs, ssm, project.task_log_group, command_id, instance_id, write, poll, sleep
            )
            logger.info("the task exited with %d", code)
            try:
                paths = collect(
                    s3, project.task_bucket, results_prefix(spec, project.task_prefix), out_dir
                )
                logger.info("copied %d output file(s) to %s", len(paths), out_dir)
            except (SitesError, ClientError, BotoCoreError) as exc:
                logger.error("could not copy the task's outputs: %s", exc)
                return code or 1
            return code
        except TaskInterrupted as exc:
            logger.warning("%s; terminating the task instance", exc)
            return exc.exit_code
        except SitesError as exc:
            logger.error("%s", exc)
            return exc.exit_code
        except (ClientError, BotoCoreError) as exc:
            logger.error("AWS call failed: %s", exc)
            return 1
        finally:
            shield()
            _cleanup(ec2, spec, instance_id, launching, logger)


def _cleanup(
    ec2: EC2Client, spec: TaskSpec, instance_id: str | None, launching: bool, logger: Logger
) -> None:
    if instance_id is None and launching:
        # Interrupted between RunInstances and recording the ID: find it by its tags.
        mine = [
            t.id
            for t in running_tasks(ec2, spec.project.project)
            if t.run_id == spec.run_id and t.task_id == spec.task_id
        ]
    else:
        mine = [instance_id] if instance_id else []
    for found in mine:
        try:
            terminate(ec2, found, logger)
        except RunError as exc:
            logger.error("%s", exc)
