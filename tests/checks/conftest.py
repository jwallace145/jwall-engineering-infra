from __future__ import annotations

from collections.abc import Iterator

import pytest

from jwall_engineering_sites.checks.framework import isolated_registry
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings
from jwall_engineering_sites.site import load_site_config


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    """Each test starts with an empty registry and its registrations are discarded."""
    with isolated_registry():
        yield


@pytest.fixture
def databricks(settings: Settings) -> Project:
    from tests.conftest import FIXTURES

    return Project.from_config(
        load_site_config(FIXTURES / "bots" / "databricks-poc.yaml"), settings
    )
