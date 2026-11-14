from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from jwall_engineering_sites.checks import databricks_config as cfg
from jwall_engineering_sites.checks.framework import (
    CheckContext,
    CheckResult,
    Outcome,
    Severity,
    Stage,
    registered,
)
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import (
    DatabricksPolicy,
    DatabricksProject,
    ProjectGuardrails,
    Settings,
)


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    """Opt out of the isolation: these tests read the registry the module filled at import."""
    yield


def _run(check_id: str, project: Project, settings: Settings) -> CheckResult:
    ctx = CheckContext(
        project=project,
        settings=settings,
        logger=logging.getLogger("t"),
        session=None,
        databricks=None,
    )
    (entry,) = [c for c in registered(Stage.CONFIG, "databricks") if c.check_id == check_id]
    results = list(entry.fn(ctx))
    assert len(results) == 1
    return results[0]


def _approved(settings: Settings, **changes: Any) -> Settings:
    guard = settings.projects["databricks-poc"]
    assert guard.databricks is not None
    new = guard.databricks.model_copy(update=changes)
    return settings.model_copy(
        update={
            "projects": {
                **settings.projects,
                "databricks-poc": guard.model_copy(update={"databricks": new}),
            }
        }
    )


def _with_project(settings: Settings, project: Project) -> Project:
    guard = settings.projects["databricks-poc"]
    assert isinstance(guard, ProjectGuardrails)
    return project.model_copy(update={"databricks_approved": guard.databricks})


def _policies(settings: Settings, **policies: DatabricksPolicy) -> Settings:
    assert settings.databricks is not None
    section = settings.databricks.model_copy(
        update={"compute_policies": {**settings.databricks.compute_policies, **policies}}
    )
    return settings.model_copy(update={"databricks": section})


def _policy(definition: dict[str, Any]) -> DatabricksPolicy:
    return DatabricksPolicy(description="d", definition=definition)


GOOD = {
    "autotermination_minutes": {"type": "fixed", "value": 30},
    "node_type_id": {"type": "allowlist", "values": ["m7g.large"], "defaultValue": "m7g.large"},
}


# -- DBX-CFG-001 -----------------------------------------------------------------


def test_cfg001_passes_for_the_fixture(databricks: Project, settings: Settings) -> None:
    assert _run("DBX-CFG-001", databricks, settings).outcome is Outcome.PASS


def test_cfg001_fails_when_private_subnets_overlap_public(
    databricks: Project, settings: Settings
) -> None:
    # A /23 gives /27 private subnets inside the first two /24s the vpc module uses as public.
    s = _approved(settings, vpc_cidr="10.40.0.0/23")
    result = _run("DBX-CFG-001", _with_project(s, databricks), s)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None
    assert "projects.databricks-poc.databricks.vpc_cidr" in result.remediation
    assert result.runbook == "docs/databricks.md#network-cidrs"


def test_cfg001_fails_when_private_subnets_are_too_small(
    databricks: Project, settings: Settings
) -> None:
    s = _approved(settings, vpc_cidr="10.40.0.0/25")
    result = _run("DBX-CFG-001", _with_project(s, databricks), s)
    assert result.outcome is Outcome.FAIL
    assert "/17" in result.facts["problem"]


# -- DBX-CFG-002 -----------------------------------------------------------------


def test_cfg002_passes(databricks: Project, settings: Settings) -> None:
    assert _run("DBX-CFG-002", databricks, settings).outcome is Outcome.PASS


def test_cfg002_fails_on_the_bot_vpc(databricks: Project, settings: Settings) -> None:
    s = _approved(settings, vpc_cidr="10.0.0.0/20")
    result = _run("DBX-CFG-002", _with_project(s, databricks), s)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None
    assert "vpc_cidr" in result.remediation
    assert "10.0.0.0/16" in result.facts["overlaps"]


def test_cfg002_fails_on_another_project(databricks: Project, settings: Settings) -> None:
    other = DatabricksProject(
        compute_policies=(),
        cost_center="cc-1",
        vpc_cidr="10.40.16.0/20",
        admins=("a@example.com",),
    )
    s = settings.model_copy(
        update={
            "projects": {**settings.projects, "team-a": ProjectGuardrails(databricks=other)},
        }
    )
    result = _run("DBX-CFG-002", databricks, s)
    assert result.outcome is Outcome.FAIL
    assert "team-a" in result.facts["overlaps"]


# -- DBX-CFG-003 -----------------------------------------------------------------


def test_cfg003_passes_for_the_fixture(databricks: Project, settings: Settings) -> None:
    assert _run("DBX-CFG-003", databricks, settings).outcome is Outcome.PASS


def test_cfg003_fails_without_autotermination(databricks: Project, settings: Settings) -> None:
    s = _policies(settings, jobs=_policy({"node_type_id": GOOD["node_type_id"]}))
    s = _policies(s, **{"small-interactive": _policy({"node_type_id": GOOD["node_type_id"]})})
    result = _run("DBX-CFG-003", databricks, s)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None
    assert "databricks.compute_policies.small-interactive" in result.remediation
    assert "autotermination_minutes" in result.remediation


def test_cfg003_fails_on_custom_tags(databricks: Project, settings: Settings) -> None:
    bad = {**GOOD, "custom_tags.Team": {"type": "fixed", "value": "x"}}
    s = _policies(settings, **{"small-interactive": _policy(bad)})
    result = _run("DBX-CFG-003", databricks, s)
    assert result.outcome is Outcome.FAIL
    assert "custom_tags" in (result.remediation or "")


def test_cfg003_fails_when_default_node_type_is_off_the_allowlist(
    databricks: Project, settings: Settings
) -> None:
    bad = {
        **GOOD,
        "node_type_id": {"type": "allowlist", "values": ["m7g.large"], "defaultValue": "m5.large"},
    }
    s = _policies(settings, **{"small-interactive": _policy(bad)})
    result = _run("DBX-CFG-003", databricks, s)
    assert result.outcome is Outcome.FAIL
    assert "m5.large" in (result.remediation or "")


def test_cfg003_fails_on_a_missing_central_policy(databricks: Project, settings: Settings) -> None:
    assert settings.databricks is not None
    section = settings.databricks.model_copy(update={"compute_policies": {}})
    s = settings.model_copy(update={"databricks": section})
    result = _run("DBX-CFG-003", databricks, s)
    assert result.outcome is Outcome.FAIL
    assert "databricks.compute_policies" in (result.remediation or "")


# -- DBX-CFG-004 -----------------------------------------------------------------


def test_cfg004_passes(databricks: Project, settings: Settings) -> None:
    assert _run("DBX-CFG-004", databricks, settings).outcome is Outcome.PASS


def test_cfg004_fails_on_a_long_project_name(databricks: Project, settings: Settings) -> None:
    long = databricks.model_copy(
        update={"config": databricks.config.model_copy(update={"project": "p" * 30})}
    )
    result = _run("DBX-CFG-004", long, settings)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None
    assert "project name" in result.remediation
    assert "cross-account role" in result.facts["too_long"]


def test_cfg004_fails_on_an_invalid_deployment_name(
    databricks: Project, settings: Settings
) -> None:
    assert settings.databricks is not None
    section = settings.databricks.model_copy(update={"deployment_name_prefix": "p" * 60})
    s = settings.model_copy(update={"databricks": section})
    result = _run("DBX-CFG-004", databricks, s)
    assert result.outcome is Outcome.FAIL
    assert "deployment_name" in (result.remediation or "")


# -- DBX-CFG-005 -----------------------------------------------------------------


def test_cfg005_passes(databricks: Project, settings: Settings) -> None:
    assert _run("DBX-CFG-005", databricks, settings).outcome is Outcome.PASS


def test_cfg005_fails_on_a_reserved_key(databricks: Project, settings: Settings) -> None:
    config = databricks.config.model_copy(
        update={"tags": {**databricks.config.tags, "Vendor": "x"}}
    )
    result = _run("DBX-CFG-005", databricks.model_copy(update={"config": config}), settings)
    assert result.outcome is Outcome.FAIL
    assert "Vendor" in (result.remediation or "")
    assert "tags" in (result.remediation or "")


def test_cfg005_fails_on_a_missing_required_tag(databricks: Project, settings: Settings) -> None:
    tags = {k: v for k, v in databricks.config.tags.items() if k != "Owner"}
    config = databricks.config.model_copy(update={"tags": tags})
    result = _run("DBX-CFG-005", databricks.model_copy(update={"config": config}), settings)
    assert result.outcome is Outcome.FAIL
    assert "Owner" in (result.remediation or "")


def test_every_config_check_has_a_runbook(databricks: Project, settings: Settings) -> None:
    ids = {c.check_id for c in registered(Stage.CONFIG, "databricks")}
    assert ids == {f"DBX-CFG-00{i}" for i in range(1, 6)}
    assert all(
        c.runbook and c.severity is Severity.ERROR for c in registered(Stage.CONFIG, "databricks")
    )
    assert cfg  # module imported => registered
