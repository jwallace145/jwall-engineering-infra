"""Databricks `live` checks: read-only calls to AWS and the Databricks account.

They run before `plan`/`apply` and in `databricks doctor`. Every check SKIPs
until `databricks.enabled` is true (the owner's one-time setup). No failure
message carries the client secret or an SDK exception's text.
Registered at import; `checks.load_builtin_checks()` imports this module.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from databricks.sdk.errors.base import DatabricksError

from ..databricks_auth import load_secret
from ..databricks_names import REGION, ZONES, derived_names
from ..errors import ConfigError, PreflightError
from ..settings import DatabricksSection
from ..stack import deployment_name
from ._results import Meta
from .framework import CheckContext, CheckFn, CheckResult, Severity, Stage, check

_DBX = frozenset({"databricks"})
SKIP_HINT = (
    "set databricks.enabled: true in config.yaml after the one-time setup in "
    "docs/databricks.md#one-time-setup"
)
_PLACEHOLDER_HINT = "fill in config.yaml databricks.account_id/client_id (not the nil UUID)"
#: EC2 "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances", in vCPUs.
_STANDARD_VCPU_QUOTA = "L-1216C47A"

LIVE_001 = Meta("DBX-LIVE-001", "The Databricks client secret is in SSM", "credentials")
LIVE_002 = Meta("DBX-LIVE-002", "Databricks issues an OAuth token", "credentials")
LIVE_003 = Meta("DBX-LIVE-003", "The service principal is an account admin", "account-admin")
LIVE_004 = Meta("DBX-LIVE-004", "The metastore exists in us-east-1", "metastore")
LIVE_005 = Meta("DBX-LIVE-005", "Every workspace member is an account user", "workspace-members")
LIVE_006 = Meta("DBX-LIVE-006", "The derived bucket names are free or ours", "bucket-names")
LIVE_007 = Meta(
    "DBX-LIVE-007",
    "Policy node types are offered in both zones",
    "instance-availability",
    Severity.WARNING,
)
LIVE_008 = Meta(
    "DBX-LIVE-008", "EC2 vCPU quota covers the largest policy", "vcpu-quota", Severity.WARNING
)
LIVE_009 = Meta("DBX-LIVE-009", "The NAT AMI name resolves to one arm64 image", "nat-ami")
LIVE_010 = Meta("DBX-LIVE-010", "The deployment name is free", "deployment-name")


def _section(ctx: CheckContext) -> DatabricksSection:
    if ctx.settings.databricks is None:
        raise ConfigError("config.yaml has no databricks section")
    return ctx.settings.databricks


def _live(
    meta: Meta, *, needs_ids: bool = False, stage: Stage = Stage.LIVE
) -> Callable[[CheckFn], CheckFn]:
    """Register a check (LIVE unless `stage` says otherwise; the BETWEEN and AFTER modules reuse
    this) that SKIPs until Databricks is enabled (and, with `needs_ids`,
    until `account_id`/`client_id` are real)."""

    def wrap(fn: CheckFn) -> CheckFn:
        def guarded(ctx: CheckContext) -> Iterable[CheckResult]:
            section = _section(ctx)
            if not section.enabled:
                yield meta.skipped(SKIP_HINT, runbook="docs/databricks.md#one-time-setup")
                return
            if needs_ids and section.has_placeholder_ids:
                yield meta.skipped(
                    f"{_PLACEHOLDER_HINT}; DBX-LIVE-002 reports this", runbook=meta.runbook
                )
                return
            try:
                results = list(fn(ctx))
            except DatabricksError as exc:
                yield meta.failed(
                    "the Databricks call failed: check databricks.account_id and "
                    "databricks.client_id in config.yaml and the service principal's "
                    "permissions; DBX-LIVE-002 and the credentials runbook cover them",
                    error=type(exc).__name__,
                )
                return
            except ClientError as exc:
                yield meta.failed(
                    f"the AWS call {exc.operation_name} was refused: check the CI role's "
                    "databricks/read permissions",
                    error=type(exc).__name__,
                )
                return
            except BotoCoreError as exc:
                yield meta.failed(
                    "an AWS call failed before reaching AWS: check the credentials, region "
                    "and network",
                    error=type(exc).__name__,
                )
                return
            yield from results

        guarded.__doc__ = fn.__doc__
        return check(meta.check_id, meta.title, stage, _DBX, meta.severity, meta.runbook)(guarded)

    return wrap


def _session(ctx: CheckContext) -> Any:
    if ctx.session is None:
        raise PreflightError("a live check needs an AWS session")
    return ctx.session


def _account(ctx: CheckContext) -> Any:
    if ctx.databricks is None:
        raise PreflightError("a live check needs the Databricks clients")
    return ctx.databricks.account


@_live(LIVE_001)
def secret_exists(ctx: CheckContext) -> Iterable[CheckResult]:
    parameter = _section(ctx).secret_parameter
    ssm = _session(ctx).client("ssm")
    try:
        load_secret(ssm, parameter)
    except PreflightError as exc:
        yield LIVE_001.failed(
            f"store the service principal's secret as a SecureString at {parameter} (or point "
            f"databricks.secret_parameter in config.yaml at it): {exc}",
            parameter=parameter,
        )
        return
    yield LIVE_001.passed(parameter=parameter)


@_live(LIVE_002)
def token_issued(ctx: CheckContext) -> Iterable[CheckResult]:
    section = _section(ctx)
    if section.has_placeholder_ids:
        yield LIVE_002.failed(_PLACEHOLDER_HINT, account_id=section.account_id)
        return
    try:
        _account(ctx).config.authenticate()
    except PreflightError:
        raise
    except Exception as exc:  # the SDK's message may quote credentials; keep the type only
        yield LIVE_002.failed(
            "Databricks refused the credentials: check databricks.account_id and "
            "databricks.client_id in config.yaml and that the SSM secret is the current one",
            error=type(exc).__name__,
        )
        return
    yield LIVE_002.passed(client_id=section.client_id)


@_live(LIVE_003, needs_ids=True)
def is_account_admin(ctx: CheckContext) -> Iterable[CheckResult]:
    client_id = _section(ctx).client_id
    accounts = _account(ctx).service_principals
    found = [
        sp
        for sp in accounts.list(filter=f"applicationId eq '{client_id}'")
        if str(sp.application_id).lower() == client_id.lower()
    ]
    if not found:
        yield LIVE_003.failed(
            f"no service principal with application ID {client_id} is in the account: check "
            "databricks.client_id in config.yaml",
            client_id=client_id,
        )
        return
    # The list response may omit `roles`; read each principal in full.
    roles = {r.value for sp in found for r in (accounts.get(sp.id).roles or [])}
    if "account_admin" not in roles:
        yield LIVE_003.failed(
            f"grant the service principal {client_id} the account admin role directly in the "
            "account console (databricks.client_id in config.yaml); admin through a group "
            "is not detected",
            client_id=client_id,
        )
        return
    yield LIVE_003.passed(client_id=client_id)


@_live(LIVE_004, needs_ids=True)
def metastore_present(ctx: CheckContext) -> Iterable[CheckResult]:
    name = _section(ctx).metastore_name
    in_region = [m for m in _account(ctx).metastores.list() if m.region == REGION]
    if any(m.name == name for m in in_region):
        yield LIVE_004.passed(metastore=name)
        return
    if in_region:
        other = ", ".join(sorted(str(m.name) for m in in_region))
        yield LIVE_004.failed(
            f"{REGION} already has the metastore {other}, which Databricks created "
            "automatically. With the owner's go-ahead, delete it in the account console "
            "(Catalog > Metastores, unassign workspaces, then delete) and re-run; the "
            f"foundation creates {name}. "
            "Or set databricks.metastore_name in config.yaml to that name",
            existing=other,
        )
        return
    yield LIVE_004.failed(
        f"the metastore {name} does not exist yet: apply the foundation first "
        "(databricks.metastore_name in config.yaml)",
        metastore=name,
    )


@_live(LIVE_005, needs_ids=False)
def members_exist(ctx: CheckContext) -> Iterable[CheckResult]:
    approved = ctx.project.databricks_approved
    if approved is None:
        raise ConfigError(f"add projects.{ctx.project.project}.databricks to config.yaml")
    emails = [*approved.admins, *approved.users]
    field = f"projects.{ctx.project.project}.databricks.admins/users"
    placeholders = [e for e in emails if e.lower().endswith("@example.com")]
    if placeholders:
        yield LIVE_005.failed(
            f"replace the placeholder email {', '.join(placeholders)} in {field} in config.yaml",
            placeholders=", ".join(placeholders),
        )
        return
    if _section(ctx).has_placeholder_ids:
        yield LIVE_005.skipped(f"{_PLACEHOLDER_HINT}; DBX-LIVE-002 reports this")
        return
    known = {str(u.user_name).lower() for u in _account(ctx).users.list()}
    missing = [e for e in emails if e.lower() not in known]
    if missing:
        yield LIVE_005.failed(
            f"add {', '.join(missing)} as account users in the account console, or remove them "
            f"from {field} in config.yaml",
            missing=", ".join(missing),
        )
        return
    yield LIVE_005.passed(members=str(len(emails)))


@_live(LIVE_006)
def buckets_available(ctx: CheckContext) -> Iterable[CheckResult]:
    s3 = _session(ctx).client("s3")
    names = derived_names(ctx.project)
    taken = []
    for name in names.buckets.values():
        try:
            s3.head_bucket(Bucket=name)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("403", "AccessDenied"):
                taken.append(name)
            elif code not in ("404", "NoSuchBucket", "NotFound"):
                raise
    if taken:
        yield LIVE_006.failed(
            f"the bucket name(s) {', '.join(taken)} are taken by another AWS account, or not "
            "readable by this role (check databricks/read); if taken, choose a different "
            "project name (the project file's `project`), since the modules derive the "
            "bucket names from it",
            taken=", ".join(taken),
        )
        return
    yield LIVE_006.passed(**names.buckets)


def _policy_node_types(ctx: CheckContext) -> dict[str, list[str]]:
    """Node types each of the project's policies permits (from node_type_id rules)."""
    central = _section(ctx).compute_policies
    out: dict[str, list[str]] = {}
    for name in ctx.project.databricks.compute_policies:
        rule = central[name].definition.get("node_type_id", {}) if name in central else {}
        values = rule.get("values") or ([rule["value"]] if "value" in rule else [])
        out[name] = [str(v) for v in values]
    return out


@_live(LIVE_007)
def node_types_in_zones(ctx: CheckContext) -> Iterable[CheckResult]:
    wanted = {t for types in _policy_node_types(ctx).values() for t in types}
    if not wanted:
        yield LIVE_007.passed(node_types="none")
        return
    response = (
        _session(ctx)
        .client("ec2")
        .describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[
                {"Name": "location", "Values": list(ZONES)},
                {"Name": "instance-type", "Values": sorted(wanted)},
            ],
        )
    )
    offered = {(o["InstanceType"], o["Location"]) for o in response["InstanceTypeOfferings"]}
    gaps = [f"{t} in {z}" for t in sorted(wanted) for z in ZONES if (t, z) not in offered]
    if gaps:
        yield LIVE_007.failed(
            f"not offered: {', '.join(gaps)}. Remove those node types from node_type_id in the "
            "policies under databricks.compute_policies in config.yaml, or clusters in that "
            "zone fail to start",
            gaps=", ".join(gaps),
        )
        return
    yield LIVE_007.passed(node_types=", ".join(sorted(wanted)))


@_live(LIVE_008)
def vcpu_headroom(ctx: CheckContext) -> Iterable[CheckResult]:
    central = _section(ctx).compute_policies
    session = _session(ctx)
    node_types = _policy_node_types(ctx)
    wanted = sorted({t for types in node_types.values() for t in types})
    if not wanted:
        yield LIVE_008.passed(needed="0")
        return
    infos = session.client("ec2").describe_instance_types(InstanceTypes=wanted)["InstanceTypes"]
    vcpus = {i["InstanceType"]: int(i["VCpuInfo"]["DefaultVCpus"]) for i in infos}
    worst, worst_policy = 0, ""
    for name, types in node_types.items():
        workers = int(central[name].definition.get("num_workers", {}).get("maxValue", 0))
        need = (workers + 1) * max((vcpus.get(t, 0) for t in types), default=0)
        if need > worst:
            worst, worst_policy = need, name
    quota = float(
        session.client("service-quotas").get_service_quota(
            ServiceCode="ec2", QuotaCode=_STANDARD_VCPU_QUOTA
        )["Quota"]["Value"]
    )
    facts = {"needed_vcpus": str(worst), "quota_vcpus": str(int(quota)), "policy": worst_policy}
    if worst > quota:
        yield LIVE_008.failed(
            f"the policy {worst_policy} can need {worst} vCPUs but the quota is {int(quota)}: "
            "request an increase for the Running On-Demand Standard instances quota, or lower "
            f"num_workers in databricks.compute_policies.{worst_policy} in config.yaml "
            "(this compares the quota with the policy, not the current usage)",
            **facts,
        )
        return
    yield LIVE_008.passed(**facts)


@_live(LIVE_009)
def nat_ami_resolves(ctx: CheckContext) -> Iterable[CheckResult]:
    name = _section(ctx).nat_ami
    images = (
        _session(ctx)
        .client("ec2")
        .describe_images(Owners=["amazon"], Filters=[{"Name": "name", "Values": [name]}])["Images"]
    )
    if len(images) != 1:
        yield LIVE_009.failed(
            f"databricks.nat_ami {name!r} matches {len(images)} images, not one: set it to a "
            "current Amazon Linux 2023 arm64 image name in config.yaml",
            matches=str(len(images)),
        )
        return
    architecture = images[0].get("Architecture")
    if architecture != "arm64":
        yield LIVE_009.failed(
            f"databricks.nat_ami {name!r} is {architecture}, not arm64: pick an arm64 image "
            "in config.yaml",
            architecture=str(architecture),
        )
        return
    yield LIVE_009.passed(ami=name, image_id=str(images[0].get("ImageId")))


@_live(LIVE_010, needs_ids=True)
def deployment_name_free(ctx: CheckContext) -> Iterable[CheckResult]:
    section = _section(ctx)
    name = deployment_name(
        ctx.project.project, section.deployment_name_prefix, ctx.project.databricks.deployment_name
    )
    if name is None:
        yield LIVE_010.skipped(
            "no databricks.deployment_name_prefix is set, so Databricks picks the URL"
        )
        return
    full = f"{section.deployment_name_prefix}-{name}"
    ours = ctx.project.databricks_tags["Workspace"]
    clashes = [w for w in _account(ctx).workspaces.list() if w.deployment_name == full]
    if not clashes:
        yield LIVE_010.passed(deployment_name=full)
        return
    if any(w.workspace_name == ours for w in clashes):
        yield LIVE_010.skipped(f"the workspace {ours} already exists; nothing to check")
        return
    yield LIVE_010.failed(
        f"the deployment name {full} belongs to another workspace: set `deployment_name` on the "
        "databricks-workspace component to a different name",
        deployment_name=full,
    )
