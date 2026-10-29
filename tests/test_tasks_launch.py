"""Launching task instances: tags, on-demand only, capacity fallback, the cap."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from jwall_engineering_sites import tasks
from jwall_engineering_sites.errors import PreflightError
from jwall_engineering_sites.project import Project

REGION = "us-east-1"
LOG = logging.getLogger("test")


def _spec(bot: Project, **overrides: Any) -> tasks.TaskSpec:
    values: dict[str, Any] = {
        "project": bot,
        "instance_type": "c8a.xlarge",
        "max_hours": 6,
        "run_id": "123-1",
        "task_id": "sprt-0",
        "workflow": "SPRT",
        "repository": "jwallace145/rusty-chess-v2",
        "ref": "0123456789abcdef0123456789abcdef01234567",
        "script": "scripts/sprt.sh",
        "args": ("--games", "200"),
    }
    values.update(overrides)
    return tasks.TaskSpec(**values)


SUBNETS = [tasks.Subnet("subnet-a", "us-east-1a"), tasks.Subnet("subnet-b", "us-east-1b")]


class FakeEC2:
    """Records run_instances calls; fails in the listed subnets with the given codes."""

    def __init__(self, failures: dict[str, str] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[dict[str, Any]] = []

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        code = self.failures.get(kwargs["SubnetId"])
        if code:
            raise ClientError({"Error": {"Code": code, "Message": "no"}}, "RunInstances")
        return {"Instances": [{"InstanceId": f"i-{kwargs['SubnetId']}"}]}


def test_task_tags(bot: Project) -> None:
    tags = tasks.task_tags(_spec(bot))
    assert tags == {
        **bot.config.tags,
        "Component": "task-runner",
        "Purpose": "ci-task",
        "Name": "jwall-engineering-rusty-chess-task-123-1-sprt-0",
        "Workflow": "SPRT",
        "RunId": "123-1",
        "TaskId": "sprt-0",
        "MaxLifetimeHours": "6",
    }


def test_task_tags_sanitize_values(bot: Project) -> None:
    tags = tasks.task_tags(_spec(bot, workflow="SPRT (nightly) #1 ✓"))
    assert tags["Workflow"] == "SPRT -nightly- -1 -"


def test_launch_request(bot: Project) -> None:
    ec2 = FakeEC2()
    instance_id = tasks.launch(ec2, _spec(bot), "tmpl", SUBNETS, LOG)  # type: ignore[arg-type]
    assert instance_id == "i-subnet-a"
    (call,) = ec2.calls
    assert call["LaunchTemplate"] == {"LaunchTemplateName": "tmpl", "Version": "$Default"}
    assert call["InstanceType"] == "c8a.xlarge"
    assert (call["MinCount"], call["MaxCount"]) == (1, 1)
    assert call["SubnetId"] == "subnet-a"
    # On-demand only: no spot request of any kind.
    assert "InstanceMarketOptions" not in call
    expected = [{"Key": k, "Value": v} for k, v in sorted(tasks.task_tags(_spec(bot)).items())]
    assert call["TagSpecifications"] == [
        {"ResourceType": "instance", "Tags": expected},
        {"ResourceType": "volume", "Tags": expected},
    ]


@pytest.mark.parametrize("code", ["InsufficientInstanceCapacity", "Unsupported"])
def test_launch_falls_back_to_the_next_zone(
    bot: Project, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    ec2 = FakeEC2({"subnet-a": code})
    with caplog.at_level(logging.WARNING):
        assert tasks.launch(ec2, _spec(bot), "tmpl", SUBNETS, LOG) == "i-subnet-b"  # type: ignore[arg-type]
    assert [c["SubnetId"] for c in ec2.calls] == ["subnet-a", "subnet-b"]
    assert all("InstanceMarketOptions" not in c for c in ec2.calls)
    assert "us-east-1a" in caplog.text and "c8a.xlarge" in caplog.text


def test_launch_exhausted(bot: Project) -> None:
    ec2 = FakeEC2({"subnet-a": "InsufficientInstanceCapacity", "subnet-b": "Unsupported"})
    with pytest.raises(PreflightError) as error:
        tasks.launch(ec2, _spec(bot), "tmpl", SUBNETS, LOG)  # type: ignore[arg-type]
    assert str(error.value) == (
        "no on-demand c8a.xlarge capacity in us-east-1a, us-east-1b; re-run later"
    )


def test_launch_other_errors_are_not_retried(bot: Project) -> None:
    ec2 = FakeEC2({"subnet-a": "UnauthorizedOperation"})
    with pytest.raises(ClientError):
        tasks.launch(ec2, _spec(bot), "tmpl", SUBNETS, LOG)  # type: ignore[arg-type]
    assert len(ec2.calls) == 1


def test_launch_needs_a_subnet(bot: Project) -> None:
    with pytest.raises(PreflightError, match="no public subnet"):
        tasks.launch(FakeEC2(), _spec(bot), "tmpl", [], LOG)  # type: ignore[arg-type]


def _running(n: int) -> list[tasks.TaskInstance]:
    return [
        tasks.TaskInstance(
            id=f"i-{i}", type="c8a.xlarge", state="running", run_id=f"r{i}", task_id="t",
            launched_at=datetime(2026, 10, 29, tzinfo=UTC), max_hours=6,
        )
        for i in range(n)
    ]  # fmt: skip


def test_cap_allows_below_the_limit() -> None:
    tasks.check_capacity_cap(_running(3), 4)


def test_cap_refuses_at_the_limit() -> None:
    with pytest.raises(PreflightError) as error:
        tasks.check_capacity_cap(_running(4), 4)
    assert "4 task instances already running (max_concurrent 4)" in str(error.value)
    assert "i-0" in str(error.value) and "i-3" in str(error.value)


def _ec2_instance(ec2: Any, tags: dict[str, str]) -> str:
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    reservation = ec2.run_instances(
        ImageId=image, MinCount=1, MaxCount=1, InstanceType="c8a.xlarge",
        TagSpecifications=[
            {"ResourceType": "instance", "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}
        ],
    )  # fmt: skip
    instance_id: str = reservation["Instances"][0]["InstanceId"]
    return instance_id


def test_running_tasks_ignores_the_bot_and_other_projects() -> None:
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        task = _ec2_instance(
            ec2,
            {"Project": "rusty-chess", "Purpose": "ci-task", "RunId": "9-1", "TaskId": "a",
             "MaxLifetimeHours": "6"},
        )  # fmt: skip
        _ec2_instance(ec2, {"Project": "rusty-chess", "Component": "container-instance"})
        _ec2_instance(ec2, {"Project": "other", "Purpose": "ci-task"})
        stopped = _ec2_instance(ec2, {"Project": "rusty-chess", "Purpose": "ci-task"})
        ec2.terminate_instances(InstanceIds=[stopped])
        running = tasks.running_tasks(ec2, "rusty-chess")
    assert [t.id for t in running] == [task]
    assert (running[0].run_id, running[0].task_id, running[0].max_hours) == ("9-1", "a", 6)
    assert running[0].type == "c8a.xlarge"


def test_public_subnets_sorted_by_zone() -> None:
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        ids = {}
        for cidr, zone, public, project in [
            ("10.0.2.0/24", "us-east-1b", True, "rusty-chess"),
            ("10.0.1.0/24", "us-east-1a", True, "rusty-chess"),
            ("10.0.3.0/24", "us-east-1c", False, "rusty-chess"),
            ("10.0.4.0/24", "us-east-1d", True, "other"),
        ]:
            subnet = ec2.create_subnet(VpcId=vpc, CidrBlock=cidr, AvailabilityZone=zone)["Subnet"]
            ec2.create_tags(
                Resources=[subnet["SubnetId"]], Tags=[{"Key": "Project", "Value": project}]
            )
            if public:
                ec2.modify_subnet_attribute(
                    SubnetId=subnet["SubnetId"], MapPublicIpOnLaunch={"Value": True}
                )
            ids[zone] = subnet["SubnetId"]
        subnets = tasks.public_subnets(ec2, "rusty-chess")
    assert subnets == [
        tasks.Subnet(ids["us-east-1a"], "us-east-1a"),
        tasks.Subnet(ids["us-east-1b"], "us-east-1b"),
    ]
