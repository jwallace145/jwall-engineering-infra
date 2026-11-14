from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from databricks.sdk.errors import (
    BadRequest,
    InternalError,
    NotFound,
    PermissionDenied,
    TemporarilyUnavailable,
    Unauthenticated,
)
from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.service.catalog import (
    CatalogInfo,
    CatalogIsolationMode,
    MetastoreAssignment,
    ValidateStorageCredentialResponse,
    ValidationResult,
    ValidationResultOperation,
    ValidationResultResult,
    WorkspaceBinding,
)
from databricks.sdk.service.compute import Policy
from databricks.sdk.service.iam import WorkspacePermission
from databricks.sdk.service.provisioning import WorkspaceStatus
from moto import mock_aws

from jwall_engineering_sites.checks import databricks_after, databricks_between  # noqa: F401
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
    FakeWorkspace,
    client_error,
    group_assignment,
    workspace_in,
)

REAL_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-aaaaaaaaaaaa"
URL = "https://dbc-example.cloud.databricks.com"
OUTPUTS = {
    "workspace_id": 1234,
    "workspace_url": URL,
    "admin_group_name": "databricks-poc-workspace-admins",
    "users_group_name": "databricks-poc-workspace-users",
    "root_bucket_name": "jwall-engineering-databricks-poc-databricks-root",
    "metastore_id": "ms-1",
}
SKIP_HINT = (
    "set databricks.enabled: true in config.yaml after the one-time setup in "
    "docs/databricks.md#one-time-setup"
)


@pytest.fixture(autouse=True)
def empty_registry() -> Iterator[None]:
    """Opt out of the isolation: these tests read the registry the modules filled at import."""
    yield


@pytest.fixture
def live(settings: Settings) -> Settings:
    assert settings.databricks is not None
    section = settings.databricks.model_copy(
        update={"enabled": True, "account_id": REAL_ID, "client_id": CLIENT_ID}
    )
    return settings.model_copy(update={"databricks": section})


class Clock:
    """A fake clock: `sleep` advances it, so polling runs instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Autouse: a polling check never sleeps for real here."""
    fake = Clock()
    monkeypatch.setattr(databricks_between, "_sleep", fake.sleep)
    monkeypatch.setattr(databricks_between, "_clock", fake)
    return fake


def _run(
    check_id: str,
    stage: Stage,
    project: Project,
    settings: Settings,
    dbx: FakeDatabricks | None,
    session: Any = None,
) -> CheckResult:
    if dbx is not None:
        dbx.account_outputs = OUTPUTS
    ctx = CheckContext(project, settings, logging.getLogger("t"), session, dbx)
    (entry,) = [c for c in registered(stage, "databricks") if c.check_id == check_id]
    results = list(entry.fn(ctx))
    assert len(results) == 1
    return results[0]


def _btw(check_id: str, project: Project, settings: Settings, **kwargs: Any) -> CheckResult:
    account = FakeAccount(
        **{k: v for k, v in kwargs.items() if k in FakeAccount.__dataclass_fields__}
    )
    workspace = FakeWorkspace(
        **{k: v for k, v in kwargs.items() if k in FakeWorkspace.__dataclass_fields__}
    )
    return _run(check_id, Stage.BETWEEN, project, settings, FakeDatabricks(account, workspace))


def _aft(
    check_id: str, project: Project, settings: Settings, workspace: FakeWorkspace | None = None
) -> CheckResult:
    return _run(check_id, Stage.AFTER, project, settings, FakeDatabricks(FakeAccount(), workspace))


# -- registration and gating ---------------------------------------------------------


def test_every_between_and_after_check_skips_when_disabled(
    databricks: Project, settings: Settings
) -> None:
    assert settings.databricks is not None and not settings.databricks.enabled
    for stage, count in ((Stage.BETWEEN, 4), (Stage.AFTER, 4)):
        checks = registered(stage, "databricks")
        assert len(checks) == count
        for entry in checks:
            ctx = CheckContext(databricks, settings, logging.getLogger("t"), None, None)
            (result,) = list(entry.fn(ctx))
            assert result.outcome is Outcome.SKIP, entry.check_id
            assert result.remediation == SKIP_HINT


def test_only_the_egress_check_is_a_warning() -> None:
    severities = {
        c.check_id: c.severity
        for stage in (Stage.BETWEEN, Stage.AFTER)
        for c in registered(stage, "databricks")
    }
    assert severities.pop("DBX-AFT-004") is Severity.WARNING
    assert set(severities.values()) == {Severity.ERROR}


def test_an_api_failure_is_a_failure_without_the_message(
    databricks: Project, live: Settings
) -> None:
    workspace = FakeWorkspace(me_error=DatabricksError("token sentinel-secret-value rejected"))
    result = _run(
        "DBX-BTW-002", Stage.BETWEEN, databricks, live, FakeDatabricks(workspace=workspace)
    )
    assert result.outcome is Outcome.FAIL
    assert "sentinel-secret-value" not in repr(result)
    assert result.facts["error"] == "DatabricksError"


# -- DBX-BTW-001 ---------------------------------------------------------------------


def test_btw001_passes_when_running(databricks: Project, live: Settings, clock: Clock) -> None:
    result = _btw("DBX-BTW-001", databricks, live)
    assert result.outcome is Outcome.PASS
    assert clock.sleeps == []


def test_btw001_fails_immediately_when_failed(
    databricks: Project, live: Settings, clock: Clock
) -> None:
    result = _btw(
        "DBX-BTW-001",
        databricks,
        live,
        workspace_states=[workspace_in(WorkspaceStatus.FAILED, "invalid credentials")],
    )
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None and "invalid credentials" in result.remediation
    assert clock.sleeps == []


def test_readiness_succeeds_after_provisioning(
    databricks: Project, live: Settings, clock: Clock
) -> None:
    states = [
        workspace_in(WorkspaceStatus.PROVISIONING, "creating network"),
        workspace_in(WorkspaceStatus.PROVISIONING, "creating network"),
        workspace_in(WorkspaceStatus.RUNNING),
    ]
    result = _btw("DBX-BTW-001", databricks, live, workspace_states=states)
    assert result.outcome is Outcome.PASS
    assert clock.sleeps == [15, 15]


def test_readiness_times_out_with_last_status(
    databricks: Project, live: Settings, clock: Clock
) -> None:
    states = [
        workspace_in(WorkspaceStatus.PROVISIONING, "step 1"),
        workspace_in(WorkspaceStatus.PROVISIONING, "waiting for the network"),
    ]
    result = _btw("DBX-BTW-001", databricks, live, workspace_states=states)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None
    assert "PROVISIONING" in result.remediation
    assert "waiting for the network" in result.remediation
    assert live.databricks is not None
    assert set(clock.sleeps) == {15}
    assert clock.now <= live.databricks.readiness_timeout_seconds


# -- DBX-BTW-002 / 003 -----------------------------------------------------------------


def test_btw002_passes_when_the_workspace_answers(databricks: Project, live: Settings) -> None:
    dbx = FakeDatabricks()
    result = _run("DBX-BTW-002", Stage.BETWEEN, databricks, live, dbx)
    assert result.outcome is Outcome.PASS
    assert dbx.hosts == [URL]


def test_btw002_fails_when_the_workspace_does_not_answer(
    databricks: Project, live: Settings, clock: Clock
) -> None:
    result = _btw("DBX-BTW-002", databricks, live, me_error=ConnectionError("down"))
    assert result.outcome is Outcome.FAIL
    assert result.facts["error"] == "ConnectionError"
    assert result.remediation is not None and "ConnectionError" in result.remediation
    assert live.databricks is not None
    assert set(clock.sleeps) == {15}
    assert clock.now <= live.databricks.readiness_timeout_seconds


def _scripted(*replies: Any) -> Any:
    """A callable that raises or returns each reply in turn; the last one repeats."""
    queue = list(replies)

    def call(*_: Any, **__: Any) -> Any:
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    return call


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("dns not yet"),
        TemporarilyUnavailable("503"),
        InternalError("500"),
        DatabricksError("502 from the edge"),
        Unauthenticated("401"),
        PermissionDenied("403"),
    ],
    ids=lambda e: type(e).__name__,
)
def test_btw002_polls_through_propagation(
    databricks: Project, live: Settings, clock: Clock, error: Exception
) -> None:
    workspace = FakeWorkspace()
    me = workspace.me
    workspace.current_user.me = _scripted(error, error, me)  # type: ignore[method-assign]
    result = _run(
        "DBX-BTW-002", Stage.BETWEEN, databricks, live, FakeDatabricks(workspace=workspace)
    )
    assert result.outcome is Outcome.PASS
    assert clock.sleeps == [15, 15]


def test_btw002_fails_at_once_on_a_non_transient_error(
    databricks: Project, live: Settings, clock: Clock
) -> None:
    result = _btw("DBX-BTW-002", databricks, live, me_error=BadRequest("bad"))
    assert result.outcome is Outcome.FAIL
    assert result.facts["error"] == "BadRequest"
    assert clock.sleeps == []


ADMIN_GROUP = str(OUTPUTS["admin_group_name"])


def test_btw003_passes_when_the_admin_group_holds_admin(
    databricks: Project, live: Settings
) -> None:
    # Me() lists no `admins` group (nested membership): the account assignment decides.
    assignments = [group_assignment(ADMIN_GROUP, WorkspacePermission.ADMIN)]
    assert _btw("DBX-BTW-003", databricks, live, assignments=assignments).outcome is Outcome.PASS


def test_btw003_fails_when_the_assignment_is_missing(databricks: Project, live: Settings) -> None:
    assignments = [group_assignment("other-group", WorkspacePermission.ADMIN)]
    result = _btw("DBX-BTW-003", databricks, live, assignments=assignments)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None and ADMIN_GROUP in result.remediation


def test_btw003_fails_when_the_group_is_only_a_user(databricks: Project, live: Settings) -> None:
    assignments = [group_assignment(ADMIN_GROUP, WorkspacePermission.USER)]
    assert _btw("DBX-BTW-003", databricks, live, assignments=assignments).outcome is Outcome.FAIL


def test_btw003_polls_until_the_assignment_is_listed(
    databricks: Project, live: Settings, clock: Clock
) -> None:
    dbx = FakeDatabricks()
    held = [group_assignment(ADMIN_GROUP, WorkspacePermission.ADMIN)]
    dbx.account.workspace_assignment.list = _scripted(iter([]), iter(held))
    assert _run("DBX-BTW-003", Stage.BETWEEN, databricks, live, dbx).outcome is Outcome.PASS
    assert clock.sleeps == [15]


def test_btw003_polls_until_the_admin_read_is_allowed(
    databricks: Project, live: Settings, clock: Clock
) -> None:
    workspace = FakeWorkspace()
    workspace.workspace_conf.get_status = _scripted(  # type: ignore[method-assign]
        PermissionDenied("not yet"), PermissionDenied("not yet"), {"enableTokensConfig": "true"}
    )
    dbx = FakeDatabricks(
        FakeAccount(assignments=[group_assignment(ADMIN_GROUP, WorkspacePermission.ADMIN)]),
        workspace,
    )
    assert _run("DBX-BTW-003", Stage.BETWEEN, databricks, live, dbx).outcome is Outcome.PASS
    assert clock.sleeps == [15, 15]


def test_btw003_fails_with_propagation_message_when_the_read_is_denied(
    databricks: Project, live: Settings
) -> None:
    assignments = [group_assignment(ADMIN_GROUP, WorkspacePermission.ADMIN)]
    result = _btw(
        "DBX-BTW-003",
        databricks,
        live,
        assignments=assignments,
        conf_error=PermissionDenied("nope"),
    )
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None and "not yet effective" in result.remediation


def test_btw001_keeps_polling_when_the_workspace_is_not_listed_yet(
    databricks: Project, live: Settings, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    dbx = FakeDatabricks()

    def gone(workspace_id: int) -> Any:
        raise NotFound("no such workspace")

    monkeypatch.setattr(dbx.account.workspaces, "get", gone)
    result = _run("DBX-BTW-001", Stage.BETWEEN, databricks, live, dbx)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None and "still did not exist" in result.remediation
    assert live.databricks is not None
    assert len(clock.sleeps) > 1 and set(clock.sleeps) == {15}
    assert clock.now <= live.databricks.readiness_timeout_seconds


def test_btw001_passes_once_the_workspace_appears(
    databricks: Project, live: Settings, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    dbx = FakeDatabricks()
    replies: list[Any] = [NotFound("not yet"), workspace_in(WorkspaceStatus.RUNNING)]

    def get(workspace_id: int) -> Any:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(dbx.account.workspaces, "get", get)
    assert _run("DBX-BTW-001", Stage.BETWEEN, databricks, live, dbx).outcome is Outcome.PASS
    assert clock.sleeps == [15]


def test_workspace_level_api_failure_names_workspace_permissions(
    databricks: Project, live: Settings
) -> None:
    assignments = [group_assignment(ADMIN_GROUP, WorkspacePermission.ADMIN)]
    result = _btw(
        "DBX-BTW-003",
        databricks,
        live,
        assignments=assignments,
        conf_error=DatabricksError("boom"),
    )
    assert result.remediation is not None
    assert "workspace permissions" in result.remediation
    assert "account_id" not in result.remediation


# -- DBX-BTW-004 -----------------------------------------------------------------------


def test_btw004_passes_when_assigned(databricks: Project, live: Settings) -> None:
    assignment = MetastoreAssignment(workspace_id=1234, metastore_id="ms-1")
    result = _btw("DBX-BTW-004", databricks, live, metastore_assignment=assignment)
    assert result.outcome is Outcome.PASS


def test_btw004_fails_when_unassigned(databricks: Project, live: Settings) -> None:
    result = _btw("DBX-BTW-004", databricks, live)
    assert result.outcome is Outcome.FAIL
    assert result.runbook == "docs/databricks.md#metastore-assignment"


def test_btw004_fails_for_another_metastore(databricks: Project, live: Settings) -> None:
    assignment = MetastoreAssignment(workspace_id=1234, metastore_id="ms-other")
    result = _btw("DBX-BTW-004", databricks, live, metastore_assignment=assignment)
    assert result.outcome is Outcome.FAIL
    assert result.facts == {"expected": "ms-1", "actual": "ms-other"}


# -- DBX-AFT-001 -----------------------------------------------------------------------


def _validation(*outcomes: ValidationResultResult) -> ValidateStorageCredentialResponse:
    ops = list(ValidationResultOperation)
    return ValidateStorageCredentialResponse(
        results=[
            ValidationResult(
                operation=ops[i],
                result=r,
                message=None if r is ValidationResultResult.PASS else "denied",
            )
            for i, r in enumerate(outcomes)
        ]
    )


def test_aft001_passes_and_validates_the_external_location_url(
    databricks: Project, live: Settings
) -> None:
    names = derived_names(databricks)
    workspace = FakeWorkspace(validation=_validation(ValidationResultResult.PASS))
    dbx = FakeDatabricks(workspace=workspace)
    result = _run("DBX-AFT-001", Stage.AFTER, databricks, live, dbx)
    assert result.outcome is Outcome.PASS
    assert workspace.storage_credentials.validated == [
        {
            "storage_credential_name": names.uc_role,
            "external_location_name": names.catalog_bucket,
            "url": f"s3://{names.catalog_bucket}/managed/",
        }
    ]


def test_aft001_fails_when_an_operation_is_denied(databricks: Project, live: Settings) -> None:
    workspace = FakeWorkspace(
        validation=_validation(ValidationResultResult.PASS, ValidationResultResult.FAIL)
    )
    result = _aft("DBX-AFT-001", databricks, live, workspace)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None and "denied" in result.remediation


def test_aft001_ignores_path_exists_and_records_it(databricks: Project, live: Settings) -> None:
    validation = ValidateStorageCredentialResponse(
        results=[
            ValidationResult(
                operation=ValidationResultOperation.READ, result=ValidationResultResult.PASS
            ),
            ValidationResult(
                operation=ValidationResultOperation.PATH_EXISTS,
                result=ValidationResultResult.FAIL,
                message="no such path",
            ),
        ]
    )
    result = _aft("DBX-AFT-001", databricks, live, FakeWorkspace(validation=validation))
    assert result.outcome is Outcome.PASS
    assert result.facts["op_path_exists"] == "FAIL"


def test_aft001_fails_when_only_informational_results_return(
    databricks: Project, live: Settings
) -> None:
    validation = ValidateStorageCredentialResponse(
        results=[
            ValidationResult(
                operation=ValidationResultOperation.PATH_EXISTS, result=ValidationResultResult.PASS
            )
        ]
    )
    result = _aft("DBX-AFT-001", databricks, live, FakeWorkspace(validation=validation))
    assert result.outcome is Outcome.FAIL


def test_aft001_fails_when_validation_returns_nothing(databricks: Project, live: Settings) -> None:
    assert _aft("DBX-AFT-001", databricks, live).outcome is Outcome.FAIL


# -- DBX-AFT-002 -----------------------------------------------------------------------


def _catalog(**kwargs: Any) -> CatalogInfo:
    bucket = "jwall-engineering-databricks-poc-databricks-catalog"
    fields: dict[str, Any] = {
        "name": "databricks_poc",
        "storage_root": f"s3://{bucket}/managed/",
        "isolation_mode": CatalogIsolationMode.ISOLATED,
    }
    return CatalogInfo(**{**fields, **kwargs})


def test_aft002_passes_for_an_isolated_bound_catalog(databricks: Project, live: Settings) -> None:
    workspace = FakeWorkspace(catalog=_catalog(), bindings=[WorkspaceBinding(workspace_id=1234)])
    assert _aft("DBX-AFT-002", databricks, live, workspace).outcome is Outcome.PASS


def test_aft002_fails_when_the_catalog_is_missing(databricks: Project, live: Settings) -> None:
    result = _aft("DBX-AFT-002", databricks, live)
    assert result.outcome is Outcome.FAIL
    assert result.facts == {"catalog": "databricks_poc"}


@pytest.mark.parametrize(
    "catalog,bindings,needle",
    [
        (_catalog(isolation_mode=CatalogIsolationMode.OPEN), [1234], "ISOLATED"),
        (_catalog(storage_root="s3://elsewhere/managed/"), [1234], "storage_root"),
        (_catalog(), [], "bound to workspaces none"),
        (_catalog(), [1234, 99], "expected only 1234"),
    ],
)
def test_aft002_fails_when_the_design_is_not_met(
    databricks: Project,
    live: Settings,
    catalog: CatalogInfo,
    bindings: list[int],
    needle: str,
) -> None:
    workspace = FakeWorkspace(
        catalog=catalog, bindings=[WorkspaceBinding(workspace_id=b) for b in bindings]
    )
    result = _aft("DBX-AFT-002", databricks, live, workspace)
    assert result.outcome is Outcome.FAIL
    assert result.remediation is not None and needle in result.remediation


# -- DBX-AFT-003 -----------------------------------------------------------------------


def _policy(name: str, tags: dict[str, str]) -> Policy:
    definition: dict[str, Any] = {"autotermination_minutes": {"type": "fixed", "value": 30}}
    definition.update({f"custom_tags.{k}": {"type": "fixed", "value": v} for k, v in tags.items()})
    return Policy(policy_id=f"id-{name}", name=name, definition=json.dumps(definition))


def test_aft003_passes_when_policies_carry_the_tags(databricks: Project, live: Settings) -> None:
    tags = databricks.databricks_tags
    workspace = FakeWorkspace(policies=[_policy("small-interactive", tags), _policy("jobs", tags)])
    assert _aft("DBX-AFT-003", databricks, live, workspace).outcome is Outcome.PASS


def test_aft003_fails_for_a_missing_policy(databricks: Project, live: Settings) -> None:
    workspace = FakeWorkspace(policies=[_policy("jobs", databricks.databricks_tags)])
    result = _aft("DBX-AFT-003", databricks, live, workspace)
    assert result.outcome is Outcome.FAIL
    assert "small-interactive is missing" in result.facts["problems"]


def test_aft003_fails_for_a_malformed_tag_entry(databricks: Project, live: Settings) -> None:
    tags = databricks.databricks_tags
    bad = Policy(
        policy_id="id-jobs",
        name="jobs",
        definition=json.dumps({f"custom_tags.{k}": "fixed" for k in tags}),
    )
    workspace = FakeWorkspace(policies=[_policy("small-interactive", tags), bad])
    result = _aft("DBX-AFT-003", databricks, live, workspace)
    assert result.outcome is Outcome.FAIL
    assert "jobs has the wrong fixed tag(s)" in result.facts["problems"]
    assert result.remediation is not None
    assert "Organization" in result.remediation and "Repository" in result.remediation


def test_aft003_fails_for_a_wrong_or_missing_tag(databricks: Project, live: Settings) -> None:
    tags = databricks.databricks_tags
    wrong = {**tags, "CostCenter": "cc-9999"}
    short = {k: v for k, v in tags.items() if k != "Workspace"}
    workspace = FakeWorkspace(
        policies=[_policy("small-interactive", wrong), _policy("jobs", short)]
    )
    result = _aft("DBX-AFT-003", databricks, live, workspace)
    assert result.outcome is Outcome.FAIL
    assert "small-interactive has the wrong fixed tag(s) CostCenter" in result.facts["problems"]
    assert "jobs has the wrong fixed tag(s) Workspace" in result.facts["problems"]


# -- DBX-AFT-004 (moto) ----------------------------------------------------------------


@pytest.fixture
def aws() -> Iterator[boto3.session.Session]:
    with mock_aws():
        yield boto3.session.Session(region_name="us-east-1")


def _network(
    session: Any, name: str, *, nat: bool = True, endpoint: bool = True, igw: bool = False
) -> None:
    ec2 = session.client("ec2")
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.0.0/24")["Subnet"]["SubnetId"]
    table = ec2.create_route_table(
        VpcId=vpc,
        TagSpecifications=[
            {"ResourceType": "route-table", "Tags": [{"Key": "Name", "Value": f"{name}-private"}]}
        ],
    )["RouteTable"]["RouteTableId"]
    if endpoint:
        ec2.create_vpc_endpoint(
            VpcId=vpc,
            ServiceName="com.amazonaws.us-east-1.s3",
            VpcEndpointType="Gateway",
            RouteTableIds=[table],
        )
    if igw:
        gateway = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=gateway, VpcId=vpc)
        ec2.create_route(RouteTableId=table, DestinationCidrBlock="0.0.0.0/0", GatewayId=gateway)
    if nat:
        instance = ec2.run_instances(
            ImageId="ami-12345678",
            MinCount=1,
            MaxCount=1,
            SubnetId=subnet,
            TagSpecifications=[
                {"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": f"{name}-nat"}]}
            ],
        )["Instances"][0]
        if not igw:
            eni = instance["NetworkInterfaces"][0]["NetworkInterfaceId"]
            ec2.create_route(
                RouteTableId=table, DestinationCidrBlock="0.0.0.0/0", NetworkInterfaceId=eni
            )


NAME = "jwall-engineering-databricks-poc"


def test_aft004_passes_for_endpoint_and_nat_route(
    databricks: Project, live: Settings, aws: Any
) -> None:
    _network(aws, NAME)
    result = _run("DBX-AFT-004", Stage.AFTER, databricks, live, FakeDatabricks(), aws)
    assert result.outcome is Outcome.PASS


def test_aft004_fails_without_the_endpoint(databricks: Project, live: Settings, aws: Any) -> None:
    _network(aws, NAME, endpoint=False)
    result = _run("DBX-AFT-004", Stage.AFTER, databricks, live, FakeDatabricks(), aws)
    assert result.outcome is Outcome.FAIL
    assert "S3 gateway endpoint" in result.facts["problems"]


def test_aft004_fails_when_the_default_route_is_an_igw(
    databricks: Project, live: Settings, aws: Any
) -> None:
    _network(aws, NAME, igw=True)
    result = _run("DBX-AFT-004", Stage.AFTER, databricks, live, FakeDatabricks(), aws)
    assert result.outcome is Outcome.FAIL
    assert "internet gateway" in result.facts["problems"]


def test_aft004_fails_without_a_default_route(
    databricks: Project, live: Settings, aws: Any
) -> None:
    _network(aws, NAME, nat=False)
    result = _run("DBX-AFT-004", Stage.AFTER, databricks, live, FakeDatabricks(), aws)
    assert result.outcome is Outcome.FAIL
    assert "no default route" in result.facts["problems"]


def test_aft004_fails_when_the_route_table_is_missing(
    databricks: Project, live: Settings, aws: Any
) -> None:
    result = _run("DBX-AFT-004", Stage.AFTER, databricks, live, FakeDatabricks(), aws)
    assert result.outcome is Outcome.FAIL
    assert result.facts == {"found": "0"}


def test_aft004_maps_an_aws_refusal_to_a_failure(databricks: Project, live: Settings) -> None:
    class Refusing:
        def client(self, service: str, **_: Any) -> Any:
            class Ec2:
                def describe_route_tables(self, **_: Any) -> Any:
                    raise client_error("UnauthorizedOperation", "DescribeRouteTables")

            return Ec2()

    result = _run("DBX-AFT-004", Stage.AFTER, databricks, live, FakeDatabricks(), Refusing())
    assert result.outcome is Outcome.FAIL
    assert result.facts == {"error": "ClientError"}
