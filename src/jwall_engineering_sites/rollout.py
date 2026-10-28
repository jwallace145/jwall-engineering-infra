"""What a release does to a bot project, and following a deploy to its end.

`decide` is pure: given the releases, the tags already in ECR and the version
running, which version to ship and whether to build, push and deploy.

A deploy writes the desired version, marks the status `pending:<version>`,
and asks the instance to reconcile through Run Command. The command only
starts the reconciler as its own systemd unit and returns; the swap itself
may wait a whole game. `follow` therefore watches the status and
running-version parameters the reconciler writes, not the command.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from logging import Logger
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from .errors import PreflightError, RunError
from .project import Project
from .releases import Release, find, newest

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient

TRIGGER = "container-reconcile-async"
PENDING = "pending:"
FAILED = "failed:"
TRIGGER_GRACE_SECONDS = 30


@dataclass(frozen=True)
class Plan:
    version: str
    build: bool
    push: bool
    deploy: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def decide(
    requested: str | None,
    releases: list[Release],
    present_tags: set[str],
    running: str | None,
    track: str,
) -> Plan:
    latest = newest(releases)
    if latest is None:
        raise PreflightError("the repository has no published release (vX.Y.Z)")
    version = find(releases, requested).tag if requested else latest.tag
    missing = version not in present_tags
    is_newest = version == latest.tag
    deploy = track == "latest-release" and is_newest and running != version
    if not missing and not deploy:
        if running == version:
            reason = f"{version} is already built and already running"
        else:
            reason = (
                f"{version} is already built; it is not the newest release, so it is not deployed"
            )
    elif missing and not deploy:
        reason = (
            f"{version} is not in the repository yet; build and push it"
            + ("" if is_newest else "; it is not the newest release, so it is not deployed")
            + (f"; {version} is already running" if running == version else "")
        )
    else:
        reason = (
            f"{version} is the newest release"
            + ("; build and push it, then deploy it" if missing else "; deploy it")
            + f" (running: {running or 'nothing'})"
        )
    return Plan(version=version, build=missing, push=missing, deploy=deploy, reason=reason)


def send_reconcile(ssm: SSMClient, instance_id: str, restart: bool = False) -> str:
    command = TRIGGER + (" --restart" if restart else "")
    try:
        response = ssm.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [command], "executionTimeout": ["120"]},
            Comment="jwall-engineering-sites deploy",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        raise RunError(
            f"could not send the reconcile command to {instance_id} ({code}); the desired "
            "version is set and the instance converges to it at its next boot"
        ) from exc
    return response["Command"]["CommandId"]


def _parameters(ssm: SSMClient, project: Project) -> tuple[str | None, str | None, str | None]:
    names = [
        project.parameter("status"),
        project.parameter("running-version"),
        project.parameter("desired-version"),
    ]
    found = {p["Name"]: p["Value"] for p in ssm.get_parameters(Names=names).get("Parameters", [])}
    return found.get(names[0]), found.get(names[1]), found.get(names[2])


def _not_yet(exc: BaseException) -> bool:
    """Only InvocationDoesNotExist means the command has not reached the agent yet."""
    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") == "InvocationDoesNotExist"
    return True


def follow(
    ssm: SSMClient,
    command_id: str,
    instance_id: str,
    project: Project,
    version: str,
    logger: Logger,
    poll_seconds: float = 15,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    max_seconds: float = 3 * 3600,
    not_yet: tuple[type[BaseException], ...] = (ClientError,),
) -> str:
    """Wait for the trigger to succeed, then for the reconciler to finish.

    Returns "ok" when the status is `ok` and the running version is `version`,
    or "superseded" when a newer deploy replaced `version` as the desired
    version (the reconciler then ships the newer one). Raises RunError on a
    failed trigger, a `failed:` status, or after `max_seconds` (the swap then
    continues on the instance)."""
    started = clock()

    # 1. The trigger command: it only starts a systemd unit, so it is quick.
    while True:
        try:
            invocation: Any = ssm.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id
            )
        except not_yet as exc:
            if not _not_yet(exc):
                raise RunError(
                    f"cannot follow the reconcile command on {instance_id}: {exc}"
                ) from exc
            if clock() - started > TRIGGER_GRACE_SECONDS:
                raise RunError(f"the reconcile command never reached {instance_id}: {exc}") from exc
            sleep(2)
            continue
        state = invocation.get("Status")
        if state == "Success":
            break
        if state in (
            "Failed",
            "TimedOut",
            "Cancelled",
            "Cancelling",
            "Undeliverable",
            "Terminated",
        ):
            raise RunError(
                f"the reconcile command {state.lower()} on {instance_id}: "
                f"{(invocation.get('StandardErrorContent') or '').strip()[-1000:]}"
            )
        sleep(2)

    # 2. The reconciler, through the parameters it writes.
    last: tuple[str | None, str | None] | None = None
    while True:
        status, running, desired = _parameters(ssm, project)
        if (status, running) != last:
            logger.info("status: %s (running %s)", status or "not set", running or "nothing")
            last = (status, running)
        if desired and desired != version:
            logger.info("%s was superseded by a newer deploy of %s", version, desired)
            return "superseded"
        if status and status.startswith(FAILED):
            raise RunError(f"deploy of {version} failed on the instance: {status}")
        if status == "ok" and running == version:
            return "ok"
        if clock() - started > max_seconds:
            raise RunError(
                f"still waiting after {int(max_seconds)}s: {status}; the swap continues on the "
                "instance; check `bot status`"
            )
        sleep(poll_seconds)
