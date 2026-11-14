"""Databricks `after` checks: verification once both stacks have applied.

Every check SKIPs until `databricks.enabled` is true, like the live checks.
No failure message carries the client secret or an SDK exception's text.
Registered at import; `checks.load_builtin_checks()` imports this module.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import CatalogIsolationMode, ValidationResultResult

from ..databricks_names import REGION, derived_names
from ._results import Meta
from .databricks_between import outputs, workspace_client
from .databricks_live import _live, _session
from .framework import CheckContext, CheckResult, Severity, Stage

AFT_001 = Meta("DBX-AFT-001", "The storage credential validates against the bucket", "catalog")
AFT_002 = Meta("DBX-AFT-002", "The catalog is isolated and bound to this workspace", "catalog")
AFT_003 = Meta(
    "DBX-AFT-003", "Each compute policy exists with the project's fixed tags", "compute-policies"
)
AFT_004 = Meta(
    "DBX-AFT-004",
    "Private egress goes through the S3 endpoint and the NAT instance",
    "egress",
    Severity.WARNING,
)


def _storage_root(ctx: CheckContext) -> str:
    return f"s3://{derived_names(ctx.project).catalog_bucket}/managed/"


def _catalog_name(ctx: CheckContext) -> str:
    return ctx.project.project.replace("-", "_")


@_live(AFT_001, needs_ids=True, stage=Stage.AFTER)
def credential_validates(ctx: CheckContext) -> Iterable[CheckResult]:
    names = derived_names(ctx.project)
    url = _storage_root(ctx)
    response = workspace_client(ctx).storage_credentials.validate(
        storage_credential_name=names.uc_role, url=url
    )
    results = response.results or []
    failed = [
        f"{r.operation.value if r.operation else 'operation'}: {r.message or 'failed'}"
        for r in results
        if r.result is ValidationResultResult.FAIL
    ]
    if failed or not results:
        yield AFT_001.failed(
            f"the storage credential {names.uc_role} cannot use {url} "
            f"({'; '.join(failed) or 'validation returned no results'}): check the Unity "
            "Catalog role's trust and policy and the catalog bucket's policy, then re-run apply "
            "(IAM can take a minute to propagate)",
            credential=names.uc_role,
            url=url,
        )
        return
    yield AFT_001.passed(credential=names.uc_role, url=url)


@_live(AFT_002, needs_ids=True, stage=Stage.AFTER)
def catalog_isolated_and_bound(ctx: CheckContext) -> Iterable[CheckResult]:
    workspace_id = int(outputs(ctx)["workspace_id"])
    client = workspace_client(ctx)
    name, root = _catalog_name(ctx), _storage_root(ctx)
    try:
        catalog = client.catalogs.get(name)
    except NotFound:
        yield AFT_002.failed(
            f"the catalog {name} does not exist: the workspace stack creates it, so re-run apply",
            catalog=name,
        )
        return
    problems = []
    if (catalog.storage_root or "").rstrip("/") != root.rstrip("/"):
        problems.append(f"storage_root is {catalog.storage_root}, expected {root}")
    if catalog.isolation_mode is not CatalogIsolationMode.ISOLATED:
        mode = catalog.isolation_mode.value if catalog.isolation_mode else "unset"
        problems.append(f"isolation_mode is {mode}, expected ISOLATED")
    bound = {b.workspace_id for b in client.workspace_bindings.get_bindings("catalog", name)}
    if bound != {workspace_id}:
        problems.append(
            f"bound to workspaces {sorted(bound) or 'none'}, expected only {workspace_id}"
        )
    if problems:
        yield AFT_002.failed(
            f"the catalog {name} differs from the design ({'; '.join(problems)}): re-run apply "
            "to restore it; never change these in the console",
            catalog=name,
        )
        return
    yield AFT_002.passed(catalog=name, storage_root=root, workspace_id=str(workspace_id))


@_live(AFT_003, needs_ids=True, stage=Stage.AFTER)
def policies_installed(ctx: CheckContext) -> Iterable[CheckResult]:
    client = workspace_client(ctx)
    expected = ctx.project.databricks_tags
    by_name = {p.name: p for p in client.cluster_policies.list()}
    problems = []
    for name in ctx.project.databricks.compute_policies:
        listed = by_name.get(name)
        if listed is None or listed.policy_id is None:
            problems.append(f"{name} is missing")
            continue
        # The list response may omit the definition; read the policy in full.
        definition = json.loads(client.cluster_policies.get(listed.policy_id).definition or "{}")
        wrong = [
            key
            for key, value in expected.items()
            if definition.get(f"custom_tags.{key}", {}).get("value") != value
        ]
        if wrong:
            problems.append(f"{name} has the wrong fixed tag(s) {', '.join(wrong)}")
    if problems:
        yield AFT_003.failed(
            f"{'; '.join(problems)}: re-run apply, which reinstalls the policies with the "
            "project's tags (databricks_tags: Project, Environment, CostCenter, Owner, "
            "ManagedBy, Workspace)",
            problems="; ".join(problems),
        )
        return
    yield AFT_003.passed(policies=", ".join(ctx.project.databricks.compute_policies))


@_live(AFT_004, stage=Stage.AFTER)
def egress_routes(ctx: CheckContext) -> Iterable[CheckResult]:
    ec2 = _session(ctx).client("ec2", region_name=REGION)
    base = f"{ctx.project.databricks_tags['Organization']}-{ctx.project.project}"
    tables = ec2.describe_route_tables(
        Filters=[{"Name": "tag:Name", "Values": [f"{base}-private"]}]
    )["RouteTables"]
    if len(tables) != 1:
        yield AFT_004.failed(
            f"expected one private route table named {base}-private, found {len(tables)}: "
            "the account stack creates it; re-run apply",
            found=str(len(tables)),
        )
        return
    table = tables[0]
    vpc_id, table_id = table["VpcId"], table["RouteTableId"]
    problems = []
    endpoints = ec2.describe_vpc_endpoints(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "service-name", "Values": [f"com.amazonaws.{REGION}.s3"]},
        ]
    )["VpcEndpoints"]
    if not any(table_id in e.get("RouteTableIds", []) for e in endpoints):
        problems.append("the S3 gateway endpoint is not on the private route table")
    instances = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name", "Values": [f"{base}-nat"]},
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )["Reservations"]
    nat_enis = {
        eni["NetworkInterfaceId"]
        for r in instances
        for i in r["Instances"]
        for eni in i.get("NetworkInterfaces", [])
    }
    default = next(
        (r for r in table.get("Routes", []) if r.get("DestinationCidrBlock") == "0.0.0.0/0"),
        None,
    )
    if default is None:
        problems.append("the private route table has no default route")
    elif str(default.get("GatewayId", "")).startswith("igw-"):
        problems.append("the private default route goes to an internet gateway")
    elif default.get("NetworkInterfaceId") not in nat_enis:
        problems.append("the private default route does not target the running NAT instance")
    if problems:
        yield AFT_004.failed(
            f"{'; '.join(problems)}: private clusters would lose, or bypass, their egress "
            "design. Re-run apply; if the NAT instance was replaced, apply re-points the route",
            problems="; ".join(problems),
        )
        return
    yield AFT_004.passed(route_table=table_id, vpc=vpc_id)
