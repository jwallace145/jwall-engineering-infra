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

import re
from dataclasses import dataclass
from pathlib import Path

from .report import truncate_tail

MAX_RAW_BYTES = 6_000
RUNBOOK_DOC = "docs/databricks.md"

_SECRET_LINE = re.compile(r"DATABRICKS_CLIENT_SECRET|client_secret", re.IGNORECASE)
_BOX = re.compile(r"^[│╷╵\s]*(?:│ ?)?")
_ERROR_START = re.compile(r"^\s*Error: ")
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


CATALOGUE: tuple[Diagnosis, ...] = (
    Diagnosis(
        error_class="DBX-CRED-VALIDATION",
        pattern=_re(r"Failed credentials validation checks"),
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
        pattern=_re(r"Failed network validation|network validation (?:failed|error)"),
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
        error_class="AWS-IAM-PROPAGATION",
        pattern=_re(
            r"MalformedPolicyDocument: Invalid principal in policy|"
            r"InvalidParameterValue: Value \(.*\) for parameter iamInstanceProfile"
        ),
        title="A new IAM role is not visible yet",
        likely_cause=(
            "IAM is eventually consistent: a policy referenced a role created seconds earlier."
        ),
        next_steps=("Re-run apply; it resumes where it stopped.",),
        runbook=_anchor("aws-iam-propagation"),
        retry_safe=True,
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
    Diagnosis(
        error_class="DBX-METASTORE-ALREADY-ASSIGNED",
        pattern=_re(r"already assigned to metastore|already has a metastore"),
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
            r"\A(?!.*(?:already assigned to metastore|already has a metastore))"
            r".*(?:metastore assignment|databricks_metastore_assignment)",
            re.IGNORECASE | re.DOTALL,
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
        error_class="DBX-WORKSPACE-FAILED",
        pattern=_re(r"failed to reach RUNNING, got FAILED|Workspace failed to launch"),
        title="The workspace entered the FAILED state",
        likely_cause=(
            "Databricks provisioned the workspace but could not finish: usually a credential, "
            "storage or network configuration it accepted but cannot use."
        ),
        next_steps=(
            "Read the status message in the error and the debug log.",
            "A FAILED workspace cannot be repaired; remove it in the account console, then re-run.",
        ),
        runbook=_anchor("dbx-workspace-failed"),
        retry_safe=False,
    ),
    Diagnosis(
        error_class="DBX-NO-EGRESS",
        pattern=_re(r"NPIP_TUNNEL_SETUP_FAILURE|BOOTSTRAP_TIMEOUT"),
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
)

RUNBOOK_ANCHORS: tuple[str, ...] = tuple(d.runbook for d in CATALOGUE)


def _error_blocks(output: str) -> str:
    """Only the error block(s) of Terraform's output, secrets removed."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in output.splitlines():
        text = _BOX.sub("", line).rstrip()
        if _ERROR_START.match(text):
            current = [text]
            blocks.append(current)
        elif "╵" in line:
            current = None
        elif current is not None:
            current.append(text)
    lines = [ln for block in blocks for ln in block] if blocks else output.splitlines()[-40:]
    return "\n".join(ln for ln in lines if not _SECRET_LINE.search(ln)).strip()


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
    other = "workspace" if partial == "account" else "account"
    lines: list[str] = []
    if partial:
        lines += [f"{partial} stack applied; {other} stack failed; {_RE_RUN_SAFE}", ""]
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
