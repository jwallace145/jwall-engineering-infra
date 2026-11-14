"""Render check results for the console, the plan comment and checks.json."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from ..report import _cell
from .framework import CheckResult, Outcome, Severity

_ICONS = {Outcome.PASS: "PASS", Outcome.FAIL: "FAIL", Outcome.SKIP: "SKIP"}
_MD_ICONS = {Outcome.PASS: "✅ pass", Outcome.FAIL: "❌ fail", Outcome.SKIP: "⏭️ skip"}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _md(text: str) -> str:
    return _cell(_flat(text))


def _facts(result: CheckResult) -> str:
    return ", ".join(f"{k}={v}" for k, v in result.facts.items())


def _label(result: CheckResult) -> str:
    label = _ICONS[result.outcome]
    if result.outcome is Outcome.FAIL and result.severity is Severity.WARNING:
        label = "WARN"
    return label


def console_table(results: Sequence[CheckResult]) -> str:
    rows = [(r.check_id, _label(r), r.title) for r in results]
    width = max((len(r[0]) for r in rows), default=0)
    lines = [f"{cid:<{width}}  {label:<4}  {title}" for cid, label, title in rows]
    return "\n".join(_with_details(results, lines))


def _with_details(results: Sequence[CheckResult], lines: list[str]) -> list[str]:
    out: list[str] = []
    for result, line in zip(results, lines, strict=True):
        out.append(line)
        if result.outcome is Outcome.FAIL:
            if result.facts:
                out.append(f"    {_facts(result)}")
            if result.remediation:
                out.append(f"    fix: {result.remediation}")
            if result.runbook:
                out.append(f"    runbook: {result.runbook}")
    return out


def markdown_table(results: Sequence[CheckResult]) -> str:
    lines = [
        "| Check | Result | Severity | Details | Remediation | Runbook |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        remediation = _md(r.remediation) if r.remediation else ""
        runbook = f"`{_md(r.runbook)}`" if r.runbook else ""
        lines.append(
            f"| `{r.check_id}` {_md(r.title)} | {_MD_ICONS[r.outcome]} | {r.severity.value} "
            f"| {_md(_facts(r))} | {remediation} | {runbook} |"
        )
    return "\n".join(lines)


def write_json(results: Sequence[CheckResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in results], indent=2) + "\n")
