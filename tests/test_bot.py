"""AWS helpers for bot projects: secrets, parameters, the instance, status, logs."""

from __future__ import annotations

import logging
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jwall_engineering_sites import bot as botlib
from jwall_engineering_sites.errors import PreflightError
from jwall_engineering_sites.project import Project

REGION = "us-east-1"


def _instance(ec2: Any, project: str, component: str = "container-instance") -> str:
    image = ec2.describe_images(Owners=["amazon"])["Images"][0]["ImageId"]
    reservation = ec2.run_instances(
        ImageId=image,
        MinCount=1,
        MaxCount=1,
        InstanceType="t3.micro",
        TagSpecifications=[
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "Project", "Value": project},
                    {"Key": "Component", "Value": component},
                ],
            }
        ],
    )
    instance_id: str = reservation["Instances"][0]["InstanceId"]
    return instance_id


def test_put_and_check_secret() -> None:
    with mock_aws():
        ssm = boto3.client("ssm", region_name=REGION)
        name = "/jwall-engineering/rusty-chess/lichess-token"
        assert botlib.secret_exists(ssm, name) is False
        botlib.put_secret(ssm, name, "lip_x")
        assert botlib.secret_exists(ssm, name) is True
        param = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]
        assert param["Type"] == "SecureString" and param["Value"] == "lip_x"
        botlib.put_secret(ssm, name, "lip_y")  # rotation overwrites
        assert ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"] == "lip_y"


def test_get_and_put_parameters() -> None:
    with mock_aws():
        ssm = boto3.client("ssm", region_name=REGION)
        botlib.put_parameter(ssm, "/p/desired-version", "v0.14.0")
        assert botlib.get_parameters(ssm, ["/p/desired-version", "/p/status"]) == {
            "/p/desired-version": "v0.14.0",
            "/p/status": None,
        }


def test_find_instance_by_tags() -> None:
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        instance_id = _instance(ec2, "rusty-chess")
        _instance(ec2, "other-project")
        found = botlib.find_instance(ec2, "rusty-chess")
        assert found is not None
        assert found.id == instance_id
        assert found.state == "running"


def test_find_instance_none() -> None:
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        assert botlib.find_instance(ec2, "rusty-chess") is None


def test_find_instance_ambiguous() -> None:
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        _instance(ec2, "rusty-chess")
        _instance(ec2, "rusty-chess")
        with pytest.raises(PreflightError, match="2 instances"):
            botlib.find_instance(ec2, "rusty-chess")


def test_find_instance_ignores_terminated() -> None:
    with mock_aws():
        ec2 = boto3.client("ec2", region_name=REGION)
        old = _instance(ec2, "rusty-chess")
        ec2.terminate_instances(InstanceIds=[old])
        new = _instance(ec2, "rusty-chess")
        found = botlib.find_instance(ec2, "rusty-chess")
        assert found is not None and found.id == new


def test_status_reads_everything(bot: Project) -> None:
    with mock_aws():
        session = boto3.session.Session(region_name=REGION)
        ec2, ssm = session.client("ec2"), session.client("ssm")
        instance_id = _instance(ec2, "rusty-chess")
        for name, value in (
            ("desired-version", "v0.14.0"),
            ("running-version", "v0.13.0"),
            ("status", "waiting:v0.13.0"),
        ):
            botlib.put_parameter(ssm, bot.parameter(name), value)
        status = botlib.status(session, bot)
        assert status.instance is not None and status.instance.id == instance_id
        assert (status.desired, status.running, status.status, status.last_sync) == (
            "v0.14.0",
            "v0.13.0",
            "waiting:v0.13.0",
            None,
        )
        text = botlib.render_status(status)
        assert "desired version:   v0.14.0" in text
        assert "last records sync: never" in text
        assert "https://lichess.org/@/rusty-chess-bot" in text


def test_status_renders_missing_values() -> None:
    text = botlib.render_status(botlib.BotStatus(None, None, None, None, None, None))
    assert "instance:" in text and "none" in text
    assert "desired version:   not set" in text


def test_tail_logs_pages_and_follows() -> None:
    calls: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = [
        {
            "events": [{"timestamp": 1_000, "logStreamName": "app-v1", "message": "a\n"}],
            "nextToken": "t",
        },
        {"events": [{"timestamp": 2_000, "logStreamName": "app-v1", "message": "b"}]},
        {"events": [{"timestamp": 3_000, "logStreamName": "app-v1", "message": "c"}]},
        {"events": []},
    ]

    class Logs:
        def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return pages.pop(0)

    sleeps: list[float] = []
    lines = list(
        botlib.tail_logs(
            Logs(),  # type: ignore[arg-type]
            "/jwall-engineering/rusty-chess/bot",
            since_seconds=600,
            follow=True,
            logger=logging.getLogger("t"),
            sleep=sleeps.append,
            now=lambda: 10.0,
            max_polls=2,
        )
    )
    assert [line.split(" ", 2)[2] for line in lines] == ["a", "b", "c"]
    assert lines[0].split(" ")[1] == "app-v1"
    assert calls[0]["startTime"] == 10_000 - 600_000
    assert calls[1]["nextToken"] == "t"
    assert calls[2]["startTime"] == 2_001, "follow resumes after the last event"
    assert sleeps == [5.0, 5.0]
