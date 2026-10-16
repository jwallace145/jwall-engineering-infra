"""The Markdown the tool writes: sticky PR comments and step summaries."""

from __future__ import annotations

from .stack import PLAN_CHANGES, PLAN_NO_CHANGES
from .sync import SyncResult

PLAN_COMMENT_HEADINGS = {
    PLAN_NO_CHANGES: "### Terraform plan: no changes",
    PLAN_CHANGES: "### Terraform plan: changes pending",
}
PLAN_FAILED_HEADING = "### Terraform plan: failed"


def plan_result_name(exit_code: int) -> str:
    return {PLAN_NO_CHANGES: "no-changes", PLAN_CHANGES: "changes"}.get(exit_code, "failed")


def truncate_tail(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the tail of `text` within `max_bytes`; the tail holds the summary."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    tail = encoded[-max_bytes:].decode("utf-8", errors="ignore")
    return tail, True


def plan_comment(
    exit_code: int,
    plan_text: str,
    max_bytes: int,
    sha: str | None,
    run_url: str | None,
    version: str,
) -> str:
    heading = PLAN_COMMENT_HEADINGS.get(exit_code, PLAN_FAILED_HEADING)
    body, truncated = truncate_tail(plan_text, max_bytes)
    provenance = []
    if sha:
        provenance.append(f"Commit {sha}")
    if run_url:
        provenance.append(f"[workflow run]({run_url})")
    provenance.append(f"jwall-engineering-sites {version}")
    lines = [heading, "", " · ".join(provenance), ""]
    if truncated:
        lines += [
            "_Plan output truncated to its tail; the full plan is in the run's artifacts._",
            "",
        ]
    lines += [
        "<details><summary>Show plan</summary>",
        "",
        "```hcl",
        body.rstrip("\n"),
        "```",
        "",
        "</details>",
    ]
    return "\n".join(lines) + "\n"


def preview_comment(url: str, sha: str | None) -> str:
    built = f"Built from {sha}. " if sha else ""
    return f"**{url}**\n\n{built}Updated on every push; removed when the pull request closes.\n"


def preview_removed_comment() -> str:
    return "Removed: this pull request is closed.\n"


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
