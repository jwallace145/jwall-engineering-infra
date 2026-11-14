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


def entry_of(path: Path) -> str:
    """`CLASS.txt` is the entry's sample; `CLASS--variant.txt` another wording or a cross-cut."""
    return path.stem.split("--")[0]


def sample(path: Path) -> str:
    return "".join(ln for ln in path.read_text().splitlines(keepends=True) if ln[0] != "#")


def test_every_entry_has_a_sample() -> None:
    assert {d.error_class for d in CATALOGUE} == {entry_of(p) for p in SAMPLES}


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_matches_its_entry(path: Path) -> None:
    failure = diagnose("databricks-account", sample(path), LOG)
    assert failure.diagnosis is not None
    assert failure.diagnosis.error_class == entry_of(path)


def test_entries_do_not_overlap() -> None:
    """Under the real first-match order each sample gets its own entry.

    Plain samples match exactly one entry; a `--` sample may also match a later, broader
    entry, but never an earlier one.
    """
    names = [d.error_class for d in CATALOGUE]
    for path in SAMPLES:
        matching = [d.error_class for d in CATALOGUE if d.pattern.search(sample(path))]
        assert matching, path.stem
        assert matching[0] == entry_of(path), (path.stem, matching)
        if "--" not in path.stem:
            assert matching == [entry_of(path)], (path.stem, matching)
        assert names.index(matching[0]) == min(names.index(m) for m in matching)


def test_auth_error_on_a_metastore_resource_is_auth() -> None:
    failure = diagnose(
        "databricks-workspace", sample(ERRORS / "DBX-AUTH--metastore-assignment.txt"), LOG
    )
    assert failure.diagnosis is not None
    assert failure.diagnosis.error_class == "DBX-AUTH"


def test_address_line_alone_is_not_a_metastore_assignment_error() -> None:
    out = "Error: cannot read something else\n\n  with databricks_metastore_assignment.this,\n"
    assert diagnose("databricks-workspace", out, LOG).diagnosis is None


def test_eventual_consistency_read_is_left_unmatched() -> None:
    """Not 'make the SP a metastore admin': the read races the assignment, so no entry claims it."""
    out = (
        "Error: cannot read metastore assignment: "
        "No metastore assigned for the current workspace.\n\n"
        "  with databricks_metastore_assignment.this,\n"
    )
    failure = diagnose("databricks-workspace", out, LOG)
    assert failure.diagnosis is None
    assert "metastore admin" not in render_failure(failure)


def test_only_iam_propagation_is_retry_safe_by_default() -> None:
    assert {d.error_class for d in CATALOGUE if d.retry_safe} == {
        "AWS-IAM-PROPAGATION",
        "AWS-ASSUME-ROLE-PROPAGATION",
    }


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
    assert "client_id=" in failure.raw
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


def test_secret_values_are_masked_but_the_error_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = "SENTINEL-s3cr3t-value"
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", sentinel)
    out = (
        "Error: cannot create mws credentials: default auth: cannot configure default credentials. "
        f"Config: host=https://accounts.example.com, client_id=abc, client_secret={sentinel}. "
        "Env: DATABRICKS_HOST, DATABRICKS_CLIENT_SECRET\n\n"
        f"  with databricks_mws_credentials.this,\n  detail echoed {sentinel} and "
        f'client_secret = "{sentinel}"\n'
    )
    failure = diagnose("databricks-account", out, LOG)
    assert sentinel not in failure.raw
    assert sentinel not in render_failure(failure, partial="account")
    assert "cannot create mws credentials" in failure.raw
    assert failure.diagnosis is not None
    assert failure.diagnosis.error_class == "DBX-AUTH"


def test_secret_masked_without_the_environment_variable() -> None:
    out = "Error: boom. Config: client_secret=abc123def, host=h\n"
    failure = diagnose("site", out, LOG)
    assert "abc123def" not in failure.raw
    assert "host=h" in failure.raw


def test_plain_block_ends_at_the_next_warning_or_end_of_output() -> None:
    out = (
        "Error: first problem\n\n  with a.b,\n\n"
        "Warning: a deprecation\n\n  with c.d,\n\n"
        "Error: second problem\n\n  with e.f,\n"
    )
    failure = diagnose("site", out, LOG)
    assert "first problem" in failure.raw
    assert "second problem" in failure.raw
    assert "deprecation" not in failure.raw


def test_boxed_layout_still_parses() -> None:
    path = ERRORS / "DBX-STORAGE-VALIDATION.txt"
    assert "│" in path.read_text()
    failure = diagnose("databricks-account", sample(path), LOG)
    assert failure.resource == "databricks_mws_storage_configurations.this"
    assert "│" not in failure.raw


def test_partial_only_accepts_account() -> None:
    failure = diagnose("site", "Error: x\n", LOG)
    with pytest.raises(ValueError, match="partial"):
        render_failure(failure, partial="workspace")


def test_metastore_entries_name_the_import_command() -> None:
    entry = next(d for d in CATALOGUE if d.error_class == "DBX-METASTORE-ALREADY-ASSIGNED")
    steps = " ".join(entry.next_steps)
    assert (
        "terraform import 'module.workspace.databricks_metastore_assignment.this' "
        "'<workspace_id>|<metastore_id>'" in steps
    )
    assert "account stack" in steps and "owner" in steps.lower()


def test_auth_entry_does_not_claim_any_oauth_m2m_mention() -> None:
    """A metastore or workspace error that merely names the auth type is not an auth error."""
    out = (
        "Error: cannot create workspace: auth type oauth-m2m: workspace quota exceeded\n\n"
        "  with module.workspace.databricks_mws_workspaces.this,\n"
    )
    diagnosis = diagnose("databricks-account", out, LOG).diagnosis
    assert diagnosis is None or diagnosis.error_class != "DBX-AUTH"


def test_already_exists_on_another_metastore_resource_is_not_already_assigned() -> None:
    out = (
        "Error: cannot create metastore data access: metastore data access already exists\n\n"
        "  with databricks_metastore_data_access.this,\n"
    )
    diagnosis = diagnose("foundation", out, LOG).diagnosis
    assert diagnosis is not None and diagnosis.error_class == "TF-ALREADY-EXISTS"


def test_assume_role_propagation_is_retry_safe_and_names_the_resource() -> None:
    path = ERRORS / "AWS-ASSUME-ROLE-PROPAGATION--sts-assume-role.txt"
    failure = diagnose("databricks-workspace", sample(path), LOG)
    assert failure.diagnosis is not None
    assert failure.diagnosis.error_class == "AWS-ASSUME-ROLE-PROPAGATION"
    assert failure.resource == "module.catalog.databricks_storage_credential.this"
    assert "re-running apply is safe" in render_failure(failure).lower()


def test_runbook_anchors_are_one_list_and_cover_the_catalogue() -> None:
    assert {d.runbook for d in CATALOGUE} <= set(RUNBOOK_ANCHORS)
    assert all(a.startswith("docs/databricks.md#") for a in RUNBOOK_ANCHORS)


def test_every_runbook_anchor_exists() -> None:
    headings = {
        re.sub(r"[^a-z0-9 -]", "", m.group(1).lower()).strip().replace(" ", "-")
        for m in re.finditer(r"^#+ (.+)$", DOCS.read_text(), re.MULTILINE)
    }
    for anchor in RUNBOOK_ANCHORS:
        assert anchor.split("#", 1)[1] in headings
