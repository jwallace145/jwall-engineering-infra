from __future__ import annotations

import socket
from typing import Any

import boto3
import pytest
from botocore.exceptions import (
    BotoCoreError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoCredentialsError,
)
from databricks.sdk import AccountClient, WorkspaceClient
from moto import mock_aws

from jwall_engineering_sites.checks.framework import DatabricksApi
from jwall_engineering_sites.databricks_auth import (
    ACCOUNTS_HOST,
    RUNBOOK,
    DatabricksClients,
    load_secret,
    terraform_env,
)
from jwall_engineering_sites.errors import ConfigError, PreflightError
from jwall_engineering_sites.settings import Settings

SENTINEL = "S3CR3T-SENTINEL"
PARAMETER = "/jwall-engineering/databricks/client-secret"
ACCOUNT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "66666666-7777-8888-9999-000000000000"


@pytest.fixture
def ids(settings: Settings) -> Settings:
    """The fixture settings with real-looking (non-placeholder) Databricks IDs."""
    assert settings.databricks is not None
    section = settings.databricks.model_copy(
        update={"account_id": ACCOUNT_ID, "client_id": CLIENT_ID}
    )
    return settings.model_copy(update={"databricks": section})


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("a network call was attempted")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@mock_aws
def test_load_secret_reads_the_decrypted_parameter() -> None:
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name=PARAMETER, Value=SENTINEL, Type="SecureString")
    assert load_secret(ssm, PARAMETER) == SENTINEL


@mock_aws
def test_missing_secret_is_preflight_with_runbook() -> None:
    ssm = boto3.client("ssm", region_name="us-east-1")
    with pytest.raises(PreflightError) as caught:
        load_secret(ssm, PARAMETER)
    message = str(caught.value)
    assert RUNBOOK == "docs/databricks.md#credentials"
    assert RUNBOOK in message
    assert PARAMETER in message
    assert caught.value.exit_code == 3


@mock_aws
def test_empty_secret_is_preflight_without_the_value() -> None:
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name=PARAMETER, Value=" ", Type="SecureString")
    with pytest.raises(PreflightError, match=r"docs/databricks\.md#credentials"):
        load_secret(ssm, PARAMETER)


def test_unreadable_secret_is_preflight_without_the_value() -> None:
    class Denied:
        def get_parameter(self, **kwargs: Any) -> Any:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "not allowed"}},
                "GetParameter",
            )

    with pytest.raises(PreflightError) as caught:
        load_secret(Denied(), PARAMETER)
    assert "AccessDeniedException" in str(caught.value)
    assert RUNBOOK in str(caught.value)


@pytest.mark.parametrize(
    "error",
    [
        NoCredentialsError(),
        EndpointConnectionError(endpoint_url="https://ssm.us-east-1.amazonaws.com"),
        ConnectTimeoutError(endpoint_url="https://ssm.us-east-1.amazonaws.com"),
    ],
    ids=["no-credentials", "endpoint", "timeout"],
)
def test_aws_failure_is_preflight_with_runbook(error: BotoCoreError) -> None:
    class Failing:
        def get_parameter(self, **kwargs: Any) -> Any:
            raise error

    with pytest.raises(PreflightError) as caught:
        load_secret(Failing(), PARAMETER)
    assert RUNBOOK in str(caught.value)
    assert PARAMETER in str(caught.value)
    assert type(error).__name__ in str(caught.value)


def test_terraform_env(ids: Settings) -> None:
    assert terraform_env(ids, SENTINEL) == {
        "DATABRICKS_ACCOUNT_ID": ACCOUNT_ID,
        "DATABRICKS_CLIENT_ID": CLIENT_ID,
        "DATABRICKS_CLIENT_SECRET": SENTINEL,
    }


def test_terraform_env_needs_the_databricks_section(settings: Settings) -> None:
    without = settings.model_copy(update={"databricks": None, "projects": {}})
    with pytest.raises(ConfigError, match="databricks"):
        terraform_env(without, SENTINEL)


def test_clients_are_lazy_and_offline(ids: Settings, no_network: None) -> None:
    clients = DatabricksClients(ids, SENTINEL)
    assert clients.account_outputs is None
    # Constructing and inspecting never reaches the network nor builds a client.
    assert SENTINEL not in repr(clients)


def test_clients_satisfy_the_check_protocol(ids: Settings) -> None:
    api: DatabricksApi = DatabricksClients(ids, SENTINEL, account_outputs={"workspace_id": 1})
    assert api.account_outputs == {"workspace_id": 1}


class _Recorded:
    """Stands in for an SDK client: the real ones resolve host metadata and OIDC
    endpoints over the network as they are built."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    import databricks.sdk

    monkeypatch.setattr(databricks.sdk, "AccountClient", type("Account", (_Recorded,), {}))
    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", type("Workspace", (_Recorded,), {}))


def test_sdk_clients_are_the_real_types() -> None:
    import databricks.sdk

    assert databricks.sdk.AccountClient is AccountClient
    assert databricks.sdk.WorkspaceClient is WorkspaceClient


def test_account_client_uses_oauth_m2m(ids: Settings, recorded: None) -> None:
    clients = DatabricksClients(ids, SENTINEL)
    account = clients.account
    assert account.kwargs == {  # type: ignore[attr-defined]
        "host": ACCOUNTS_HOST,
        "account_id": ACCOUNT_ID,
        "client_id": CLIENT_ID,
        "client_secret": SENTINEL,
        "auth_type": "oauth-m2m",
    }
    assert ACCOUNTS_HOST == "https://accounts.cloud.databricks.com"
    assert clients.account is account  # built once


def test_workspace_client_uses_the_same_service_principal(ids: Settings, recorded: None) -> None:
    clients = DatabricksClients(ids, SENTINEL)
    host = "https://dbc-0000.cloud.databricks.com"
    workspace = clients.workspace(host)
    assert workspace.kwargs == {  # type: ignore[attr-defined]
        "host": host,
        "client_id": CLIENT_ID,
        "client_secret": SENTINEL,
        "auth_type": "oauth-m2m",
    }
    assert clients.workspace(host) is workspace
    assert clients.workspace("https://dbc-1111.cloud.databricks.com") is not workspace
