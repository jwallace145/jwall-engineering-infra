"""The Markdown the tool writes: sticky PR comments and step summaries.

Every comment is a branded card: a centred product header, one GitHub alert
that says the single most important thing (no changes / changes / destructive
changes / failed), the detail a reviewer needs without expanding anything, and
a provenance footer. GitHub strips styles from comments, so the layout uses
only what its Markdown keeps: `align`, alerts, tables, `<details>` and `<sub>`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .stack import PLAN_CHANGES, PLAN_NO_CHANGES
from .sync import SyncResult

PRODUCT = "JWall Engineering Sites"
PRODUCT_MARK = "⚡"
MAX_RESOURCE_ROWS = 40
_MIN_BODY_BYTES = 256
_SEP = " &nbsp;·&nbsp; "

_PLAN_LINE = re.compile(r"^Plan: (\d+) to add, (\d+) to change, (\d+) to destroy\.", re.MULTILINE)
_RESOURCE_LINE = re.compile(
    r"^\s*# (?P<address>\S.*?) (?P<verb>will be created|will be updated in-place|"
    r"must be replaced|will be destroyed|will be read during apply)\s*$",
    re.MULTILINE,
)
_VERB_ACTIONS = {
    "will be created": "create",
    "will be updated in-place": "update",
    "must be replaced": "replace",
    "will be destroyed": "destroy",
    "will be read during apply": "read",
}
_ADD_ICON = "➕"  # noqa: RUF001 -- the emoji, not a plus sign
_ACTION_ICONS = {
    "create": _ADD_ICON,
    "update": "🔄",
    "replace": "♻️",
    "destroy": "🗑️",
    "read": "👁️",
}
_ACTION_LABELS = {
    "create": "create",
    "update": "update in-place",
    "replace": "replace (destroy, then create)",
    "destroy": "destroy",
    "read": "read during apply",
}
_DESTRUCTIVE = {"replace", "destroy"}


@dataclass(frozen=True)
class Provenance:
    """Where a comment came from: the tool version, the commit and the run."""

    version: str
    sha: str | None = None
    commit_url: str | None = None
    run_url: str | None = None


@dataclass(frozen=True)
class ResourceChange:
    address: str
    action: str


@dataclass(frozen=True)
class PlanSummary:
    """What `terraform plan -no-color` said, parsed from its text."""

    add: int | None
    change: int | None
    destroy: int | None
    resources: tuple[ResourceChange, ...]
    outputs_changed: bool

    @property
    def destructive(self) -> bool:
        return bool(self.destroy) or any(r.action in _DESTRUCTIVE for r in self.resources)


def plan_result_name(exit_code: int) -> str:
    return {PLAN_NO_CHANGES: "no-changes", PLAN_CHANGES: "changes"}.get(exit_code, "failed")


def parse_plan(text: str) -> PlanSummary:
    """Pull the counts and the per-resource actions out of plan text."""
    counts = _PLAN_LINE.search(text)
    add: int | None = None
    change: int | None = None
    destroy: int | None = None
    if counts:
        add, change, destroy = (int(n) for n in counts.groups())
    elif "No changes." in text:
        add = change = destroy = 0
    resources = tuple(
        ResourceChange(m.group("address"), _VERB_ACTIONS[m.group("verb")])
        for m in _RESOURCE_LINE.finditer(text)
    )
    return PlanSummary(add, change, destroy, resources, "Changes to Outputs:" in text)


def truncate_tail(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the tail of `text` within `max_bytes`; the tail holds the summary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    tail = encoded[-max_bytes:].decode("utf-8", errors="ignore")
    return tail, True


# -- building blocks ----------------------------------------------------------


def _header(kind: str, subject: str, extra: str | None = None) -> list[str]:
    parts = [f"<b>{kind}</b>", f"<code>{subject}</code>"]
    if extra:
        parts.append(extra)
    return [
        f'<h2 align="center">{PRODUCT_MARK} {PRODUCT}</h2>',
        f'<p align="center">{_SEP.join(parts)}</p>',
        "",
    ]


def _alert(kind: str, *lines: str) -> list[str]:
    return [f"> [!{kind}]", *(f"> {line}" for line in lines), ""]


def _footer(provenance: Provenance, note: str | None = None) -> list[str]:
    parts = [f"<b>{PRODUCT}</b> v{provenance.version}"]
    if provenance.sha:
        short = f"<code>{provenance.sha[:7]}</code>"
        if provenance.commit_url:
            parts.append(f'Commit <a href="{provenance.commit_url}">{short}</a>')
        else:
            parts.append(f"Commit <code>{provenance.sha}</code>")
    if provenance.run_url:
        parts.append(f'<a href="{provenance.run_url}">Workflow run</a>')
    if note:
        parts.append(note)
    return ["---", "", f'<p align="center"><sub>{_SEP.join(parts)}</sub></p>']


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _counts_table(summary: PlanSummary) -> list[str]:
    return [
        f"| {_ADD_ICON} Add | 🔄 Change | 🗑️ Destroy |",
        "| :---: | :---: | :---: |",
        f"| **{summary.add}** | **{summary.change}** | **{summary.destroy}** |",
        "",
    ]


def _resource_table(summary: PlanSummary) -> list[str]:
    rows = summary.resources[:MAX_RESOURCE_ROWS]
    lines = ["| | Resource | Action |", "| :---: | --- | --- |"]
    lines += [
        f"| {_ACTION_ICONS[r.action]} | `{_cell(r.address)}` | {_ACTION_LABELS[r.action]} |"
        for r in rows
    ]
    hidden = len(summary.resources) - len(rows)
    if hidden:
        lines.append(f"| … | _and {hidden} more in the full plan output_ | |")
    return [*lines, ""]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _plan_alert(exit_code: int, summary: PlanSummary, subject: str) -> list[str]:
    if exit_code == PLAN_NO_CHANGES:
        return _alert(
            "NOTE",
            f"**No changes.** The infrastructure of `{subject}` already matches this pull "
            "request. Merging will not touch AWS.",
        )
    if exit_code != PLAN_CHANGES:
        return _alert(
            "CAUTION",
            "**Plan failed.** Terraform exited with an error before producing a plan. "
            "Its output is below; the debug log is in the workflow run's artifacts.",
        )
    if summary.add is None:
        what = (
            "This plan only updates Terraform outputs; no resources change."
            if summary.outputs_changed
            else "Review the plan output below before merging."
        )
        return _alert("WARNING", f"**Changes pending.** {what}")
    counts = (
        f"**{summary.add}** to add, **{summary.change}** to change, "
        f"**{summary.destroy}** to destroy"
    )
    if summary.destructive:
        return _alert(
            "CAUTION",
            f"**Destructive changes pending.** Merging this pull request will destroy or "
            f"replace {_plural(summary.destroy or 0, 'resource')} of `{subject}`: {counts}. "
            "Check every 🗑️ and ♻️ row below before approving.",
        )
    return _alert(
        "WARNING",
        f"**Changes pending.** Merging this pull request will change the infrastructure "
        f"of `{subject}`: {counts}.",
    )


# -- comments -----------------------------------------------------------------


def plan_comment(
    exit_code: int,
    plan_text: str,
    max_bytes: int,
    subject: str,
    provenance: Provenance,
) -> str:
    """The sticky plan comment. `max_bytes` bounds the whole comment."""
    summary = parse_plan(plan_text)
    failed = exit_code not in (PLAN_NO_CHANGES, PLAN_CHANGES)
    head = _header("Terraform plan", subject) + _plan_alert(exit_code, summary, subject)
    if exit_code == PLAN_CHANGES and summary.add is not None:
        head += _counts_table(summary)
    if exit_code == PLAN_CHANGES and summary.resources:
        head += _resource_table(summary)
    details = "<details open>" if failed else "<details>"
    title = "Terraform output" if failed else "Full plan output"
    head += [f"{details}<summary>📄 {title}</summary>", ""]
    truncated_note = "_Plan output truncated to its tail; the full plan is in the run's artifacts._"
    tail = ["```", "", "</details>", "", *_footer(provenance, "Updated on every push"), ""]

    fixed = "\n".join([*head, truncated_note, "", "```hcl", *tail]).encode("utf-8")
    budget = max(max_bytes - len(fixed) - 1, _MIN_BODY_BYTES)  # -1: the newline before the body
    body, truncated = truncate_tail(plan_text.rstrip("\n"), budget)
    lines = list(head)
    if truncated:
        lines += [truncated_note, ""]
    lines += ["```hcl", body, *tail]
    return "\n".join(lines)


def preview_comment(url: str, pr: int, subject: str, provenance: Provenance) -> str:
    built = "this pull request"
    if provenance.sha:
        short = f"`{provenance.sha[:7]}`"
        built = f"[{short}]({provenance.commit_url})" if provenance.commit_url else short
    lines = _header("Preview", subject, f"#{pr}")
    lines += _alert(
        "TIP",
        f"**Preview deployed.** The site built from this pull request is live at **{url}**",
    )
    lines += [
        "| | |",
        "| --- | --- |",
        f"| 🌐 Preview | **{url}** |",
        f"| 🔖 Built from | {built} |",
        "| ♻️ Lifecycle | Updated on every push, removed when the pull request closes |",
        "",
    ]
    lines += _footer(provenance)
    return "\n".join(lines) + "\n"


def preview_removed_comment(pr: int, subject: str, provenance: Provenance) -> str:
    lines = _header("Preview", subject, f"#{pr}")
    lines += _alert(
        "NOTE",
        f"**Preview removed.** Pull request #{pr} is closed, so its preview of `{subject}` "
        "has been deleted.",
    )
    lines += _footer(provenance)
    return "\n".join(lines) + "\n"


# -- step summaries -----------------------------------------------------------


def sync_summary(title: str, url: str, result: SyncResult, invalidation_id: str | None) -> str:
    lines = [
        f"## {title}",
        "",
        f"- {url}",
        f"- {result.uploaded} uploaded ({result.bytes_uploaded} bytes), "
        f"{result.unchanged} unchanged, {result.deleted} deleted",
    ]
    if invalidation_id:
        lines.append(f"- CloudFront invalidation `{invalidation_id}` created")
    return "\n".join(lines) + "\n"


def run_summary(title: str, items: dict[str, str]) -> str:
    lines = [f"## {title}", ""]
    lines += [f"- {key}: {value}" for key, value in items.items()]
    return "\n".join(lines) + "\n"
