"""Remote CI tasks: one task on one fresh, dedicated, on-demand EC2 instance.

`task run` launches an instance from the project's task launch template
(module task-runner), runs one script from the consumer repository on it
through SSM Run Command, streams its output from CloudWatch, copies its
outputs from S3 and terminates it, on every exit path. Instances are
on-demand only: a spot interruption would lose a long benchmark.
"""

from __future__ import annotations

import base64
import json
import math
import re
import shlex
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
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
    from mypy_boto3_pricing import PricingClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ssm import SSMClient

#: Tags the task-runner launch template puts on task instances and volumes,
#: which the task launch role requires and scopes termination by.
TASK_COMPONENT = "task-runner"
TASK_PURPOSE = "ci-task"

#: RunInstances errors that mean "not here, not now": try the next zone.
CAPACITY_ERRORS = frozenset({"InsufficientInstanceCapacity", "Unsupported"})
#: The same for spot, plus a price above the cap and the spot quota.
SPOT_CAPACITY_ERRORS = CAPACITY_ERRORS | {"SpotMaxPriceTooLow", "MaxSpotInstanceCountExceeded"}
#: Pause between two passes over every zone while spot keeps failing.
SPOT_RETRY_SECONDS = 20
#: How often `wait_for_slot` checks the running tasks again.
SLOT_POLL_SECONDS = 30

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
    market: str = "spot"
    max_price_fraction: float = 0.58
    on_demand_fallback: bool = True
    spot_wait_minutes: int = 5
    pull_request: str | None = None


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
    lifecycle: str


@dataclass(frozen=True)
class Subnet:
    id: str
    zone: str
    zone_id: str


@dataclass(frozen=True)
class PricePlan:
    """Where and at what price to try: subnets in launch order, the spot cap
    (None lets AWS cap at the on-demand price), and what was seen."""

    subnets: list[Subnet]
    max_price: str | None
    on_demand: float | None
    spot: dict[str, float]
    scores: dict[str, int]


class NoCapacity(PreflightError):  # noqa: N818 (an outcome, read as `no-capacity`)
    """No zone had capacity for the type, spot or on demand."""


def _tag_value(value: str) -> str:
    """EC2 tag values allow letters, digits, spaces and + - = . _ : / @."""
    return _TAG_VALUE_UNSAFE.sub("-", value)[:256]


def task_tags(spec: TaskSpec, lifecycle: str) -> dict[str, str]:
    """Every tag a task instance and its volume carry: the project file's six
    tags (RunInstances does not apply the provider's default tags), the
    launch template's two, and the run's."""
    project = spec.project
    tags = {
        **project.config.tags,
        "Component": TASK_COMPONENT,
        "Purpose": TASK_PURPOSE,
        "Name": _tag_value(f"{project.task_name}-{spec.run_id}-{spec.task_id}"),
        "Workflow": _tag_value(spec.workflow),
        "RunId": _tag_value(spec.run_id),
        "TaskId": _tag_value(spec.task_id),
        "MaxLifetimeHours": str(spec.max_hours),
        "Lifecycle": lifecycle,
    }
    if spec.pull_request:
        tags["PullRequest"] = _tag_value(spec.pull_request)
    return tags


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
    """The project's public subnets, in zone order."""
    response = ec2.describe_subnets(
        Filters=[
            {"Name": "tag:Project", "Values": [project]},
            {"Name": "map-public-ip-on-launch", "Values": ["true"]},
        ]
    )
    subnets = [
        Subnet(s["SubnetId"], s["AvailabilityZone"], s.get("AvailabilityZoneId", ""))
        for s in response["Subnets"]
    ]
    return sorted(subnets, key=lambda s: (s.zone, s.id))


def instance_types_info(ec2: EC2Client, types: list[str]) -> dict[str, tuple[int, int]]:
    """vCPUs and threads per core of each type."""
    response = ec2.describe_instance_types(InstanceTypes=types)  # type: ignore[arg-type]
    return {
        t["InstanceType"]: (
            t["VCpuInfo"]["DefaultVCpus"],
            t["VCpuInfo"].get("DefaultThreadsPerCore", 1),
        )
        for t in response["InstanceTypes"]
    }


def wait_for_slot(
    ec2: EC2Client,
    spec: TaskSpec,
    vcpus: int,
    max_vcpus: int,
    wait_seconds: float,
    logger: Logger,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Wait until the project's running tasks leave room for `vcpus` under
    `max_vcpus`, at most `wait_seconds`. A live instance with this task's
    run and task ID is refused at once: matrix legs must not share an ID."""
    if vcpus > max_vcpus:
        raise PreflightError(
            f"{spec.instance_type} has {vcpus} vCPUs, more than max_vcpus ({max_vcpus})"
        )
    deadline = clock() + wait_seconds
    sizes: dict[str, int] = {}
    while True:
        running = running_tasks(ec2, spec.project.project)
        twins = [t.id for t in running if (t.run_id, t.task_id) == (spec.run_id, spec.task_id)]
        if twins:
            raise PreflightError(
                f"task {spec.task_id} of run {spec.run_id} is already running on "
                f"{', '.join(twins)}; give each matrix leg its own task id"
            )
        unknown = sorted({t.type for t in running} - set(sizes))
        if unknown:
            sizes.update({k: v[0] for k, v in instance_types_info(ec2, unknown).items()})
        used = sum(sizes.get(t.type, 0) for t in running)
        if used + vcpus <= max_vcpus:
            return
        if clock() >= deadline:
            listing = ", ".join(f"{t.id} ({t.type}, run {t.run_id})" for t in running)
            raise PreflightError(
                f"no free slot after {wait_seconds / 60:.0f} minutes: {used} of {max_vcpus} "
                f"task vCPUs in use ({listing}); re-run later or stop them with `task terminate`"
            )
        logger.info("%d of %d task vCPUs in use; waiting for room for %d", used, max_vcpus, vcpus)
        sleep(SLOT_POLL_SECONDS)


def on_demand_price(pricing: PricingClient, instance_type: str, region: str) -> float | None:
    """The Linux on-demand hourly price of the type, from the Pricing API."""
    fields = {
        "instanceType": instance_type,
        "regionCode": region,
        "operatingSystem": "Linux",
        "tenancy": "Shared",
        "preInstalledSw": "NA",
        "capacitystatus": "Used",
    }
    response = pricing.get_products(
        ServiceCode="AmazonEC2",
        Filters=[{"Type": "TERM_MATCH", "Field": k, "Value": v} for k, v in fields.items()],
        MaxResults=10,
    )
    for raw in response.get("PriceList", []):
        item = json.loads(raw) if isinstance(raw, str) else raw
        for term in item.get("terms", {}).get("OnDemand", {}).values():
            for dimension in term.get("priceDimensions", {}).values():
                usd = dimension.get("pricePerUnit", {}).get("USD")
                if usd and float(usd) > 0:
                    return float(usd)
    return None


def price_plan(
    ec2: EC2Client, pricing: PricingClient, spec: TaskSpec, subnets: list[Subnet], logger: Logger
) -> PricePlan:
    """Resolve the spot cap and order the zones: best spot placement score
    first, then the cheapest current spot price. Lookups that fail are
    logged; launching never depends on them, except the cap."""
    region = spec.project.region
    try:
        on_demand = on_demand_price(pricing, spec.instance_type, region)
    except (ClientError, BotoCoreError) as exc:
        logger.warning("the Pricing API failed: %s", exc)
        on_demand = None
    if spec.market != "spot":
        return PricePlan(subnets=subnets, max_price=None, on_demand=on_demand, spot={}, scores={})
    max_price: str | None = None
    if on_demand is None:
        logger.warning(
            "no on-demand price for %s; spot is capped at the on-demand price by AWS",
            spec.instance_type,
        )
    else:
        # Rounded down: the cap must never exceed the fraction of on demand.
        cents = math.floor(on_demand * spec.max_price_fraction * 10_000)
        max_price = f"{cents / 10_000:.4f}"
    spot: dict[str, float] = {}
    scores: dict[str, int] = {}
    try:
        history = ec2.describe_spot_price_history(
            InstanceTypes=[spec.instance_type],  # type: ignore[list-item]
            ProductDescriptions=["Linux/UNIX"],
            StartTime=datetime.now(UTC),
        )
        latest: dict[str, tuple[datetime, float]] = {}
        for entry in history.get("SpotPriceHistory", []):
            zone, when = entry["AvailabilityZone"], entry["Timestamp"]
            if zone not in latest or when > latest[zone][0]:
                latest[zone] = (when, float(entry["SpotPrice"]))
        spot = {zone: price for zone, (_, price) in latest.items()}
    except (ClientError, BotoCoreError) as exc:
        logger.warning("could not read spot prices: %s", exc)
    try:
        response = ec2.get_spot_placement_scores(
            InstanceTypes=[spec.instance_type],
            TargetCapacity=1,
            SingleAvailabilityZone=True,
            RegionNames=[region],
        )
        by_id = {s["AvailabilityZoneId"]: s["Score"] for s in response["SpotPlacementScores"]}
        scores = {s.zone: by_id[s.zone_id] for s in subnets if s.zone_id in by_id}
    except (ClientError, BotoCoreError) as exc:
        logger.warning("could not read spot placement scores: %s", exc)
    ordered = sorted(
        subnets, key=lambda s: (-scores.get(s.zone, 0), spot.get(s.zone, math.inf), s.zone)
    )
    return PricePlan(
        subnets=ordered, max_price=max_price, on_demand=on_demand, spot=spot, scores=scores
    )


def _run_instance(
    ec2: EC2Client, spec: TaskSpec, template: str, subnet: Subnet, lifecycle: str,
    max_price: str | None,
) -> Launched:  # fmt: skip
    tags: list[TagTypeDef] = [
        {"Key": k, "Value": v} for k, v in sorted(task_tags(spec, lifecycle).items())
    ]
    specifications: list[TagSpecificationTypeDef] = [
        {"ResourceType": "instance", "Tags": tags},
        {"ResourceType": "volume", "Tags": tags},
    ]
    kwargs: dict[str, Any] = {
        "LaunchTemplate": {"LaunchTemplateName": template, "Version": "$Default"},
        "InstanceType": spec.instance_type,
        "SubnetId": subnet.id,
        "MinCount": 1,
        "MaxCount": 1,
        "TagSpecifications": specifications,
    }
    if lifecycle == "spot":
        options: dict[str, Any] = {
            "SpotInstanceType": "one-time",
            "InstanceInterruptionBehavior": "terminate",
        }
        if max_price is not None:
            options = {"MaxPrice": max_price, **options}
        kwargs["InstanceMarketOptions"] = {"MarketType": "spot", "SpotOptions": options}
    response = ec2.run_instances(**kwargs)
    instance = response["Instances"][0]
    return Launched(
        id=instance["InstanceId"],
        zone=instance.get("Placement", {}).get("AvailabilityZone", subnet.zone),
        image=instance.get("ImageId", "unknown"),
        lifecycle=lifecycle,
    )


def _try_zones(
    ec2: EC2Client, spec: TaskSpec, template: str, plan: PricePlan, lifecycle: str,
    logger: Logger,
) -> Launched | None:  # fmt: skip
    errors = SPOT_CAPACITY_ERRORS if lifecycle == "spot" else CAPACITY_ERRORS
    for subnet in plan.subnets:
        try:
            return _run_instance(ec2, spec, template, subnet, lifecycle, plan.max_price)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code not in errors:
                raise
            logger.warning(
                "no %s %s capacity in %s (%s); trying the next zone",
                lifecycle, spec.instance_type, subnet.zone, code,
            )  # fmt: skip
    return None


def launch(
    ec2: EC2Client,
    spec: TaskSpec,
    template: str,
    plan: PricePlan,
    logger: Logger,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Launched:
    """Launch one instance from the template's default version. Spot: every
    zone in plan order, again and again for spot_wait_minutes, then (if
    allowed) on demand. This is the only place on demand replaces spot:
    never once a task runs."""
    if not plan.subnets:
        raise PreflightError(
            f"no public subnet tagged Project={spec.project.project}; apply the bot stack first"
        )
    zones = ", ".join(s.zone for s in plan.subnets)
    if spec.market == "spot":
        deadline = clock() + spec.spot_wait_minutes * 60
        while True:
            launched = _try_zones(ec2, spec, template, plan, "spot", logger)
            if launched is not None:
                return launched
            if clock() >= deadline:
                break
            sleep(SPOT_RETRY_SECONDS)
        if not spec.on_demand_fallback:
            raise NoCapacity(
                f"no spot {spec.instance_type} capacity in {zones} for "
                f"{spec.spot_wait_minutes} minutes; re-run later"
            )
        logger.warning(
            "no spot %s capacity in %s for %d minutes; falling back to on demand",
            spec.instance_type, zones, spec.spot_wait_minutes,
        )  # fmt: skip
    launched = _try_zones(ec2, spec, template, plan, "on-demand", logger)
    if launched is not None:
        return launched
    if spec.market == "spot":
        raise NoCapacity(
            f"neither spot nor on-demand {spec.instance_type} capacity in {zones}; re-run later"
        )
    raise NoCapacity(f"no on-demand {spec.instance_type} capacity in {zones}; re-run later")


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
    clock: Callable[[], float] = time.monotonic,
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
    pricing = session.client("pricing", region_name="us-east-1")  # its endpoint's region
    ssm = session.client("ssm")
    logs = session.client("logs")
    s3 = session.client("s3")

    instance_id: str | None = None
    launching = False
    with _interruptible() as shield:
        try:
            info = instance_types_info(ec2, [spec.instance_type])
            vcpus, threads = info[spec.instance_type]
            if threads > 1:
                raise ConfigError(
                    f"{spec.instance_type} runs {threads} threads per core; tasks need "
                    "whole cores (an SMT-off type such as c8a)"
                )
            wait_for_slot(
                ec2, spec, vcpus, runner.max_vcpus, runner.slot_wait_minutes * 60, logger,
                sleep, clock,
            )  # fmt: skip
            plan = price_plan(ec2, pricing, spec, public_subnets(ec2, project.project), logger)
            launching = True
            launched = launch(ec2, spec, project.task_template, plan, logger, sleep, clock)
            instance_id = launched.id
            _log_launch(logger, spec, launched, plan)
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


def _log_launch(logger: Logger, spec: TaskSpec, launched: Launched, plan: PricePlan) -> None:
    """The plain lines before the task's own output: what it runs on, and at what price."""
    logger.info(
        "launched %s: %s, %s, in %s (spot placement score %s), image %s",
        launched.id, spec.instance_type, launched.lifecycle, launched.zone,
        plan.scores.get(launched.zone, "unknown"), launched.image,
    )  # fmt: skip
    on_demand = f"${plan.on_demand:.4f}/h" if plan.on_demand is not None else "unknown"
    if launched.lifecycle == "spot":
        current = plan.spot.get(launched.zone)
        logger.info(
            "spot price: %s/h now in %s, capped at %s (%.2f of on demand, %s)",
            f"${current:.4f}" if current is not None else "unknown", launched.zone,
            f"${plan.max_price}/h" if plan.max_price else "the on-demand price",
            spec.max_price_fraction, on_demand,
        )  # fmt: skip
    else:
        logger.info("on-demand price: %s", on_demand)
    logger.info("the CPU model and core count head the task's output; output follows")


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
