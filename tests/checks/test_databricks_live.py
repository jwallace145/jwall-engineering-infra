from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from moto import mock_aws

from jwall_engineering_sites.checks import databricks_live  # noqa: F401  (registers the checks)
from jwall_engineering_sites.checks.framework import (
    CheckContext,
    CheckResult,
    Outcome,
    Severity,
    Stage,
    registered,
)
from jwall_engineering_sites.databricks_names import derived_names
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings
from tests.checks.fakes import (
    FakeAccount,
    FakeDatabricks,
    Overlay,
    OverlaySession,
    client_error,
    metastore,
    service_principal,
    user,
    workspace,
)

REAL_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-aaaaaaaaaaaa"
SECRET = "sentinel-secret-value"
SKIP_HINT = (
    "set databricks.enabled: true in config.yaml after the one-time setup in "
    "docs/databricks.md#one-time-setup"
)


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    """Opt out of the isolation: these tests read the registry the module filled at import."""
    yield


@pytest.fixture
def live(settings: Settings) -> Settings:
    assert settings.databricks is not None
    section = settings.databricks.model_copy(
        update={"enabled": True, "account_id": REAL_ID, "client_id": CLIENT_ID}
    )
    return settings.model_copy(update={"databricks": section})


@pytest.fixture
def aws() -> Iterator[boto3.session.Session]:
    with mock_aws():
        yield boto3.session.Session(region_name="us-east-1")


def _put_secret(session: Any, settings: Settings) -> None:
    assert settings.databricks is not None
    session.client("ssm").put_parameter(
        Name=settings.databricks.secret_parameter, Value=SECRET, Type="SecureString"
    )


def _run(
    check_id: str,
    project: Project,
    settings: Settings,
    session: Any = None,
    databricks: FakeDatabricks | None = None,
) -> CheckResult:
    ctx = CheckContext(
        project=project,
        settings=settings,
        logger=logging.getLogger("t"),
        session=session,
        databricks=databricks,
    )
    (entry,) = [c for c in registered(Stage.LIVE, "databricks") if c.check_id == check_id]
    results = list(entry.fn(ctx))
    assert len(results) == 1
    return results[0]


def _account(**kwargs: Any) -> FakeDatabricks:
    return FakeDatabricks(FakeAccount(**kwargs))


# -- every live check skips until enabled ------------------------------------------


def test_every_live_check_skips_when_disabled(databricks: Project, settings: Settings) -> None:
    assert settings.databricks is not None and not settings.databricks.enabled
    checks = registered(Stage.LIVE, "databricks")
    assert len(checks) == 10
    for entry in checks:
        ctx = CheckContext(databricks.model_copy(), settings, logging.getLogger("t"), None, None)
        (result,) = list(entry.fn(ctx))
        assert result.outcome is Outcome.SKIP, entry.check_id
        assert result.remediation == SKIP_HINT
        assert result.runbook == "docs/databricks.md#one-time-setup"


# -- DBX-LIVE-001 ----------------------------------------------------------------


def test_live001_passes_when_the_secret_decrypts(
    databricks: Project, live: Settings, aws: Any
) -> None:
    _put_secret(aws, live)
    result = _run("DBX-LIVE-001", databricks, live, aws)
    assert result.outcome is Outcome.PASS
    assert SECRET not in repr(result)


def test_live001_fails_when_the_secret_is_missing(
    databricks: Project, live: Settings, aws: Any
) -> None:
    result = _run("DBX-LIVE-001", databricks, live, aws)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None
    assert "databricks.secret_parameter" in result.remediation
    assert result.runbook == "docs/databricks.md#credentials"


# -- DBX-LIVE-002 ----------------------------------------------------------------


def test_live002_passes_when_a_token_is_issued(databricks: Project, live: Settings) -> None:
    assert _run("DBX-LIVE-002", databricks, live, None, _account()).outcome is Outcome.PASS


def test_live002_fails_on_placeholder_ids(databricks: Project, live: Settings) -> None:
    assert live.databricks is not None
    section = live.databricks.model_copy(
        update={"account_id": "00000000-0000-0000-0000-000000000000"}
    )
    s = live.model_copy(update={"databricks": section})
    result = _run("DBX-LIVE-002", databricks, s, None, _account())
    assert result.outcome is Outcome.FAIL
    assert "config.yaml databricks.account_id/client_id" in (result.remediation or "")


def test_live002_fails_when_the_token_is_refused(databricks: Project, live: Settings) -> None:
    api = _account(token_error=RuntimeError(f"invalid_client {SECRET}"))
    result = _run("DBX-LIVE-002", databricks, live, None, api)
    assert result.outcome is Outcome.FAIL
    assert "databricks.client_id" in (result.remediation or "")
    assert SECRET not in repr(result)


# -- DBX-LIVE-003 ----------------------------------------------------------------


def test_live003_passes_for_an_account_admin(databricks: Project, live: Settings) -> None:
    api = _account(service_principals=[service_principal(CLIENT_ID, admin=True)])
    assert _run("DBX-LIVE-003", databricks, live, None, api).outcome is Outcome.PASS


def test_live003_fails_when_not_an_admin(databricks: Project, live: Settings) -> None:
    api = _account(service_principals=[service_principal(CLIENT_ID, admin=False)])
    result = _run("DBX-LIVE-003", databricks, live, None, api)
    assert result.outcome is Outcome.FAIL
    assert "account admin" in (result.remediation or "")
    assert "databricks.client_id" in (result.remediation or "")


# -- DBX-LIVE-004 ----------------------------------------------------------------


def test_live004_passes_when_the_metastore_exists(databricks: Project, live: Settings) -> None:
    assert live.databricks is not None
    api = _account(metastores=[metastore(live.databricks.metastore_name)])
    assert _run("DBX-LIVE-004", databricks, live, None, api).outcome is Outcome.PASS


def test_live004_fails_with_the_deletion_step_for_another_metastore(
    databricks: Project, live: Settings
) -> None:
    api = _account(metastores=[metastore("metastore_aws_us_east_1")])
    result = _run("DBX-LIVE-004", databricks, live, None, api)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None
    assert "metastore_aws_us_east_1" in result.remediation
    assert "delete" in result.remediation.lower()
    assert result.runbook == "docs/databricks.md#metastore"


def test_live004_fails_when_the_foundation_is_not_applied(
    databricks: Project, live: Settings
) -> None:
    result = _run("DBX-LIVE-004", databricks, live, None, _account())
    assert result.outcome is Outcome.FAIL
    assert "apply the foundation first" in (result.remediation or "")


# -- DBX-LIVE-005 ----------------------------------------------------------------


def _members(live: Settings, databricks: Project, *emails: str) -> Settings:
    guard = live.projects["databricks-poc"]
    assert guard.databricks is not None
    new = guard.databricks.model_copy(update={"admins": emails[:1], "users": emails[1:]})
    return live.model_copy(
        update={
            "projects": {
                **live.projects,
                "databricks-poc": guard.model_copy(update={"databricks": new}),
            }
        }
    )


def _project(project: Project, settings: Settings) -> Project:
    return project.model_copy(
        update={"databricks_approved": settings.projects[project.project].databricks}
    )


def test_live005_passes_when_every_member_is_an_account_user(
    databricks: Project, live: Settings
) -> None:
    s = _members(live, databricks, "a@corp.test", "b@corp.test")
    api = _account(users=[user("A@corp.test"), user("b@corp.test")])
    assert _run("DBX-LIVE-005", _project(databricks, s), s, None, api).outcome is Outcome.PASS


def test_live005_names_each_missing_member(databricks: Project, live: Settings) -> None:
    s = _members(live, databricks, "a@corp.test", "b@corp.test", "c@corp.test")
    api = _account(users=[user("a@corp.test")])
    result = _run("DBX-LIVE-005", _project(databricks, s), s, None, api)
    assert result.outcome is Outcome.FAIL
    assert "b@corp.test" in (result.remediation or "")
    assert "c@corp.test" in (result.remediation or "")
    assert "a@corp.test" not in (result.remediation or "")


def test_live005_rejects_placeholder_emails_without_calling_the_api(
    databricks: Project, live: Settings
) -> None:
    result = _run("DBX-LIVE-005", databricks, live, None, None)
    assert result.outcome is Outcome.FAIL
    assert "replace the placeholder email" in (result.remediation or "")
    assert "owner@example.com" in (result.remediation or "")


# -- DBX-LIVE-006 ----------------------------------------------------------------


def test_live006_passes_when_the_buckets_are_free(
    databricks: Project, live: Settings, aws: Any
) -> None:
    assert _run("DBX-LIVE-006", databricks, live, aws).outcome is Outcome.PASS


def test_live006_passes_when_the_buckets_are_ours(
    databricks: Project, live: Settings, aws: Any
) -> None:
    for name in derived_names(databricks).buckets.values():
        aws.client("s3").create_bucket(Bucket=name)
    assert _run("DBX-LIVE-006", databricks, live, aws).outcome is Outcome.PASS


def test_live006_fails_when_someone_else_owns_a_bucket(
    databricks: Project, live: Settings, aws: Any
) -> None:
    taken = derived_names(databricks).catalog_bucket

    def head_bucket(Bucket: str) -> dict[str, Any]:  # noqa: N803
        if Bucket == taken:
            raise client_error("403", "HeadBucket")
        raise client_error("404", "HeadBucket")

    session = OverlaySession(aws, s3=Overlay(aws.client("s3"), head_bucket=head_bucket))
    result = _run("DBX-LIVE-006", databricks, live, session)
    assert result.outcome is Outcome.FAIL
    assert taken in (result.remediation or "")
    assert "project name" in (result.remediation or "")


# -- DBX-LIVE-007 ----------------------------------------------------------------


def test_live007_passes_when_offered_in_both_zones(
    databricks: Project, live: Settings, aws: Any
) -> None:
    result = _run("DBX-LIVE-007", databricks, live, aws)
    assert result.outcome is Outcome.PASS
    assert result.severity is Severity.WARNING


def test_live007_fails_when_a_zone_lacks_a_node_type(
    databricks: Project, live: Settings, aws: Any
) -> None:
    def offerings(**_: Any) -> dict[str, Any]:
        return {
            "InstanceTypeOfferings": [
                {"InstanceType": "m7g.large", "Location": "us-east-1a"},
                {"InstanceType": "m7g.xlarge", "Location": "us-east-1a"},
            ]
        }

    session = OverlaySession(
        aws, ec2=Overlay(aws.client("ec2"), describe_instance_type_offerings=offerings)
    )
    result = _run("DBX-LIVE-007", databricks, live, session)
    assert result.outcome is Outcome.FAIL
    assert "us-east-1b" in (result.remediation or "")
    assert "node_type_id" in (result.remediation or "")


# -- DBX-LIVE-008 ----------------------------------------------------------------


def _quota(value: float) -> Any:
    def get_service_quota(**_: Any) -> dict[str, Any]:
        return {"Quota": {"Value": value}}

    return SimpleNamespace(get_service_quota=get_service_quota)


def test_live008_passes_with_headroom(databricks: Project, live: Settings, aws: Any) -> None:
    # service-quotas is not in moto's supported set here; its client is stubbed.
    session = OverlaySession(aws, **{"service-quotas": _quota(256)})
    result = _run("DBX-LIVE-008", databricks, live, session)
    assert result.outcome is Outcome.PASS
    assert result.severity is Severity.WARNING


def test_live008_fails_without_headroom(databricks: Project, live: Settings, aws: Any) -> None:
    session = OverlaySession(aws, **{"service-quotas": _quota(8)})
    result = _run("DBX-LIVE-008", databricks, live, session)
    assert result.outcome is Outcome.FAIL
    assert "quota" in (result.remediation or "").lower()
    assert "num_workers" in (result.remediation or "")


# -- DBX-LIVE-009 ----------------------------------------------------------------


def _images(*images: dict[str, str]) -> Any:
    def describe_images(**_: Any) -> dict[str, Any]:
        return {"Images": list(images)}

    return describe_images


def test_live009_passes_for_one_arm64_image(databricks: Project, live: Settings, aws: Any) -> None:
    ec2 = Overlay(
        aws.client("ec2"), describe_images=_images({"ImageId": "ami-1", "Architecture": "arm64"})
    )
    assert (
        _run("DBX-LIVE-009", databricks, live, OverlaySession(aws, ec2=ec2)).outcome is Outcome.PASS
    )


def test_live009_fails_when_the_name_resolves_to_nothing(
    databricks: Project, live: Settings, aws: Any
) -> None:
    ec2 = Overlay(aws.client("ec2"), describe_images=_images())
    result = _run("DBX-LIVE-009", databricks, live, OverlaySession(aws, ec2=ec2))
    assert result.outcome is Outcome.FAIL
    assert "databricks.nat_ami" in (result.remediation or "")


def test_live009_fails_for_a_non_arm64_image(databricks: Project, live: Settings, aws: Any) -> None:
    ec2 = Overlay(
        aws.client("ec2"), describe_images=_images({"ImageId": "ami-1", "Architecture": "x86_64"})
    )
    result = _run("DBX-LIVE-009", databricks, live, OverlaySession(aws, ec2=ec2))
    assert result.outcome is Outcome.FAIL
    assert "arm64" in (result.remediation or "")


# -- DBX-LIVE-010 ----------------------------------------------------------------


def _prefixed(live: Settings) -> Settings:
    assert live.databricks is not None
    section = live.databricks.model_copy(update={"deployment_name_prefix": "acme"})
    return live.model_copy(update={"databricks": section})


def test_live010_skips_without_a_prefix(databricks: Project, live: Settings) -> None:
    result = _run("DBX-LIVE-010", databricks, live, None, _account())
    assert result.outcome is Outcome.SKIP


def test_live010_passes_when_the_name_is_free(databricks: Project, live: Settings) -> None:
    result = _run("DBX-LIVE-010", databricks, _prefixed(live), None, _account())
    assert result.outcome is Outcome.PASS


def test_live010_skips_when_our_workspace_exists(databricks: Project, live: Settings) -> None:
    api = _account(workspaces=[workspace("databricks-poc-prod", "acme-databricks-poc")])
    result = _run("DBX-LIVE-010", databricks, _prefixed(live), None, api)
    assert result.outcome is Outcome.SKIP


def test_live010_fails_when_the_name_is_taken(databricks: Project, live: Settings) -> None:
    api = _account(workspaces=[workspace("someone-else", "acme-databricks-poc")])
    result = _run("DBX-LIVE-010", databricks, _prefixed(live), None, api)
    assert result.outcome is Outcome.FAIL
    assert "deployment_name" in (result.remediation or "")
