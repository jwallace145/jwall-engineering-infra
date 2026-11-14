from __future__ import annotations

from collections.abc import Iterator

import pytest

from jwall_engineering_sites.checks import load_builtin_checks
from jwall_engineering_sites.checks.framework import Stage, registered
from jwall_engineering_sites.diagnose import RUNBOOK_ANCHORS


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    """Opt out of the isolation: this asserts what the real registry holds."""
    yield


def test_loader_registers_the_databricks_checks() -> None:
    load_builtin_checks()
    config = [c.check_id for c in registered(Stage.CONFIG, "databricks")]
    live = [c.check_id for c in registered(Stage.LIVE, "databricks")]
    assert config == [f"DBX-CFG-00{i}" for i in range(1, 6)]
    assert live == [f"DBX-LIVE-{i:03d}" for i in range(1, 11)]
    between = [c.check_id for c in registered(Stage.BETWEEN, "databricks")]
    after = [c.check_id for c in registered(Stage.AFTER, "databricks")]
    assert between == [f"DBX-BTW-00{i}" for i in range(1, 5)]
    assert after == [f"DBX-AFT-00{i}" for i in range(1, 5)]
    assert registered(Stage.LIVE, "site") == []


def test_every_check_runbook_is_a_known_anchor() -> None:
    load_builtin_checks()
    for stage in (Stage.CONFIG, Stage.LIVE, Stage.BETWEEN, Stage.AFTER):
        for entry in registered(stage, "databricks"):
            assert entry.runbook in RUNBOOK_ANCHORS, entry.check_id
