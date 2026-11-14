"""Explain a failed Terraform run: what happened, why, what to do next.

`diagnose` parses the error block(s) out of Terraform's combined `-no-color`
output, matches them against `CATALOGUE` (known Databricks and AWS provider
failures) and returns a `Failure`. `render_failure` turns it into Markdown that
reads the same on the console and in the job summary. An error nobody has
catalogued still yields the stack, the resource address and the debug log.

Runbook anchors live in `RUNBOOK_ANCHORS`, one list; `docs/databricks.md` has a
heading for each.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .report import truncate_tail

MAX_RAW_BYTES = 6_000
RUNBOOK_DOC = "docs/databricks.md"

_SECRET_VALUE = re.compile(r"(client_secret\s*[=:]\s*)(\"?)[^\s\",]+", re.IGNORECASE)
_MASK = "***"
_BOX_CHARS = "╷╵│ "
_ADDRESS = re.compile(r"^\s*with (\S+?),\s*$", re.MULTILINE)
_RE_RUN_SAFE = "re-running apply is safe"


@dataclass(frozen=True)
class Diagnosis:
    error_class: str
    pattern: re.Pattern[str]
    title: str
    likely_cause: str
    next_steps: tuple[str, ...]
    runbook: str
    retry_safe: bool


@dataclass(frozen=True)
class Failure:
    stack: str
    resource: str | None
    raw: str
    diagnosis: Diagnosis | None
    debug_log: Path


def _anchor(name: str) -> str:
    return f"{RUNBOOK_DOC}#{name}"


def _re(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern[str]:
    return re.compile(pattern, flags)


# First match wins: specific before generic (auth and validations first, the broad
# `already exists` and metastore entries last).
CATALOGUE: tuple[Diagnosis, ...] = (
    Diagnosis(
        error_class="DBX-AUTH",
        pattern=_re(
            r"invalid_client|Client authentication failed|invalid client secret|"
            r"cannot configure default credentials|oauth-m2m"
        ),
        title="Databricks rejected the service principal credentials",
        likely_cause=(
            "The OAuth client ID or secret is wrong, expired or not in the SSM parameter the "
            "job reads."
        ),
        next_steps=(
            "Check `/jwall-engineering/databricks/client-secret` holds the current secret.",
            "Confirm the service principal is an account admin and its secret has not expired.",
            "Re-run apply; no partial state is lost.",
        ),
        runbook=_anchor("dbx-auth"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="DBX-CRED-VALIDATION",
        pattern=_re(r"Failed credentials? (?:configuration )?validation checks"),
        title="Databricks rejected the cross-account role",
        likely_cause=(
            "The cross-account IAM role is missing EC2 permissions, or its trust policy does not "
            "let the Databricks account (external ID) assume it."
        ),
        next_steps=(
            "Compare the role's policy with the checks named in the error.",
            "Confirm the trust policy names the Databricks account ID as the external ID.",
            "If the role was only just created, wait a minute and re-run apply (IAM is eventually "
            "consistent).",
        ),
        runbook=_anchor("dbx-cred-validation"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="DBX-STORAGE-VALIDATION",
        pattern=_re(r"Failed storage configuration validation checks"),
        title="Databricks cannot use the root storage bucket",
        likely_cause=(
            "The root bucket policy does not grant the cross-account role List, Put, Delete and "
            "GetBucketLocation, or the bucket is in a different region."
        ),
        next_steps=(
            "Check the bucket policy names the cross-account role and the bucket's region.",
            "Re-run apply once the policy is in place.",
        ),
        runbook=_anchor("dbx-storage-validation"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="DBX-NETWORK-VALIDATION",
        pattern=_re(
            r"Failed network validation|network validation (?:failed|error)|"
            r"already used by another Network|is missing default route|"
            r"could not be updated with latest Security Group Rules"
        ),
        title="Databricks rejected the VPC configuration",
        likely_cause=(
            "Subnets, security group or routes do not meet the customer-managed VPC rules: "
            "subnets in two availability zones, a self-referencing security group, outbound access."
        ),
        next_steps=(
            "Read the check names in the error and compare with the VPC the stack created.",
            "Confirm the subnets are in at least two availability zones.",
            "Confirm the security group allows all TCP and UDP traffic within itself.",
        ),
        runbook=_anchor("dbx-network-validation"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="DBX-WORKSPACE-FAILED",
        pattern=_re(r"Workspace failed to (?:create|launch)|failed to reach RUNNING, got FAILED"),
        title="The workspace entered the FAILED state",
        likely_cause=(
            "Databricks provisioned the workspace but could not finish: usually a credential, "
            "storage or network configuration it accepted but cannot use."
        ),
        next_steps=(
            "Fix the cause named in the error (credential, storage or network) and re-run apply.",
            "The provider deletes the failed workspace itself; only if the error also reports a "
            "delete failure, remove it by hand in the account console first.",
        ),
        runbook=_anchor("dbx-workspace-failed"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="DBX-NO-EGRESS",
        pattern=_re(
            r"NPIP_TUNNEL_SETUP_FAILURE|BOOTSTRAP_TIMEOUT|NPIP_TUNNEL_TOKEN_FAILURE|"
            r"CONTROL_PLANE_CONNECTION_FAILURE|NETWORK_CHECK_CONTROL_PLANE_FAILURE"
        ),
        title="Cluster nodes cannot reach the Databricks control plane",
        likely_cause=(
            "No outbound path from the private subnets (NAT, route table or endpoint) so the "
            "secure cluster connectivity tunnel never came up."
        ),
        next_steps=(
            "Check the NAT instance is running and the private route tables point at it.",
            "Check the security group allows outbound 443 and the Databricks relay port.",
            "Re-run apply once egress works.",
        ),
        runbook=_anchor("dbx-no-egress"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="AWS-IAM-PROPAGATION",
        pattern=_re(r"MalformedPolicy(?:Document)?: Invalid principal in policy"),
        title="A new IAM role is not visible yet",
        likely_cause=(
            "IAM is eventually consistent: a policy referenced a role created seconds earlier."
        ),
        next_steps=(
            "Re-run apply; it resumes where it stopped.",
            "The provider already retried; if it repeats, check the principal ARN in the "
            "policy exists.",
        ),
        runbook=_anchor("aws-iam-propagation"),
        retry_safe=True,
    ),
    Diagnosis(
        error_class="DBX-METASTORE-ALREADY-ASSIGNED",
        pattern=_re(
            r"already assigned to metastore|already has a metastore|metastore[^\n]*already exists"
        ),
        title="The workspace was already assigned to the region's metastore",
        likely_cause=(
            "Databricks auto-assigned the new workspace to the metastore for its region, so "
            "Terraform's own assignment collides with it."
        ),
        next_steps=(
            "Import the existing assignment: "
            "`terraform import databricks_metastore_assignment.this "
            "<workspace_id>|<metastore_id>`.",
            "Or disable metastore auto-assignment for the account and re-run.",
            "Re-run apply afterwards.",
        ),
        runbook=_anchor("dbx-metastore-already-assigned"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="DBX-METASTORE-ASSIGNMENT",
        pattern=_re(
            r"cannot (?:create|update) metastore assignment:.*"
            r"(?:permission|metastore admin|region)"
        ),
        title="The metastore could not be assigned to the workspace",
        likely_cause=(
            "The service principal is not a metastore admin, or the metastore is in another region."
        ),
        next_steps=(
            "Make the Terraform service principal a metastore admin.",
            "Check the metastore and workspace share a region.",
            "Re-run apply.",
        ),
        runbook=_anchor("dbx-metastore-assignment"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="TF-ALREADY-EXISTS",
        pattern=_re(
            r"EntityAlreadyExists|BucketAlreadyOwnedByYou|AlreadyExistsException|already exists"
        ),
        title="The resource already exists outside Terraform state",
        likely_cause=(
            "A previous run created it but state was not saved, or it was created by hand."
        ),
        next_steps=(
            "Confirm the existing resource belongs to this project.",
            "Import it: `terraform import <address> <id>` (the address is in the error).",
            "Never delete a resource this stack does not own; ask the owner.",
        ),
        runbook=_anchor("tf-already-exists"),
        retry_safe=False,
    ),
)

#: Anchors the preflight checks (`checks/databricks_*.py`) link to, beside the catalogue's.
CHECK_ANCHORS: tuple[str, ...] = tuple(
    _anchor(name)
    for name in (
        "one-time-setup",
        "credentials",
        "network-cidrs",
        "compute-policies",
        "resource-names",
        "required-tags",
        "account-admin",
        "metastore",
        "workspace-members",
        "bucket-names",
        "instance-availability",
        "vcpu-quota",
        "nat-ami",
        "deployment-name",
        "workspace-readiness",
        "workspace-admin",
        "metastore-assignment",
        "catalog",
        "egress",
        "account-state",
    )
)

RUNBOOK_ANCHORS: tuple[str, ...] = tuple(
    dict.fromkeys([*(d.runbook for d in CATALOGUE), *CHECK_ANCHORS])
)


def _redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask secret values, keep the surrounding text (the SDK puts them in the summary line).

    `secrets` are values to mask wherever they appear, beside the environment's."""
    for secret in (os.environ.get("DATABRICKS_CLIENT_SECRET"), *secrets):
        if secret:
            text = text.replace(secret, _MASK)
    return _SECRET_VALUE.sub(rf"\1\2{_MASK}", text)


def _error_blocks(output: str) -> str:
    """Only the error block(s) of Terraform's output, secrets masked.

    A block starts at an `Error: ` line and ends at the next `Error:` or `Warning:` line,
    a box-drawing `╵` or the end of the output. Plain (`-no-color`) and boxed layouts both work.
    """
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in output.splitlines():
        text = line.lstrip(_BOX_CHARS) if line[:1] in ("╷", "╵", "│") else line
        if text.startswith("Error: "):
            current = [text.rstrip()]
            blocks.append(current)
        elif text.startswith("Warning: ") or "╵" in line:
            current = None
        elif current is not None:
            current.append(text.rstrip())
    lines = [ln for block in blocks for ln in block] if blocks else output.splitlines()[-40:]
    return _redact("\n".join(lines)).strip()


def _head(text: str, max_bytes: int) -> str:
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode(errors="ignore") + "\n... (truncated)"


def diagnose(stack: str, output: str, debug_log: Path) -> Failure:
    blocks = _error_blocks(output)
    match = _ADDRESS.search(blocks)
    diagnosis = next((d for d in CATALOGUE if d.pattern.search(blocks)), None)
    raw = (
        _head(blocks, MAX_RAW_BYTES)
        if "Error: " in blocks
        else truncate_tail(blocks, MAX_RAW_BYTES)[0]
    )
    return Failure(
        stack=stack,
        resource=match.group(1) if match else None,
        raw=raw,
        diagnosis=diagnosis,
        debug_log=debug_log,
    )


def render_failure(failure: Failure, partial: str | None = None) -> str:
    """Markdown for the console and the job summary."""
    if partial not in (None, "account"):
        raise ValueError(f"partial must be None or 'account', got {partial!r}")
    lines: list[str] = []
    if partial:
        lines += [f"{partial} stack applied; workspace stack failed; {_RE_RUN_SAFE}", ""]
    where = f"`{failure.stack}`" + (f", resource `{failure.resource}`" if failure.resource else "")
    d = failure.diagnosis
    if d is None:
        lines += [
            f"**Terraform failed** in {where}.",
            "",
            "This error is not in the catalogue. Read the error below and the debug log.",
        ]
    else:
        lines += [f"**{d.error_class}: {d.title}** in {where}.", ""]
        lines += [f"Likely cause: {d.likely_cause}", "", "Next steps:"]
        lines += [f"{i}. {step}" for i, step in enumerate(d.next_steps, 1)]
        lines += ["", f"Runbook: {d.runbook}"]
        if d.retry_safe and not partial:
            lines += ["", "Re-running apply is safe: it resumes where it stopped."]
    lines += [
        "",
        f"Debug log: `{failure.debug_log}`",
        "",
        "```",
        failure.raw.replace("```", "'''"),
        "```",
    ]
    return "\n".join(lines) + "\n"
