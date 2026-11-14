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
from databricks.sdk.service.catalog import MetastoreInfo
from databricks.sdk.service.iam import ComplexValue, ServicePrincipal, User
from databricks.sdk.service.provisioning import Workspace


class _Metastores:
    def __init__(self, items: list[MetastoreInfo]) -> None:
        self._items = items

    def list(self) -> Iterator[MetastoreInfo]:
        return iter(self._items)


class _Workspaces:
    def __init__(self, items: list[Workspace]) -> None:
        self._items = items

    def list(self) -> Iterator[Workspace]:
        return iter(self._items)


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
    token_error: Exception | None = None
    list_error: Exception | None = None


class _Account:
    def __init__(self, fake: FakeAccount) -> None:
        self.metastores = _Metastores(fake.metastores)
        self.users = _Users(fake.users)
        self.service_principals = _ServicePrincipals(fake.service_principals)
        self.workspaces = _Workspaces(fake.workspaces)

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


class FakeDatabricks:
    """Satisfies `DatabricksApi`."""

    def __init__(self, account: FakeAccount | None = None) -> None:
        self.fake = account or FakeAccount()
        self._account = _Account(self.fake)
        self.account_outputs: Mapping[str, Any] | None = None

    @property
    def account(self) -> Any:
        return self._account

    def workspace(self, host: str) -> Any:  # pragma: no cover - not used by these checks
        raise AssertionError(f"unexpected workspace client for {host}")


def metastore(name: str, region: str = "us-east-1") -> MetastoreInfo:
    return MetastoreInfo(name=name, region=region, metastore_id=f"id-{name}")


def user(email: str) -> User:
    return User(user_name=email)


def service_principal(application_id: str, *, admin: bool) -> ServicePrincipal:
    roles = [ComplexValue(value="account_admin")] if admin else []
    return ServicePrincipal(
        id="sp-1", application_id=application_id, display_name="ci", roles=roles
    )


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
