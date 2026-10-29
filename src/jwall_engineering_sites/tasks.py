"""Remote CI tasks: one task on one fresh, dedicated, on-demand EC2 instance.

`task run` launches an instance from the project's task launch template
(module task-runner), runs one script from the consumer repository on it
through SSM Run Command, streams its output from CloudWatch, copies its
outputs from S3 and terminates it, on every exit path. Instances are
on-demand only: a spot interruption would lose a long benchmark.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from logging import Logger
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from .errors import PreflightError
from .project import Project

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ec2.type_defs import TagSpecificationTypeDef, TagTypeDef

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
) -> str:
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
        instance_id: str = response["Instances"][0]["InstanceId"]
        return instance_id
    zones = ", ".join(s.zone for s in subnets)
    raise PreflightError(f"no on-demand {spec.instance_type} capacity in {zones}; re-run later")
