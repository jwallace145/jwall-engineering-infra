from __future__ import annotations

from jwall_engineering_sites.report import (
    plan_comment,
    plan_result_name,
    preview_comment,
    sync_summary,
    truncate_tail,
)
from jwall_engineering_sites.stack import PLAN_CHANGES, PLAN_ERROR, PLAN_NO_CHANGES
from jwall_engineering_sites.sync import SyncResult


def test_plan_result_names() -> None:
    assert plan_result_name(PLAN_NO_CHANGES) == "no-changes"
    assert plan_result_name(PLAN_CHANGES) == "changes"
    assert plan_result_name(PLAN_ERROR) == "failed"


def test_plan_comment_headings() -> None:
    assert plan_comment(PLAN_NO_CHANGES, "x", 1000, "abc", None, "1.0").startswith(
        "### Terraform plan: no changes"
    )
    assert plan_comment(PLAN_CHANGES, "x", 1000, None, None, "1.0").startswith(
        "### Terraform plan: changes pending"
    )
    assert plan_comment(PLAN_ERROR, "x", 1000, None, None, "1.0").startswith(
        "### Terraform plan: failed"
    )


def test_plan_comment_has_provenance_and_details() -> None:
    comment = plan_comment(PLAN_CHANGES, "Plan: 1 to add", 1000, "deadbeef", "https://run", "0.5.0")
    assert "Commit deadbeef" in comment
    assert "[workflow run](https://run)" in comment
    assert "jwall-engineering-sites 0.5.0" in comment
    assert "<details><summary>Show plan</summary>" in comment
    assert "```hcl\nPlan: 1 to add\n```" in comment


def test_plan_comment_truncates_to_the_tail() -> None:
    text = "head\n" + "x" * 5000 + "\nPlan: 2 to add"
    comment = plan_comment(PLAN_CHANGES, text, 100, None, None, "1.0")
    assert "truncated" in comment
    assert "Plan: 2 to add" in comment
    assert "head" not in comment


def test_truncate_tail_keeps_small_text() -> None:
    assert truncate_tail("abc", 10) == ("abc", False)


def test_preview_comment() -> None:
    assert preview_comment("https://pr-3.preview.x/", "abc") == (
        "**https://pr-3.preview.x/**\n\n"
        "Built from abc. Updated on every push; removed when the pull request closes.\n"
    )


def test_sync_summary() -> None:
    summary = sync_summary("Published prod", "https://x/", SyncResult(2, 3, 1, 99), "I1")
    assert "## Published prod" in summary
    assert "2 uploaded (99 bytes), 3 unchanged, 1 deleted" in summary
    assert "invalidation `I1`" in summary
