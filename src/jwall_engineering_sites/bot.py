"""AWS calls for bot projects: the secret, the shared parameters, the
instance, the status view and the container's logs."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from logging import Logger
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from .errors import PreflightError, RunError
from .project import Project

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_ssm import SSMClient

#: The `Component` tag the container-instance module puts on the instance.
INSTANCE_COMPONENT = "container-instance"
FOLLOW_POLL_SECONDS = 5.0


def put_secret(ssm: SSMClient, name: str, value: str) -> None:
    """Write (or rotate) a SecureString with the account's default SSM key."""
    try:
        ssm.put_parameter(
            Name=name, Value=value, Type="SecureString", Overwrite=True, Tier="Standard"
        )
    except ClientError as exc:
        raise RunError(f"could not write {name}: {exc.response['Error']['Code']}") from exc


def secret_exists(ssm: SSMClient, name: str) -> bool:
    response = ssm.describe_parameters(ParameterFilters=[{"Key": "Name", "Values": [name]}])
    return bool(response.get("Parameters"))


def get_parameters(ssm: SSMClient, names: list[str]) -> dict[str, str | None]:
    response = ssm.get_parameters(Names=names)
    found = {p["Name"]: p["Value"] for p in response.get("Parameters", [])}
    return {name: found.get(name) for name in names}


def put_parameter(ssm: SSMClient, name: str, value: str) -> None:
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)


@dataclass(frozen=True)
class InstanceInfo:
    id: str
    state: str
    public_ip: str | None


def find_instance(
    ec2: EC2Client, project: str, component: str = INSTANCE_COMPONENT
) -> InstanceInfo | None:
    """The project's bot instance, by its Project and Component tags."""
    response = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Project", "Values": [project]},
            {"Name": "tag:Component", "Values": [component]},
            {
                "Name": "instance-state-name",
                "Values": ["pending", "running", "stopping", "stopped"],
            },
        ]
    )
    instances = [i for r in response.get("Reservations", []) for i in r.get("Instances", [])]
    if not instances:
        return None
    if len(instances) > 1:
        ids = ", ".join(i["InstanceId"] for i in instances)
        raise PreflightError(
            f"{len(instances)} instances are tagged Project={project}, Component={component} "
            f"({ids}); exactly one bot may hold the token"
        )
    instance = instances[0]
    return InstanceInfo(
        id=instance["InstanceId"],
        state=instance["State"]["Name"],
        public_ip=instance.get("PublicIpAddress"),
    )


@dataclass(frozen=True)
class BotStatus:
    instance: InstanceInfo | None
    desired: str | None
    running: str | None
    status: str | None
    last_sync: str | None
    profile_url: str | None


def status(session: boto3.session.Session, project: Project) -> BotStatus:
    names = [project.parameter(n) for n in ("desired-version", "running-version", "status")]
    names.append(project.parameter("last-sync"))
    values = get_parameters(session.client("ssm"), names)
    return BotStatus(
        instance=find_instance(session.client("ec2"), project.project),
        desired=values[names[0]],
        running=values[names[1]],
        status=values[names[2]],
        last_sync=values[names[3]],
        profile_url=project.profile_url,
    )


def render_status(s: BotStatus) -> str:
    instance = (
        f"{s.instance.id} ({s.instance.state}, {s.instance.public_ip or 'no public IP'})"
        if s.instance
        else "none"
    )
    rows = [
        ("instance", instance),
        ("desired version", s.desired or "not set"),
        ("running version", s.running or "not set"),
        ("status", s.status or "not set"),
        ("last records sync", s.last_sync or "never"),
        ("lichess profile", s.profile_url or "not configured (lichess_user)"),
    ]
    width = max(len(k) for k, _ in rows) + 2
    return "\n".join(f"{k + ':':<{width}}{v}" for k, v in rows)


def tail_logs(
    logs: CloudWatchLogsClient,
    group: str,
    since_seconds: int,
    follow: bool,
    logger: Logger,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    max_polls: int | None = None,
) -> Iterator[str]:
    """Events of the log group as `<iso time> <stream> <message>`, oldest first.
    With `follow`, keeps polling every 5 s (forever unless `max_polls`)."""
    start = int(now() * 1000) - since_seconds * 1000
    polls = 0
    while True:
        token: str | None = None
        while True:
            kwargs: dict[str, object] = {"logGroupName": group, "startTime": start}
            if token:
                kwargs["nextToken"] = token
            try:
                page = logs.filter_log_events(**kwargs)  # type: ignore[arg-type]
            except ClientError as exc:
                raise RunError(f"cannot read {group}: {exc.response['Error']['Code']}") from exc
            for event in page.get("events", []):
                stamp = datetime.fromtimestamp(event["timestamp"] / 1000, tz=UTC)
                start = max(start, event["timestamp"] + 1)
                yield (
                    f"{stamp.isoformat(timespec='seconds')} {event['logStreamName']} "
                    f"{event['message'].rstrip()}"
                )
            token = page.get("nextToken")
            if not token:
                break
        if not follow or (max_polls is not None and polls >= max_polls):
            return
        polls += 1
        logger.debug("following %s", group)
        sleep(FOLLOW_POLL_SECONDS)
