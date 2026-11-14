"""The Databricks service principal's credentials and the SDK clients built from them.

One service principal (OAuth machine-to-machine) manages every workspace: its
client ID and the account ID are in config.yaml, its client secret only in SSM.
The secret goes to Terraform through the child process's environment and to the
SDK clients in memory; it is never written to a file, logged, or put in an
exception message.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from botocore.exceptions import BotoCoreError, ClientError

from .errors import ConfigError, PreflightError
from .settings import DatabricksSection, Settings

if TYPE_CHECKING:
    from databricks.sdk import AccountClient, WorkspaceClient

#: Where the owner is told how to create the service principal and store its secret.
RUNBOOK = "docs/databricks.md#credentials"
#: The account console's API host (AWS commercial).
ACCOUNTS_HOST = "https://accounts.cloud.databricks.com"
_AUTH_TYPE = "oauth-m2m"


def _section(settings: Settings) -> DatabricksSection:
    if settings.databricks is None:
        raise ConfigError("config.yaml has no databricks section")
    return settings.databricks


def load_secret(ssm: Any, parameter: str) -> str:
    """The decrypted client secret in SSM `parameter`.

    `ssm` is a boto3 SSM client. A missing, empty or unreadable parameter is a
    preflight failure pointing at the runbook; no message carries the value.
    """
    try:
        response = ssm.get_parameter(Name=parameter, WithDecryption=True)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            raise PreflightError(
                f"the Databricks client secret is not in SSM: {parameter} does not exist; "
                f"store it as a SecureString (see {RUNBOOK})"
            ) from None
        raise PreflightError(
            f"cannot read the Databricks client secret {parameter} from SSM ({code}); see {RUNBOOK}"
        ) from None
    except BotoCoreError as exc:  # no credentials, no endpoint, a timeout
        raise PreflightError(
            f"cannot read the Databricks client secret {parameter} from SSM "
            f"({type(exc).__name__}); check the AWS credentials and network, see {RUNBOOK}"
        ) from None
    value = str(response.get("Parameter", {}).get("Value", ""))
    if not value.strip():
        raise PreflightError(
            f"the Databricks client secret {parameter} in SSM is empty; see {RUNBOOK}"
        )
    return value


def secret_modified(ssm: Any, parameter: str) -> datetime | None:
    """When the client secret in SSM `parameter` was last modified, or None if it is absent.

    Reads (and decrypts) the parameter to prove the caller can, then discards the
    value: only the date leaves this function. Other failures are a preflight
    failure naming the error code, never the value.
    """
    try:
        response = ssm.get_parameter(Name=parameter, WithDecryption=True)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            return None
        raise PreflightError(
            f"cannot read the Databricks client secret {parameter} from SSM ({code}); see {RUNBOOK}"
        ) from None
    except BotoCoreError as exc:
        raise PreflightError(
            f"cannot read the Databricks client secret {parameter} from SSM "
            f"({type(exc).__name__}); check the AWS credentials and network, see {RUNBOOK}"
        ) from None
    modified = response.get("Parameter", {}).get("LastModifiedDate")
    return modified if isinstance(modified, datetime) else datetime.min


def terraform_env(settings: Settings, secret: str) -> dict[str, str]:
    """What the Databricks Terraform provider authenticates with, for the child's env only."""
    section = _section(settings)
    return {
        "DATABRICKS_ACCOUNT_ID": section.account_id,
        "DATABRICKS_CLIENT_ID": section.client_id,
        "DATABRICKS_CLIENT_SECRET": secret,
    }


class DatabricksClients:
    """The account client and per-workspace clients of the service principal.

    Satisfies `checks.framework.DatabricksApi`. Clients are built on first use,
    so constructing this makes no network call.
    """

    def __init__(
        self,
        settings: Settings,
        secret: str,
        account_outputs: Mapping[str, Any] | None = None,
    ) -> None:
        section = _section(settings)
        self._account_id = section.account_id
        self._client_id = section.client_id
        self._secret = secret
        self.account_outputs: Mapping[str, Any] | None = account_outputs
        self._account: AccountClient | None = None
        self._workspaces: dict[str, WorkspaceClient] = {}

    def __repr__(self) -> str:
        return f"DatabricksClients(account_id={self._account_id!r}, client_id={self._client_id!r})"

    @property
    def account(self) -> AccountClient:
        if self._account is None:
            from databricks.sdk import AccountClient

            self._account = AccountClient(
                host=ACCOUNTS_HOST,
                account_id=self._account_id,
                client_id=self._client_id,
                client_secret=self._secret,
                auth_type=_AUTH_TYPE,
            )
        return self._account

    def workspace(self, host: str) -> WorkspaceClient:
        if host not in self._workspaces:
            from databricks.sdk import WorkspaceClient

            self._workspaces[host] = WorkspaceClient(
                host=host,
                client_id=self._client_id,
                client_secret=self._secret,
                auth_type=_AUTH_TYPE,
            )
        return self._workspaces[host]
