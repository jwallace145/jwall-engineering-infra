from __future__ import annotations

import re
from pathlib import Path

import pytest

from jwall_engineering_sites.diagnose import (
    CATALOGUE,
    RUNBOOK_ANCHORS,
    diagnose,
    render_failure,
)

ERRORS = Path(__file__).parent / "fixtures" / "errors"
DOCS = Path(__file__).parent.parent / "docs" / "databricks.md"
LOG = Path("/tmp/terraform-debug.log")
SAMPLES = sorted(ERRORS.glob("*.txt"))


def sample(path: Path) -> str:
    return "".join(ln for ln in path.read_text().splitlines(keepends=True) if ln[0] != "#")


def test_every_entry_has_a_sample() -> None:
    assert {d.error_class for d in CATALOGUE} == {p.stem for p in SAMPLES}


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_matches_its_entry(path: Path) -> None:
    failure = diagnose("databricks-account", sample(path), LOG)
    assert failure.diagnosis is not None
    assert failure.diagnosis.error_class == path.stem


def test_entries_do_not_overlap() -> None:
    for path in SAMPLES:
        text = sample(path)
        matching = [d.error_class for d in CATALOGUE if d.pattern.search(text)]
        assert matching == [path.stem]


def test_only_iam_propagation_is_retry_safe_by_default() -> None:
    assert {d.error_class for d in CATALOGUE if d.retry_safe} == {"AWS-IAM-PROPAGATION"}


def test_resource_address_parsed() -> None:
    failure = diagnose("databricks-account", sample(ERRORS / "DBX-CRED-VALIDATION.txt"), LOG)
    assert failure.resource == "databricks_mws_credentials.this"
    assert failure.stack == "databricks-account"
    assert failure.debug_log == LOG


def test_raw_is_only_the_error_block_and_truncated() -> None:
    noise = "aws_s3_bucket.x: Creating...\n" * 5000
    out = noise + sample(ERRORS / "DBX-AUTH.txt")
    failure = diagnose("databricks-workspace", out, LOG)
    assert "Creating..." not in failure.raw
    assert "invalid_client" in failure.raw
    big = "Error: boom\n\n  with a.b,\n" + "x" * 50_000
    assert len(diagnose("site", big, LOG).raw.encode()) <= 8_000


def test_unmatched_error_still_points_somewhere() -> None:
    out = (
        "Error: something nobody has seen before\n\n"
        '  with databricks_thing.odd,\n  on main.tf line 3, in resource "databricks_thing" "odd":\n'
    )
    failure = diagnose("databricks-workspace", out, LOG)
    assert failure.diagnosis is None
    text = render_failure(failure)
    assert "databricks-workspace" in text
    assert "databricks_thing.odd" in text
    assert str(LOG) in text
    assert "something nobody has seen before" in text


def test_output_without_an_error_block_keeps_the_tail() -> None:
    failure = diagnose("site", "line\n" * 3 + "terraform exploded\n", LOG)
    assert failure.resource is None
    assert "terraform exploded" in failure.raw


def test_render_known_failure_has_all_parts() -> None:
    failure = diagnose("databricks-account", sample(ERRORS / "DBX-CRED-VALIDATION.txt"), LOG)
    text = render_failure(failure)
    d = failure.diagnosis
    assert d is not None
    for part in (d.error_class, d.title, d.likely_cause, d.runbook, *d.next_steps):
        assert part in text
    assert "re-running apply is safe" not in text


def test_retry_safe_entry_says_so() -> None:
    failure = diagnose("databricks-account", sample(ERRORS / "AWS-IAM-PROPAGATION.txt"), LOG)
    assert "re-running apply is safe" in render_failure(failure).lower()


def test_partial_run_message() -> None:
    failure = diagnose("databricks-workspace", sample(ERRORS / "DBX-WORKSPACE-FAILED.txt"), LOG)
    text = render_failure(failure, partial="account")
    assert "account stack applied; workspace stack failed; re-running apply is safe" in text


def test_client_secret_never_stored_or_rendered() -> None:
    sentinel = "SENTINEL-s3cr3t-value"
    out = (
        "Error: cannot configure default credentials\n"
        f"  DATABRICKS_CLIENT_SECRET={sentinel}\n"
        f'  client_secret = "{sentinel}"\n'
        "  invalid_client: Client authentication failed\n\n"
        "  with databricks_mws_credentials.this,\n"
    )
    failure = diagnose("databricks-account", out, LOG)
    assert sentinel not in failure.raw
    assert sentinel not in render_failure(failure, partial="account")
    assert failure.diagnosis is not None
    assert failure.diagnosis.error_class == "DBX-AUTH"


def test_metastore_entries_name_the_import_command() -> None:
    entry = next(d for d in CATALOGUE if d.error_class == "DBX-METASTORE-ALREADY-ASSIGNED")
    steps = " ".join(entry.next_steps)
    assert "terraform import databricks_metastore_assignment.this" in steps


def test_runbook_anchors_are_one_list_and_cover_the_catalogue() -> None:
    assert {d.runbook for d in CATALOGUE} <= set(RUNBOOK_ANCHORS)
    assert all(a.startswith("docs/databricks.md#") for a in RUNBOOK_ANCHORS)


@pytest.mark.xfail(strict=True, reason="docs/databricks.md is created in Task 12")
def test_every_runbook_anchor_exists() -> None:
    headings = {
        re.sub(r"[^a-z0-9 -]", "", m.group(1).lower()).strip().replace(" ", "-")
        for m in re.finditer(r"^#+ (.+)$", DOCS.read_text(), re.MULTILINE)
    }
    for anchor in RUNBOOK_ANCHORS:
        assert anchor.split("#", 1)[1] in headings
