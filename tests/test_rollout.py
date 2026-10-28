"""Rollout decisions and following a deploy on the instance."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from jwall_engineering_sites import rollout
from jwall_engineering_sites.errors import PreflightError, RunError
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.releases import Release

RELEASES = [Release("v0.15.0"), Release("v0.14.0"), Release("v0.13.0")]


def test_decide_newest_missing_tag() -> None:
    plan = rollout.decide(None, RELEASES, set(), "v0.14.0", "latest-release")
    assert (plan.version, plan.build, plan.push, plan.deploy) == ("v0.15.0", True, True, True)


def test_decide_present_and_running() -> None:
    plan = rollout.decide(None, RELEASES, {"v0.15.0"}, "v0.15.0", "latest-release")
    assert (plan.build, plan.push, plan.deploy) == (False, False, False)
    assert "already running" in plan.reason


def test_decide_present_not_running() -> None:
    plan = rollout.decide(None, RELEASES, {"v0.15.0"}, "v0.14.0", "latest-release")
    assert (plan.build, plan.push, plan.deploy) == (False, False, True)


def test_decide_requested_older_release() -> None:
    plan = rollout.decide("v0.13.0", RELEASES, set(), "v0.15.0", "latest-release")
    assert (plan.version, plan.build, plan.push, plan.deploy) == ("v0.13.0", True, True, False)
    assert "not the newest" in plan.reason


def test_decide_requested_not_a_release() -> None:
    with pytest.raises(PreflightError, match="v9"):
        rollout.decide("v9.0.0", RELEASES, set(), None, "latest-release")


def test_decide_without_releases() -> None:
    with pytest.raises(PreflightError, match="no published release"):
        rollout.decide(None, [], set(), None, "latest-release")


class FakeSSM:
    """get_command_invocation returns `invocations` in order; parameters come
    from `states` (one dict per poll after the trigger succeeded)."""

    def __init__(self, invocations: list[Any], states: list[dict[str, str]]) -> None:
        self.invocations = invocations
        self.states = states
        self.current: dict[str, str] = {}

    def get_command_invocation(self, **kwargs: Any) -> dict[str, Any]:
        item = self.invocations.pop(0) if len(self.invocations) > 1 else self.invocations[0]
        if isinstance(item, Exception):
            raise item
        result: dict[str, Any] = item
        return result

    def get_parameters(self, Names: list[str]) -> dict[str, Any]:  # noqa: N803
        if self.states:
            self.current = self.states.pop(0)
        return {
            "Parameters": [
                {"Name": n, "Value": self.current[n.rsplit("/", 1)[1]]}
                for n in Names
                if n.rsplit("/", 1)[1] in self.current
            ]
        }


class NotYetError(Exception):
    """Stands in for botocore's InvocationDoesNotExist."""


def _follow(ssm: FakeSSM, bot: Project, version: str = "v0.15.0") -> str:
    clock = iter(range(0, 100_000, 15))
    return rollout.follow(
        ssm,  # type: ignore[arg-type]
        "cmd-1",
        "i-1",
        bot,
        version,
        logging.getLogger("t"),
        sleep=lambda _: None,
        clock=lambda: float(next(clock)),
        not_yet=(NotYetError,),
    )


def test_follow_waits_through_waiting_status(bot: Project) -> None:
    ssm = FakeSSM(
        [NotYetError(), {"Status": "InProgress"}, {"Status": "Success"}],
        [
            {"status": "pending:v0.15.0", "running-version": "v0.14.0"},
            {"status": "waiting:v0.14.0", "running-version": "v0.14.0"},
            {"status": "waiting:v0.14.0", "running-version": "v0.14.0"},
            {"status": "ok", "running-version": "v0.15.0"},
        ],
    )
    assert _follow(ssm, bot) == "ok"


def test_follow_does_not_trust_a_stale_ok_while_pending(bot: Project) -> None:
    ssm = FakeSSM(
        [{"Status": "Success"}],
        [
            {"status": "pending:v0.14.0", "running-version": "v0.14.0"},
            {"status": "pending:v0.14.0", "running-version": "v0.14.0"},
            {"status": "ok", "running-version": "v0.14.0"},
        ],
    )
    assert _follow(ssm, bot, "v0.14.0") == "ok"
    assert ssm.states == [], "every state was polled before returning"


def test_follow_raises_on_failed_status(bot: Project) -> None:
    ssm = FakeSSM(
        [{"Status": "Success"}],
        [{"status": "failed:v0.15.0 did not become healthy; v0.14.0 restored"}],
    )
    with pytest.raises(RunError, match=r"v0\.14\.0 restored"):
        _follow(ssm, bot)


def test_follow_raises_on_trigger_failure(bot: Project) -> None:
    ssm = FakeSSM([{"Status": "Failed", "StandardErrorContent": "systemd-run: not found"}], [])
    with pytest.raises(RunError, match="systemd-run: not found"):
        _follow(ssm, bot)


def test_follow_gives_up_without_touching_the_instance(bot: Project) -> None:
    ssm = FakeSSM([{"Status": "Success"}], [{"status": "waiting:v0.14.0"}])
    clock = iter(range(0, 10**9, 3600))
    with pytest.raises(RunError, match="the swap continues on the instance"):
        rollout.follow(
            ssm,  # type: ignore[arg-type]
            "cmd-1",
            "i-1",
            bot,
            "v0.15.0",
            logging.getLogger("t"),
            sleep=lambda _: None,
            clock=lambda: float(next(clock)),
            max_seconds=7200,
        )


def test_send_reconcile_uses_the_async_trigger() -> None:
    sent: dict[str, Any] = {}

    class SSM:
        def send_command(self, **kwargs: Any) -> dict[str, Any]:
            sent.update(kwargs)
            return {"Command": {"CommandId": "cmd-9"}}

    assert rollout.send_reconcile(SSM(), "i-1", restart=True) == "cmd-9"  # type: ignore[arg-type]
    assert sent["InstanceIds"] == ["i-1"]
    assert sent["DocumentName"] == "AWS-RunShellScript"
    assert sent["Parameters"]["commands"] == ["container-reconcile-async --restart"]
