"""In-memory stand-ins for the Databricks account client and boto3 overlays.

Objects are the SDK's own dataclasses; the fake `list()` methods take only the
keyword arguments the real ones do. Nothing here touches the network.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Any

import boto3
from botocore.exceptions import ClientError
from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import (
    AccountsMetastoreAssignment,
    CatalogInfo,
    MetastoreAssignment,
    MetastoreInfo,
    ValidateStorageCredentialResponse,
    WorkspaceBinding,
)
from databricks.sdk.service.compute import Policy
from databricks.sdk.service.iam import ComplexValue, ServicePrincipal, User
from databricks.sdk.service.provisioning import Workspace, WorkspaceStatus


class _Metastores:
    def __init__(self, items: list[MetastoreInfo]) -> None:
        self._items = items

    def list(self) -> Iterator[MetastoreInfo]:
        return iter(self._items)


class _Workspaces:
    def __init__(self, items: list[Workspace], states: list[Workspace]) -> None:
        self._items = items
        self._states = list(states)
        self.gets = 0

    def list(self) -> Iterator[Workspace]:
        return iter(self._items)

    def get(self, workspace_id: int) -> Workspace:
        """Each call returns the next scripted state; the last one repeats."""
        self.gets += 1
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]


class _MetastoreAssignments:
    def __init__(self, assignment: MetastoreAssignment | None) -> None:
        self._assignment = assignment

    def get(self, workspace_id: int) -> AccountsMetastoreAssignment:
        if self._assignment is None:
            raise NotFound("no metastore assignment")
        return AccountsMetastoreAssignment(metastore_assignment=self._assignment)


class _Users:
    def __init__(self, items: list[User]) -> None:
        self._items = items

    def list(self, *, filter: str | None = None) -> Iterator[User]:
        return iter(self._items)


class _ServicePrincipals:
    """`list` omits roles (as SCIM may); only `get` returns them."""

    def __init__(self, items: list[ServicePrincipal]) -> None:
        self._items = items

    def list(self, *, filter: str | None = None) -> Iterator[ServicePrincipal]:
        return iter(replace(sp, roles=None) for sp in self._items)

    def get(self, id: str) -> ServicePrincipal:
        return next(sp for sp in self._items if sp.id == id)


@dataclass
class FakeAccount:
    metastores: list[MetastoreInfo] = field(default_factory=list)
    users: list[User] = field(default_factory=list)
    service_principals: list[ServicePrincipal] = field(default_factory=list)
    workspaces: list[Workspace] = field(default_factory=list)
    #: What `workspaces.get` returns call by call (the last repeats).
    workspace_states: list[Workspace] = field(
        default_factory=lambda: [workspace_in(WorkspaceStatus.RUNNING)]
    )
    metastore_assignment: MetastoreAssignment | None = None
    token_error: Exception | None = None
    list_error: Exception | None = None


class _Account:
    def __init__(self, fake: FakeAccount) -> None:
        self.metastores = _Metastores(fake.metastores)
        self.users = _Users(fake.users)
        self.service_principals = _ServicePrincipals(fake.service_principals)
        self.workspaces = _Workspaces(fake.workspaces, fake.workspace_states)
        self.metastore_assignments = _MetastoreAssignments(fake.metastore_assignment)

        def authenticate() -> dict[str, str]:
            if fake.token_error is not None:
                raise fake.token_error
            return {"Authorization": "Bearer fake-token"}

        self.config = SimpleNamespace(authenticate=authenticate)
        if fake.list_error is not None:
            error = fake.list_error

            def boom(**_: Any) -> Iterator[Any]:
                raise error

            self.metastores.list = boom  # type: ignore[method-assign]


class _CurrentUser:
    def __init__(self, fake: FakeWorkspace) -> None:
        self._fake = fake

    def me(self, *, attributes: str | None = None, excluded_attributes: str | None = None) -> User:
        if self._fake.me_error is not None:
            raise self._fake.me_error
        return self._fake.me


class _StorageCredentials:
    def __init__(self, fake: FakeWorkspace) -> None:
        self._fake = fake
        self.validated: list[dict[str, Any]] = []

    def validate(
        self,
        *,
        external_location_name: str | None = None,
        read_only: bool | None = None,
        storage_credential_name: str | None = None,
        url: str | None = None,
    ) -> ValidateStorageCredentialResponse:
        self.validated.append({"storage_credential_name": storage_credential_name, "url": url})
        return self._fake.validation


class _Catalogs:
    def __init__(self, fake: FakeWorkspace) -> None:
        self._fake = fake

    def get(self, name: str, *, include_browse: bool | None = None) -> CatalogInfo:
        if self._fake.catalog is None or self._fake.catalog.name != name:
            raise NotFound(f"catalog {name} does not exist")
        return self._fake.catalog


class _WorkspaceBindings:
    def __init__(self, fake: FakeWorkspace) -> None:
        self._fake = fake

    def get_bindings(
        self,
        securable_type: str,
        securable_name: str,
        *,
        max_results: int | None = None,
        page_token: str | None = None,
    ) -> Iterator[WorkspaceBinding]:
        assert securable_type == "catalog"
        return iter(self._fake.bindings)


class _ClusterPolicies:
    """`list` omits definitions (as the API may); only `get` returns them."""

    def __init__(self, fake: FakeWorkspace) -> None:
        self._fake = fake

    def list(self, *, sort_column: Any = None, sort_order: Any = None) -> Iterator[Policy]:
        return iter(replace(p, definition=None) for p in self._fake.policies)

    def get(self, policy_id: str) -> Policy:
        return next(p for p in self._fake.policies if p.policy_id == policy_id)


@dataclass
class FakeWorkspace:
    me: User = field(
        default_factory=lambda: User(user_name="sp", groups=[ComplexValue(display="admins")])
    )
    me_error: Exception | None = None
    validation: ValidateStorageCredentialResponse = field(
        default_factory=ValidateStorageCredentialResponse
    )
    catalog: CatalogInfo | None = None
    bindings: list[WorkspaceBinding] = field(default_factory=list)
    policies: list[Policy] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.current_user = _CurrentUser(self)
        self.storage_credentials = _StorageCredentials(self)
        self.catalogs = _Catalogs(self)
        self.workspace_bindings = _WorkspaceBindings(self)
        self.cluster_policies = _ClusterPolicies(self)


class FakeDatabricks:
    """Satisfies `DatabricksApi`."""

    def __init__(
        self, account: FakeAccount | None = None, workspace: FakeWorkspace | None = None
    ) -> None:
        self.fake = account or FakeAccount()
        self.fake_workspace = workspace or FakeWorkspace()
        self.hosts: list[str] = []
        self._account = _Account(self.fake)
        self.account_outputs: Mapping[str, Any] | None = None

    @property
    def account(self) -> Any:
        return self._account

    def workspace(self, host: str) -> Any:
        self.hosts.append(host)
        return self.fake_workspace


def metastore(name: str, region: str = "us-east-1") -> MetastoreInfo:
    return MetastoreInfo(name=name, region=region, metastore_id=f"id-{name}")


def user(email: str) -> User:
    return User(user_name=email)


def service_principal(application_id: str, *, admin: bool) -> ServicePrincipal:
    roles = [ComplexValue(value="account_admin")] if admin else []
    return ServicePrincipal(
        id="sp-1", application_id=application_id, display_name="ci", roles=roles
    )


def workspace_in(status: WorkspaceStatus, message: str | None = None) -> Workspace:
    return Workspace(workspace_id=1234, workspace_status=status, workspace_status_message=message)


def workspace(name: str, deployment_name: str) -> Workspace:
    return Workspace(workspace_name=name, deployment_name=deployment_name)


class Overlay:
    """A boto3 client with some methods replaced; the rest go to the real (moto) client."""

    def __init__(self, base: Any, **methods: Any) -> None:
        self._base = base
        self._methods = methods

    def __getattr__(self, name: str) -> Any:
        if name in self._methods:
            return self._methods[name]
        return getattr(self._base, name)


class OverlaySession:
    """A boto3 session whose clients may be replaced per service."""

    def __init__(self, base: boto3.session.Session, **clients: Any) -> None:
        self._base: Any = base
        self._clients = clients

    def client(self, service: str, **kwargs: Any) -> Any:
        if service in self._clients:
            return self._clients[service]
        return self._base.client(service, **kwargs)


def client_error(code: str, operation: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)
