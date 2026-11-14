from __future__ import annotations

from collections.abc import Iterator

import pytest

from jwall_engineering_sites.checks.framework import isolated_registry


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    """Each test starts with an empty registry and its registrations are discarded."""
    with isolated_registry():
        yield
