from __future__ import annotations

from collections.abc import Iterator

import pytest

from jwall_engineering_sites.checks import load_builtin_checks
from jwall_engineering_sites.checks.framework import Stage, registered


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
    assert registered(Stage.LIVE, "site") == []
