"""Types, the registry and the runner for preflight checks.

A check is a function registered with `@check(...)` for a stage and a set of
project kinds. Adding one is one decorated function; the orchestrator calls
`run_stage` and renders the results. The framework is generic: kinds are plain
strings and nothing here imports a provider.

The registry is module-global, filled at import time. Tests isolate it with
`isolated_registry()` (the checks tests do so in an autouse fixture).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from logging import Logger
from typing import TYPE_CHECKING, Any, Protocol

from ..errors import PreflightError
from ..project import Project
from ..settings import Settings

if TYPE_CHECKING:
    import boto3.session


class Stage(StrEnum):
    CONFIG = "config"
    LIVE = "live"
    BETWEEN = "between"
    AFTER = "after"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    title: str
    outcome: Outcome
    severity: Severity
    facts: dict[str, str]
    remediation: str | None
    runbook: str | None  # "docs/databricks.md#<anchor>"


class DatabricksApi(Protocol):
    """What a Databricks check may use; Task 6's client satisfies it."""

    @property
    def account(self) -> Any: ...

    def workspace(self, host: str) -> Any: ...

    account_outputs: Mapping[str, Any] | None  # BETWEEN / AFTER


@dataclass(frozen=True)
class CheckContext:
    project: Project
    settings: Settings
    logger: Logger
    session: boto3.session.Session | None  # None at CONFIG
    databricks: DatabricksApi | None  # None at CONFIG


CheckFn = Callable[[CheckContext], Iterable[CheckResult]]

KINDS = frozenset({"site", "bot", "research", "databricks"})

_ID = re.compile(r"^[A-Z]{2,5}-[A-Z]{2,5}-\d{3}$")


@dataclass(frozen=True)
class RegisteredCheck:
    check_id: str
    title: str
    stage: Stage
    kinds: frozenset[str]
    severity: Severity
    runbook: str | None
    fn: CheckFn


_REGISTRY: dict[str, RegisteredCheck] = {}


@contextmanager
def isolated_registry() -> Iterator[None]:
    """Run with an empty registry and restore the previous one afterwards."""
    global _REGISTRY
    saved = _REGISTRY
    _REGISTRY = {}
    try:
        yield
    finally:
        _REGISTRY = saved


def check(
    check_id: str,
    title: str,
    stage: Stage,
    kinds: frozenset[str],
    severity: Severity = Severity.ERROR,
    runbook: str | None = None,
) -> Callable[[CheckFn], CheckFn]:
    """Register a check. A malformed or duplicate id raises when the decorator is applied."""
    if not _ID.fullmatch(check_id):
        raise ValueError(f"check id {check_id!r} must match {_ID.pattern}")
    if not kinds or not kinds <= KINDS:
        raise ValueError(
            f"check kinds {sorted(kinds)} must be a non-empty subset of {sorted(KINDS)}"
        )

    def register(fn: CheckFn) -> CheckFn:
        if check_id in _REGISTRY:
            raise ValueError(f"check id {check_id} is already registered")
        _REGISTRY[check_id] = RegisteredCheck(check_id, title, stage, kinds, severity, runbook, fn)
        return fn

    return register


def registered(stage: Stage, kind: str) -> list[RegisteredCheck]:
    """The checks for a stage and project kind, ordered by id."""
    return sorted(
        (c for c in _REGISTRY.values() if c.stage == stage and kind in c.kinds),
        key=lambda c: c.check_id,
    )


def run_stage(stage: Stage, ctx: CheckContext) -> list[CheckResult]:
    """Run every check for the stage and the project's kind.

    A check that raises becomes a FAIL result with `facts["error"]` (the exception's type;
    its message and traceback go to the debug log); one broken
    check never stops the others.
    """
    results: list[CheckResult] = []
    for registered_check in registered(stage, ctx.project.config.kind):
        try:
            # Items yielded before an exception are kept on purpose; the crash FAIL follows them.
            for item in registered_check.fn(ctx):
                if not isinstance(item, CheckResult):
                    raise TypeError(f"yielded {type(item).__name__}, expected CheckResult")
                results.append(item)
        except Exception as exc:
            ctx.logger.debug("check %s raised", registered_check.check_id, exc_info=True)
            results.append(
                CheckResult(
                    check_id=registered_check.check_id,
                    title=registered_check.title,
                    outcome=Outcome.FAIL,
                    severity=registered_check.severity,
                    # The type only: an SDK's message may quote a credential, and the table
                    # and checks.json are not redacted. The (redacted) debug log has it all.
                    facts={"error": type(exc).__name__},
                    remediation="The check itself failed to run; the debug log has the traceback.",
                    runbook=registered_check.runbook,
                )
            )
    return results


def raise_on_errors(results: Sequence[CheckResult]) -> None:
    """Raise `PreflightError` (exit code 3) naming each failed ERROR check."""
    failed = [r for r in results if r.outcome is Outcome.FAIL and r.severity is Severity.ERROR]
    if not failed:
        return
    lines = [f"{len(failed)} preflight check(s) failed:"]
    for r in failed:
        line = f"  {r.check_id} {r.title}"
        if r.remediation:
            line += f": {r.remediation}"
        if r.runbook:
            line += f" ({r.runbook})"
        lines.append(line)
    raise PreflightError("\n".join(lines))


__all__ = [
    "CheckContext",
    "CheckFn",
    "CheckResult",
    "DatabricksApi",
    "Outcome",
    "RegisteredCheck",
    "Severity",
    "Stage",
    "check",
    "isolated_registry",
    "raise_on_errors",
    "registered",
    "run_stage",
]
