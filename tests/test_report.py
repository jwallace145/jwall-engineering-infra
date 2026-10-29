from __future__ import annotations

from jwall_engineering_sites.report import (
    MAX_RESOURCE_ROWS,
    PRODUCT,
    Provenance,
    parse_plan,
    plan_comment,
    plan_result_name,
    preview_comment,
    preview_removed_comment,
    sync_summary,
    truncate_tail,
)
from jwall_engineering_sites.stack import PLAN_CHANGES, PLAN_ERROR, PLAN_NO_CHANGES
from jwall_engineering_sites.sync import SyncResult

PROVENANCE = Provenance(
    version="0.9.1",
    sha="193fcc3deadbeef0000000000000000000000000",
    commit_url="https://github.com/o/r/commit/193fcc3deadbeef0000000000000000000000000",
    run_url="https://github.com/o/r/actions/runs/1",
)

PLAN_WITH_CHANGES = """\
Terraform used the selected providers to generate the following execution
plan. Resource actions are indicated with the following symbols:
  + create
  ~ update in-place
-/+ destroy and then create replacement

Terraform will perform the following actions:

  # module.site.aws_s3_bucket.this will be created
  + resource "aws_s3_bucket" "this" {
      + bucket = "jwall-engineering-site"
    }

  # module.dns[0].aws_route53_record.txt["_github-pages-challenge"] will be updated in-place
  ~ resource "aws_route53_record" "txt" {
      ~ records = [ "old" ] -> [ "new" ]
    }

  # module.cdn.aws_cloudfront_function.rewrite must be replaced
-/+ resource "aws_cloudfront_function" "rewrite" {
    }

  # data.aws_caller_identity.current will be read during apply
 <= data "aws_caller_identity" "current" {
    }

Plan: 2 to add, 1 to change, 1 to destroy.
"""

PLAN_ADD_ONLY = """\
Terraform will perform the following actions:

  # module.site.aws_s3_bucket.this will be created
  + resource "aws_s3_bucket" "this" {
    }

Plan: 1 to add, 0 to change, 0 to destroy.
"""

PLAN_WITH_DESTROY = """\
Terraform will perform the following actions:

  # module.site.aws_s3_bucket.legacy will be destroyed
  - resource "aws_s3_bucket" "legacy" {
    }

Plan: 0 to add, 0 to change, 1 to destroy.
"""

PLAN_NO_CHANGES_TEXT = """\
module.site.aws_s3_bucket.this: Refreshing state... [id=jwall-engineering-site]

No changes. Your infrastructure matches the configuration.
"""

PLAN_OUTPUTS_ONLY = """\
Changes to Outputs:
  + bucket = "jwall-engineering-site"

You can apply this plan to save these new output values to the Terraform
state, without changing any real infrastructure.
"""


def test_plan_result_names() -> None:
    assert plan_result_name(PLAN_NO_CHANGES) == "no-changes"
    assert plan_result_name(PLAN_CHANGES) == "changes"
    assert plan_result_name(PLAN_ERROR) == "failed"


# -- parse_plan ---------------------------------------------------------------


def test_parse_plan_counts_and_resources() -> None:
    summary = parse_plan(PLAN_WITH_CHANGES)
    assert (summary.add, summary.change, summary.destroy) == (2, 1, 1)
    assert [(r.address, r.action) for r in summary.resources] == [
        ("module.site.aws_s3_bucket.this", "create"),
        ('module.dns[0].aws_route53_record.txt["_github-pages-challenge"]', "update"),
        ("module.cdn.aws_cloudfront_function.rewrite", "replace"),
        ("data.aws_caller_identity.current", "read"),
    ]
    assert summary.outputs_changed is False


def test_parse_plan_destroy() -> None:
    summary = parse_plan(PLAN_WITH_DESTROY)
    assert (summary.add, summary.change, summary.destroy) == (0, 0, 1)
    assert [r.action for r in summary.resources] == ["destroy"]


def test_parse_plan_no_changes() -> None:
    summary = parse_plan(PLAN_NO_CHANGES_TEXT)
    assert (summary.add, summary.change, summary.destroy) == (0, 0, 0)
    assert summary.resources == ()


def test_parse_plan_outputs_only() -> None:
    summary = parse_plan(PLAN_OUTPUTS_ONLY)
    assert (summary.add, summary.change, summary.destroy) == (None, None, None)
    assert summary.outputs_changed is True


def test_parse_plan_garbage() -> None:
    summary = parse_plan("Error: something broke\n")
    assert (summary.add, summary.change, summary.destroy) == (None, None, None)
    assert summary.resources == ()


# -- plan_comment -------------------------------------------------------------


def test_plan_comment_is_branded() -> None:
    comment = plan_comment(PLAN_NO_CHANGES, PLAN_NO_CHANGES_TEXT, 60000, "a.example", PROVENANCE)
    assert PRODUCT in comment
    assert "Terraform plan" in comment
    assert "a.example" in comment


def test_plan_comment_no_changes_is_a_note() -> None:
    comment = plan_comment(PLAN_NO_CHANGES, PLAN_NO_CHANGES_TEXT, 60000, "a.example", PROVENANCE)
    assert "> [!NOTE]" in comment
    assert "No changes" in comment
    assert "[!WARNING]" not in comment and "[!CAUTION]" not in comment
    assert "<details><summary>" in comment  # collapsed
    assert "| Resource |" not in comment


def test_plan_comment_add_only_is_a_warning() -> None:
    comment = plan_comment(PLAN_CHANGES, PLAN_ADD_ONLY, 60000, "a.example", PROVENANCE)
    assert "> [!WARNING]" in comment
    assert "Changes pending" in comment
    assert "[!CAUTION]" not in comment
    assert "| **1** | **0** | **0** |" in comment


def test_plan_comment_changes_has_counts_and_resources() -> None:
    comment = plan_comment(PLAN_CHANGES, PLAN_WITH_CHANGES, 60000, "a.example", PROVENANCE)
    assert "changes pending" in comment.lower()
    assert "| **2** | **1** | **1** |" in comment
    assert "`module.site.aws_s3_bucket.this`" in comment
    assert '`module.dns[0].aws_route53_record.txt["_github-pages-challenge"]`' in comment
    assert "create" in comment and "update in-place" in comment and "replace" in comment
    assert "`data.aws_caller_identity.current`" in comment
    assert "```hcl\n" in comment and "Plan: 2 to add, 1 to change, 1 to destroy." in comment


def test_plan_comment_destroy_is_a_caution() -> None:
    comment = plan_comment(PLAN_CHANGES, PLAN_WITH_DESTROY, 60000, "a.example", PROVENANCE)
    assert "> [!CAUTION]" in comment
    assert "destroy" in comment.lower()
    assert "[!WARNING]" not in comment


def test_plan_comment_replace_counts_as_destructive() -> None:
    comment = plan_comment(PLAN_CHANGES, PLAN_WITH_CHANGES, 60000, "a.example", PROVENANCE)
    assert "> [!CAUTION]" in comment


def test_plan_comment_outputs_only() -> None:
    comment = plan_comment(PLAN_CHANGES, PLAN_OUTPUTS_ONLY, 60000, "a.example", PROVENANCE)
    assert "> [!WARNING]" in comment
    assert "outputs" in comment.lower()
    assert "| **" not in comment  # no counts table without a Plan: line


def test_plan_comment_failed_is_a_caution_with_open_details() -> None:
    comment = plan_comment(PLAN_ERROR, "Error: boom\n", 60000, "a.example", PROVENANCE)
    assert "> [!CAUTION]" in comment
    assert "Plan failed" in comment
    assert "<details open>" in comment
    assert "Error: boom" in comment


def test_plan_comment_footer_has_provenance() -> None:
    comment = plan_comment(PLAN_NO_CHANGES, PLAN_NO_CHANGES_TEXT, 60000, "a.example", PROVENANCE)
    assert f"<b>{PRODUCT}</b> v0.9.1" in comment
    assert (
        '<a href="https://github.com/o/r/commit/193fcc3deadbeef0000000000000000000000000">'
        "<code>193fcc3</code></a>"
    ) in comment
    assert '<a href="https://github.com/o/r/actions/runs/1">Workflow run</a>' in comment


def test_plan_comment_footer_without_github_context() -> None:
    comment = plan_comment(
        PLAN_NO_CHANGES, PLAN_NO_CHANGES_TEXT, 60000, "a.example", Provenance(version="1.0")
    )
    assert "v1.0" in comment
    assert "Commit" not in comment
    assert "Workflow run" not in comment


def test_plan_comment_short_sha_without_commit_url_is_plain() -> None:
    comment = plan_comment(
        PLAN_NO_CHANGES,
        PLAN_NO_CHANGES_TEXT,
        60000,
        "a.example",
        Provenance(version="1.0", sha="193fcc3deadbeef0000000000000000000000000"),
    )
    assert "<code>193fcc3deadbeef0000000000000000000000000</code>" in comment
    assert "<a href" not in comment.split("<sub>")[1]


def test_plan_comment_fits_the_byte_budget() -> None:
    text = "head\n" + "x" * 5000 + "\nPlan: 2 to add, 0 to change, 0 to destroy."
    comment = plan_comment(PLAN_CHANGES, text, 2500, "a.example", PROVENANCE)
    assert len(comment.encode("utf-8")) <= 2500
    assert "truncated" in comment
    assert "Plan: 2 to add" in comment
    assert "head" not in comment


def test_plan_comment_caps_the_resource_table() -> None:
    lines = [f"  # aws_thing.r{i} will be created" for i in range(MAX_RESOURCE_ROWS + 5)]
    text = (
        "\n".join(lines) + f"\n\nPlan: {MAX_RESOURCE_ROWS + 5} to add, 0 to change, 0 to destroy.\n"
    )
    comment = plan_comment(PLAN_CHANGES, text, 60000, "a.example", PROVENANCE)
    assert f"`aws_thing.r{MAX_RESOURCE_ROWS - 1}`" in comment
    assert f"`aws_thing.r{MAX_RESOURCE_ROWS}`" not in comment
    assert "5 more" in comment


def test_truncate_tail_keeps_small_text() -> None:
    assert truncate_tail("abc", 10) == ("abc", False)


# -- preview comments ---------------------------------------------------------


def test_preview_comment() -> None:
    comment = preview_comment("https://pr-3.preview.a.example/", 3, "a.example", PROVENANCE)
    assert PRODUCT in comment
    assert "Preview" in comment and "#3" in comment
    assert "> [!TIP]" in comment
    assert "**https://pr-3.preview.a.example/**" in comment
    assert "[`193fcc3`](https://github.com/o/r/commit/" in comment
    assert "removed when the pull request closes" in comment
    assert f"{PRODUCT}</b> v0.9.1" in comment


def test_preview_removed_comment() -> None:
    comment = preview_removed_comment(3, "a.example", PROVENANCE)
    assert PRODUCT in comment
    assert "> [!NOTE]" in comment
    assert "removed" in comment.lower()
    assert "#3" in comment
    assert f"{PRODUCT}</b> v0.9.1" in comment


def test_sync_summary() -> None:
    summary = sync_summary("Published prod", "https://x/", SyncResult(2, 3, 1, 99), "I1")
    assert "## Published prod" in summary
    assert "2 uploaded (99 bytes), 3 unchanged, 1 deleted" in summary
    assert "invalidation `I1`" in summary


# -- bot stacks are applied by hand, not by merging ---------------------------------


def test_bot_plan_comment_names_the_manual_apply_when_destructive() -> None:
    comment = plan_comment(
        PLAN_CHANGES, PLAN_WITH_DESTROY, 60000, "rusty-chess", PROVENANCE, applied_by="Bot apply"
    )
    assert "Merging this pull request will" not in comment
    assert "The next **Bot apply** would destroy or replace" in comment
    assert "Merging this pull request alone changes nothing" in comment


def test_bot_plan_comment_names_the_manual_apply_for_changes() -> None:
    comment = plan_comment(
        PLAN_CHANGES, PLAN_ADD_ONLY, 60000, "rusty-chess", PROVENANCE, applied_by="Bot apply"
    )
    assert "Merging this pull request will" not in comment
    assert "The next **Bot apply** would change" in comment


def test_bot_plan_comment_no_changes() -> None:
    comment = plan_comment(
        PLAN_NO_CHANGES,
        PLAN_NO_CHANGES_TEXT,
        60000,
        "rusty-chess",
        PROVENANCE,
        applied_by="Bot apply",
    )
    assert "Merging will not touch AWS" not in comment
    assert "A **Bot apply** would not touch AWS" in comment


def test_site_plan_comment_wording_is_unchanged() -> None:
    comment = plan_comment(PLAN_CHANGES, PLAN_WITH_DESTROY, 60000, "a.example", PROVENANCE)
    assert "Merging this pull request will destroy or replace" in comment
