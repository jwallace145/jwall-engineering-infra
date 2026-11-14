"""In-memory stand-ins for the Databricks account client and boto3 overlays.

Nothing here touches the network.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import boto3
from botocore.exceptions import ClientError


class _Listing:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def list(self, **_: Any) -> Iterable[Any]:
        return iter(self._items)


@dataclass
class FakeAccount:
    metastores: list[Any] = field(default_factory=list)
    users: list[Any] = field(default_factory=list)
    service_principals: list[Any] = field(default_factory=list)
    workspaces: list[Any] = field(default_factory=list)
    token_error: Exception | None = None

    @property
    def config(self) -> Any:
        def authenticate() -> dict[str, str]:
            if self.token_error is not None:
                raise self.token_error
            return {"Authorization": "Bearer fake-token"}

        return SimpleNamespace(authenticate=authenticate)


class _Account:
    """The attribute surface checks use: `.metastores.list()` and friends."""

    def __init__(self, fake: FakeAccount) -> None:
        self.metastores = _Listing(fake.metastores)
        self.users = _Listing(fake.users)
        self.service_principals = _Listing(fake.service_principals)
        self.workspaces = _Listing(fake.workspaces)
        self.config = fake.config


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


def metastore(name: str, region: str = "us-east-1") -> Any:
    return SimpleNamespace(name=name, region=region, metastore_id=f"id-{name}")


def user(email: str) -> Any:
    return SimpleNamespace(user_name=email)


def service_principal(application_id: str, *, admin: bool) -> Any:
    roles = [SimpleNamespace(value="account_admin")] if admin else []
    return SimpleNamespace(application_id=application_id, roles=roles, display_name="ci")


def workspace(name: str, deployment_name: str) -> Any:
    return SimpleNamespace(workspace_name=name, deployment_name=deployment_name)


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
