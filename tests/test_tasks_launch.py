"""Launching task instances: tags, on-demand only, capacity fallback, the cap."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
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


def _any(fake: object) -> Any:
    """A fake passed where a boto3 client is typed."""
    return fake


def _spec(bot: Project, **overrides: Any) -> tasks.TaskSpec:
    values: dict[str, Any] = {
        "project": bot,
        "instance_type": "c8a.16xlarge",
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


SUBNETS = [
    tasks.Subnet("subnet-a", "us-east-1a", "use1-az6"),
    tasks.Subnet("subnet-b", "us-east-1b", "use1-az1"),
]


class FakeEC2:
    """Records run_instances calls; fails per (subnet, market) with the given codes."""

    def __init__(self, failures: dict[tuple[str, str], str] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[dict[str, Any]] = []

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        market = "spot" if "InstanceMarketOptions" in kwargs else "on-demand"
        code = self.failures.get((kwargs["SubnetId"], market)) or self.failures.get(
            (kwargs["SubnetId"], "*")
        )
        if code:
            raise ClientError({"Error": {"Code": code, "Message": "no"}}, "RunInstances")
        zone = {"subnet-a": "us-east-1a", "subnet-b": "us-east-1b"}[kwargs["SubnetId"]]
        return {
            "Instances": [
                {
                    "InstanceId": f"i-{kwargs['SubnetId']}",
                    "ImageId": "ami-1",
                    "Placement": {"AvailabilityZone": zone},
                }
            ]
        }


class Clock:
    """A fake clock that sleep advances."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _plan(max_price: str | None = "2.0002") -> tasks.PricePlan:
    return tasks.PricePlan(
        subnets=SUBNETS, max_price=max_price, on_demand=3.44864,
        spot={"us-east-1a": 1.39, "us-east-1b": 0.93}, scores={"us-east-1a": 1, "us-east-1b": 1},
    )  # fmt: skip


def _launch(ec2: FakeEC2, spec: tasks.TaskSpec, clock: Clock | None = None) -> tasks.Launched:
    clock = clock or Clock()
    return tasks.launch(_any(ec2), spec, "tmpl", _plan(), LOG, sleep=clock.sleep, clock=clock)


def test_task_tags(bot: Project) -> None:
    tags = tasks.task_tags(_spec(bot, pull_request="84"), "spot")
    assert tags == {
        **bot.config.tags,
        "Component": "task-runner",
        "Purpose": "ci-task",
        "Profile": "cpu",
        "Name": "jwall-engineering-rusty-chess-task-123-1-sprt-0",
        "Workflow": "SPRT",
        "RunId": "123-1",
        "TaskId": "sprt-0",
        "MaxLifetimeHours": "6",
        "Lifecycle": "spot",
        "PullRequest": "84",
    }


def test_task_tags_carry_the_profile(bot: Project) -> None:
    tags = tasks.task_tags(_spec(bot, profile="gpu"), "on-demand")
    assert tags["Profile"] == "gpu" and tags["Purpose"] == "ci-task"


def test_task_tags_without_a_pull_request(bot: Project) -> None:
    tags = tasks.task_tags(_spec(bot), "on-demand")
    assert tags["Lifecycle"] == "on-demand" and "PullRequest" not in tags


def test_task_tags_sanitize_values(bot: Project) -> None:
    tags = tasks.task_tags(_spec(bot, workflow="SPRT (nightly) #1 ✓"), "spot")
    assert tags["Workflow"] == "SPRT -nightly- -1 -"


def test_spot_launch_request(bot: Project) -> None:
    ec2 = FakeEC2()
    launched = _launch(ec2, _spec(bot))
    # Taken from the RunInstances response: a describe right after launch can
    # still say InvalidInstanceID.NotFound.
    assert launched == tasks.Launched("i-subnet-a", "us-east-1a", "ami-1", "spot")
    (call,) = ec2.calls
    assert call["LaunchTemplate"] == {"LaunchTemplateName": "tmpl", "Version": "$Default"}
    assert call["InstanceType"] == "c8a.16xlarge"
    assert (call["MinCount"], call["MaxCount"]) == (1, 1)
    assert call["SubnetId"] == "subnet-a"
    assert call["InstanceMarketOptions"] == {
        "MarketType": "spot",
        "SpotOptions": {
            "MaxPrice": "2.0002",
            "SpotInstanceType": "one-time",
            "InstanceInterruptionBehavior": "terminate",
        },
    }
    expected = [
        {"Key": k, "Value": v} for k, v in sorted(tasks.task_tags(_spec(bot), "spot").items())
    ]
    assert call["TagSpecifications"] == [
        {"ResourceType": "instance", "Tags": expected},
        {"ResourceType": "volume", "Tags": expected},
    ]


def test_on_demand_market_sends_no_spot_options(bot: Project) -> None:
    ec2 = FakeEC2()
    launched = _launch(ec2, _spec(bot, market="on-demand"))
    assert launched.lifecycle == "on-demand"
    assert "InstanceMarketOptions" not in ec2.calls[0]
    assert {"Key": "Lifecycle", "Value": "on-demand"} in ec2.calls[0]["TagSpecifications"][0][
        "Tags"
    ]


@pytest.mark.parametrize(
    "code",
    ["InsufficientInstanceCapacity", "Unsupported", "SpotMaxPriceTooLow",
     "MaxSpotInstanceCountExceeded"],
)  # fmt: skip
def test_spot_tries_the_next_zone(
    bot: Project, code: str, caplog: pytest.LogCaptureFixture
) -> None:
    ec2 = FakeEC2({("subnet-a", "spot"): code})
    with caplog.at_level(logging.WARNING):
        assert _launch(ec2, _spec(bot)).id == "i-subnet-b"
    assert [c["SubnetId"] for c in ec2.calls] == ["subnet-a", "subnet-b"]
    assert "us-east-1a" in caplog.text and code in caplog.text


def test_spot_retries_then_falls_back_to_on_demand(
    bot: Project, caplog: pytest.LogCaptureFixture
) -> None:
    ec2 = FakeEC2({("subnet-a", "spot"): "InsufficientInstanceCapacity",
                   ("subnet-b", "spot"): "InsufficientInstanceCapacity"})  # fmt: skip
    clock = Clock()
    with caplog.at_level(logging.WARNING):
        launched = _launch(ec2, _spec(bot), clock)
    assert launched == tasks.Launched("i-subnet-a", "us-east-1a", "ami-1", "on-demand")
    spot_calls = [c for c in ec2.calls if "InstanceMarketOptions" in c]
    assert len(spot_calls) >= 4  # every zone, more than once
    assert clock.now >= 5 * 60  # spot_wait_minutes
    assert "falling back to on demand" in caplog.text
    # The fallback is the last call: never spot again after it.
    assert "InstanceMarketOptions" not in ec2.calls[-1]


def test_no_fallback_means_no_capacity(bot: Project) -> None:
    ec2 = FakeEC2({("subnet-a", "spot"): "InsufficientInstanceCapacity",
                   ("subnet-b", "spot"): "InsufficientInstanceCapacity"})  # fmt: skip
    with pytest.raises(tasks.NoCapacity) as error:
        _launch(ec2, _spec(bot, on_demand_fallback=False))
    assert error.value.exit_code == 3
    assert "no spot c8a.16xlarge capacity in us-east-1a, us-east-1b" in str(error.value)
    assert all("InstanceMarketOptions" in c for c in ec2.calls)


def test_the_account_vcpu_limit_is_no_capacity(bot: Project) -> None:
    """An account-wide quota: trying the next zone cannot help."""
    ec2 = FakeEC2({("subnet-a", "*"): "VcpuLimitExceeded"})
    with pytest.raises(tasks.NoCapacity, match=r"'Running On-Demand Standard .*Service Quotas"):
        _launch(ec2, _spec(bot, market="on-demand"))
    assert len(ec2.calls) == 1


def test_the_gpu_quota_is_named_with_how_to_raise_it(bot: Project) -> None:
    """Acceptance 5: an exhausted G and VT quota fails at launch, naming it."""
    ec2 = FakeEC2({("subnet-a", "*"): "VcpuLimitExceeded"})
    with pytest.raises(tasks.NoCapacity) as error:
        _launch(ec2, _spec(bot, instance_type="g6.xlarge", market="on-demand"))
    message = str(error.value)
    assert "'Running On-Demand G and VT instances'" in message
    assert "Service Quotas console (Amazon EC2, us-east-1" in message
    assert error.value.exit_code == 3


def test_the_spot_quota_message_names_the_spot_quota() -> None:
    message = tasks.quota_message("g6e.xlarge", "spot", "us-east-1")
    assert "'All G and VT Spot Instance Requests'" in message


def test_no_capacity_anywhere(bot: Project) -> None:
    ec2 = FakeEC2({("subnet-a", "*"): "InsufficientInstanceCapacity",
                   ("subnet-b", "*"): "InsufficientInstanceCapacity"})  # fmt: skip
    with pytest.raises(tasks.NoCapacity, match="nor on-demand"):
        _launch(ec2, _spec(bot))


def test_launch_other_errors_are_not_retried(bot: Project) -> None:
    ec2 = FakeEC2({("subnet-a", "*"): "UnauthorizedOperation"})
    with pytest.raises(ClientError):
        _launch(ec2, _spec(bot))
    assert len(ec2.calls) == 1


def test_launch_needs_a_subnet(bot: Project) -> None:
    plan = tasks.PricePlan(subnets=[], max_price=None, on_demand=None, spot={}, scores={})
    clock = Clock()
    with pytest.raises(PreflightError, match="no public subnet"):
        tasks.launch(_any(FakeEC2()), _spec(bot), "tmpl", plan, LOG, sleep=clock.sleep, clock=clock)


# --- prices and placement -------------------------------------------------------


class FakePricing:
    def __init__(self, price: str | None) -> None:
        self.price = price
        self.calls: list[dict[str, Any]] = []

    def get_products(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.price is None:
            return {"PriceList": []}
        item = {"terms": {"OnDemand": {"T": {"priceDimensions": {"D": {
            "unit": "Hrs", "pricePerUnit": {"USD": self.price}}}}}}}  # fmt: skip
        return {"PriceList": [json.dumps(item)]}


class FakePriceEC2:
    def describe_spot_price_history(self, **kwargs: Any) -> dict[str, Any]:
        return {"SpotPriceHistory": [
            {"AvailabilityZone": "us-east-1a", "SpotPrice": "1.39", "Timestamp": NOW},
            {"AvailabilityZone": "us-east-1b", "SpotPrice": "0.93", "Timestamp": NOW},
            {"AvailabilityZone": "us-east-1b", "SpotPrice": "0.80", "Timestamp": HOUR_AGO},
            {"AvailabilityZone": "us-east-1d", "SpotPrice": "1.27", "Timestamp": NOW},
        ]}  # fmt: skip

    def get_spot_placement_scores(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["SingleAvailabilityZone"] is True
        return {"SpotPlacementScores": [
            {"AvailabilityZoneId": "use1-az6", "Score": 1},
            {"AvailabilityZoneId": "use1-az1", "Score": 1},
            {"AvailabilityZoneId": "use1-az4", "Score": 3},
        ]}  # fmt: skip


NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
HOUR_AGO = NOW - timedelta(hours=1)
THREE = [*SUBNETS, tasks.Subnet("subnet-d", "us-east-1d", "use1-az4")]


def test_on_demand_price() -> None:
    pricing = FakePricing("3.4486400000")
    assert tasks.on_demand_price(_any(pricing), "c8a.16xlarge", "us-east-1") == 3.44864
    filters = {f["Field"]: f["Value"] for f in pricing.calls[0]["Filters"]}
    assert filters["instanceType"] == "c8a.16xlarge" and filters["regionCode"] == "us-east-1"
    assert filters["operatingSystem"] == "Linux" and filters["capacitystatus"] == "Used"


def test_price_plan_caps_spot_and_orders_zones(bot: Project) -> None:
    plan = tasks.price_plan(
        _any(FakePriceEC2()), _any(FakePricing("3.44864")), _spec(bot), THREE, LOG
    )
    assert plan.max_price == "2.0002"  # 0.58 x 3.44864 = 2.00021, rounded down to 4 places
    assert plan.on_demand == 3.44864
    assert plan.spot == {"us-east-1a": 1.39, "us-east-1b": 0.93, "us-east-1d": 1.27}
    # Placement score first (1d scores 3), then the cheaper zone.
    assert [s.zone for s in plan.subnets] == ["us-east-1d", "us-east-1b", "us-east-1a"]


def test_price_plan_without_the_pricing_api_lets_aws_cap_at_on_demand(
    bot: Project, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        plan = tasks.price_plan(
            _any(FakePriceEC2()), _any(FakePricing(None)), _spec(bot), THREE, LOG
        )
    assert plan.max_price is None and plan.on_demand is None
    assert "capped at the on-demand price" in caplog.text


def test_price_plan_for_on_demand_skips_spot_lookups(bot: Project) -> None:
    plan = tasks.price_plan(
        _any(object()), _any(FakePricing("3.44864")), _spec(bot, market="on-demand"), THREE, LOG
    )
    assert plan.max_price is None and plan.spot == {} and plan.subnets == THREE


# --- the vCPU cap -----------------------------------------------------------------


class FakeTypesEC2:
    def describe_instance_types(self, InstanceTypes: list[str]) -> dict[str, Any]:  # noqa: N803
        table = {"c8a.16xlarge": (64, 1), "c8a.8xlarge": (32, 1), "c8a.xlarge": (4, 1),
                 "c7i.16xlarge": (64, 2), "g6.xlarge": (4, 2)}  # fmt: skip
        return {"InstanceTypes": [
            {"InstanceType": t, "VCpuInfo": {"DefaultVCpus": table[t][0],
                                              "DefaultThreadsPerCore": table[t][1]}}
            for t in InstanceTypes
        ]}  # fmt: skip


def test_instance_types_info() -> None:
    info = tasks.instance_types_info(_any(FakeTypesEC2()), ["c8a.16xlarge", "c7i.16xlarge"])
    assert info == {"c8a.16xlarge": (64, 1), "c7i.16xlarge": (64, 2)}


def _task(
    i: int, type_: str = "c8a.16xlarge", run: str = "r", task: str = "t", profile: str = "cpu"
) -> tasks.TaskInstance:
    return tasks.TaskInstance(f"i-{i}", type_, "running", run, task, NOW, 6, profile)


def _gpu_spec(bot: Project, **overrides: Any) -> tasks.TaskSpec:
    values: dict[str, Any] = {"instance_type": "g6.xlarge", "market": "on-demand",
                              "profile": "gpu", "whole_cores": False, "gpu": True}  # fmt: skip
    values.update(overrides)
    return _spec(bot, **values)


def test_a_gpu_task_does_not_wait_on_cpu_tasks(
    bot: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 4: a full cpu budget leaves the gpu budget untouched."""
    monkeypatch.setattr(tasks, "running_tasks", lambda ec2, p: [_task(i) for i in range(3)])
    clock = Clock()
    tasks.wait_for_slot(_any(FakeTypesEC2()), _gpu_spec(bot), 4, 4, 1800, LOG, clock.sleep, clock)
    assert clock.sleeps == []


def test_a_cpu_task_does_not_wait_on_gpu_tasks(
    bot: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = [_task(1, "g6.xlarge", profile="gpu")]
    monkeypatch.setattr(tasks, "running_tasks", lambda ec2, p: running)
    clock = Clock()
    tasks.wait_for_slot(_any(FakeTypesEC2()), _spec(bot), 64, 64, 1800, LOG, clock.sleep, clock)
    assert clock.sleeps == []


def test_gpu_tasks_share_the_gpu_budget(bot: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    running = [_task(1, "g6.xlarge", profile="gpu")]
    monkeypatch.setattr(tasks, "running_tasks", lambda ec2, p: running)
    clock = Clock()
    with pytest.raises(PreflightError, match="4 of 4 gpu task vCPUs in use"):
        tasks.wait_for_slot(_any(FakeTypesEC2()), _gpu_spec(bot), 4, 4, 60, LOG, clock.sleep, clock)


def test_a_duplicate_task_id_is_refused_across_profiles(
    bot: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = [_task(1, run="123-1", task="sprt-0")]
    monkeypatch.setattr(tasks, "running_tasks", lambda ec2, p: running)
    clock = Clock()
    with pytest.raises(PreflightError, match="already running"):
        tasks.wait_for_slot(_any(FakeTypesEC2()), _gpu_spec(bot), 4, 4, 0, LOG, clock.sleep, clock)


def test_untagged_instances_count_as_cpu() -> None:
    assert tasks.TaskInstance("i-1", "c8a.xlarge", "running", None, None, NOW, 6).profile == "cpu"


def test_slot_free_returns_at_once(bot: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "running_tasks", lambda ec2, p: [_task(1), _task(2)])
    clock = Clock()
    tasks.wait_for_slot(_any(FakeTypesEC2()), _spec(bot), 64, 192, 1800, LOG, clock.sleep, clock)
    assert clock.sleeps == []


def test_slot_waits_until_one_frees(
    bot: Project, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fleets = iter([[_task(1), _task(2), _task(3)], [_task(1), _task(2), _task(3)], [_task(1)]])
    monkeypatch.setattr(tasks, "running_tasks", lambda ec2, p: next(fleets))
    clock = Clock()
    with caplog.at_level(logging.INFO):
        tasks.wait_for_slot(
            _any(FakeTypesEC2()), _spec(bot), 64, 192, 1800, LOG, clock.sleep, clock
        )
    assert len(clock.sleeps) == 2
    assert "192 of 192 cpu task vCPUs in use" in caplog.text


def test_slot_wait_times_out(bot: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "running_tasks", lambda ec2, p: [_task(1, "c8a.8xlarge")] * 6)
    clock = Clock()
    with pytest.raises(PreflightError) as error:
        tasks.wait_for_slot(
            _any(FakeTypesEC2()), _spec(bot), 64, 192, 1800, LOG, clock.sleep, clock
        )
    assert error.value.exit_code == 3
    assert "no free slot after 30 minutes" in str(error.value)
    assert clock.now >= 1800


def test_slot_refuses_a_twin(bot: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two matrix legs with one task id would kill and read each other's work."""
    monkeypatch.setattr(
        tasks, "running_tasks", lambda ec2, p: [_task(7, run="123-1", task="sprt-0")]
    )
    clock = Clock()
    with pytest.raises(PreflightError, match="already running on i-7"):
        tasks.wait_for_slot(
            _any(FakeTypesEC2()), _spec(bot), 64, 192, 1800, LOG, clock.sleep, clock
        )
    assert clock.sleeps == []


def test_slot_refuses_a_task_bigger_than_the_cap(bot: Project) -> None:
    clock = Clock()
    with pytest.raises(PreflightError, match="max_vcpus"):
        tasks.wait_for_slot(_any(FakeTypesEC2()), _spec(bot), 64, 32, 1800, LOG, clock.sleep, clock)


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
    assert [(s.id, s.zone) for s in subnets] == [
        (ids["us-east-1a"], "us-east-1a"),
        (ids["us-east-1b"], "us-east-1b"),
    ]
    assert all(s.zone_id.startswith("use1-az") for s in subnets)
