"""Databricks `between` checks: readiness waits after the account stack, before the workspace stack.

They read `ctx.databricks.account_outputs` (the account stack's outputs). Every
check SKIPs until `databricks.enabled` is true, like the live checks. No
failure message carries the client secret or an SDK exception's text.
Registered at import; `checks.load_builtin_checks()` imports this module.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.service.iam import WorkspacePermission
from databricks.sdk.service.provisioning import WorkspaceStatus

from ..errors import PreflightError
from ._results import Meta
from .databricks_live import _account, _live, _section
from .framework import CheckContext, CheckResult, Stage

POLL_SECONDS = 15
#: Statuses a workspace does not leave on its own: waiting longer cannot help.
_TERMINAL = frozenset({WorkspaceStatus.FAILED, WorkspaceStatus.BANNED, WorkspaceStatus.CANCELLING})

#: Injection points: tests replace them so the polling loop runs instantly.
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic

BTW_001 = Meta("DBX-BTW-001", "The workspace is RUNNING", "workspace-readiness")
BTW_002 = Meta(
    "DBX-BTW-002", "The workspace URL answers an authenticated call", "workspace-readiness"
)
BTW_003 = Meta("DBX-BTW-003", "The service principal is a workspace admin", "workspace-admin")
BTW_004 = Meta("DBX-BTW-004", "The metastore is assigned to the workspace", "metastore-assignment")


def outputs(ctx: CheckContext) -> dict[str, Any]:
    """The account stack's outputs; a stage that runs without them is a bug in the caller."""
    if ctx.databricks is None or ctx.databricks.account_outputs is None:
        raise PreflightError("a between/after check needs the account stack's outputs")
    return dict(ctx.databricks.account_outputs)


def workspace_client(ctx: CheckContext) -> Any:
    assert ctx.databricks is not None  # outputs() ran first
    return ctx.databricks.workspace(str(outputs(ctx)["workspace_url"]))


@_live(BTW_001, needs_ids=True, stage=Stage.BETWEEN)
def workspace_running(ctx: CheckContext) -> Iterable[CheckResult]:
    workspace_id = int(outputs(ctx)["workspace_id"])
    timeout = _section(ctx).readiness_timeout_seconds
    workspaces = _account(ctx).workspaces
    start = _clock()
    while True:
        try:
            current = workspaces.get(workspace_id)
        except NotFound:
            yield BTW_001.failed(
                f"workspace {workspace_id} does not exist in the account: the account stack "
                "creates it, so re-run apply",
                workspace_id=str(workspace_id),
            )
            return
        status, message = current.workspace_status, current.workspace_status_message or ""
        label = status.value if status is not None else "unknown"
        if status is WorkspaceStatus.RUNNING:
            yield BTW_001.passed(workspace_id=str(workspace_id), status=label)
            return
        if status in _TERMINAL:
            yield BTW_001.failed(
                f"the workspace is {label}: {message or 'no status message'}. Fix the cause "
                "in the account console (or the cross-account role, credentials and network "
                "it names), then re-run apply",
                status=label,
                message=message,
            )
            return
        if _clock() - start + POLL_SECONDS > timeout:
            yield BTW_001.failed(
                f"the workspace was still {label} after "
                f"{_section(ctx).readiness_timeout}: {message or 'no status message'}. "
                "Re-running apply waits again; raise databricks.readiness_timeout in "
                "config.yaml if provisioning is just slow",
                status=label,
                message=message,
            )
            return
        _sleep(POLL_SECONDS)


@_live(BTW_002, needs_ids=True, stage=Stage.BETWEEN, workspace_level=True)
def workspace_answers(ctx: CheckContext) -> Iterable[CheckResult]:
    host = str(outputs(ctx)["workspace_url"])
    try:
        me = workspace_client(ctx).current_user.me()
    except (DatabricksError, OSError, ValueError) as exc:  # keep the type only
        yield BTW_002.failed(
            f"{host} did not answer an authenticated call: the workspace may still be "
            "starting, or the service principal is not assigned to it (DBX-BTW-003)",
            host=host,
            error=type(exc).__name__,
        )
        return
    yield BTW_002.passed(host=host, principal=str(me.user_name))


@_live(BTW_003, needs_ids=True, stage=Stage.BETWEEN, workspace_level=True)
def principal_is_workspace_admin(ctx: CheckContext) -> Iterable[CheckResult]:
    """The admin group holds ADMIN on the workspace (account level, authoritative) and an
    admin-only workspace read works (the service principal's rights are effective)."""
    out = outputs(ctx)
    workspace_id, admin_group = int(out["workspace_id"]), str(out["admin_group_name"])
    assignments = _account(ctx).workspace_assignment.list(workspace_id)
    held = any(
        a.principal is not None
        and a.principal.group_name == admin_group
        and WorkspacePermission.ADMIN in (a.permissions or [])
        for a in assignments
    )
    if not held:
        yield BTW_003.failed(
            f"the group {admin_group} does not hold ADMIN on workspace {workspace_id}: the "
            "account stack assigns it (after the metastore assignment); re-run apply",
            admin_group=admin_group,
        )
        return
    try:
        workspace_client(ctx).workspace_conf.get_status(keys="enableTokensConfig")
    except PermissionDenied:
        yield BTW_003.failed(
            f"{admin_group} holds ADMIN but the service principal cannot yet read admin "
            "settings: the admin assignment is not yet effective; re-run apply (propagation)",
            admin_group=admin_group,
            error="PermissionDenied",
        )
        return
    yield BTW_003.passed(admin_group=admin_group)


@_live(BTW_004, needs_ids=True, stage=Stage.BETWEEN)
def metastore_assigned(ctx: CheckContext) -> Iterable[CheckResult]:
    out = outputs(ctx)
    workspace_id, expected = int(out["workspace_id"]), str(out["metastore_id"])
    try:
        found = _account(ctx).metastore_assignments.get(workspace_id)
    except NotFound:
        yield BTW_004.failed(
            f"workspace {workspace_id} has no metastore assignment: re-run apply (the account "
            "stack assigns it), or assign it in the account console",
            workspace_id=str(workspace_id),
        )
        return
    assignment = found.metastore_assignment
    actual = assignment.metastore_id if assignment is not None else None
    if actual != expected:
        yield BTW_004.failed(
            f"workspace {workspace_id} is assigned metastore {actual}, not {expected}: "
            "unassign it in the account console, then re-run apply",
            expected=expected,
            actual=str(actual),
        )
        return
    yield BTW_004.passed(metastore_id=expected)
