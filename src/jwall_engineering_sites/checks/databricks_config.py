"""Databricks `config` checks: offline, run during `validate` and in `doctor`.

Each failure names the config.yaml field (or project-file field) to change.
Registered at import; `checks.load_builtin_checks()` imports this module.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from typing import Any

from ..databricks_names import (
    IAM_ROLE_MAX,
    S3_BUCKET_MAX,
    ZONES,
    derived_names,
)
from ..errors import ConfigError
from ..project import DATABRICKS_RESERVED_TAGS
from ..settings import BOT_VPC_CIDR, DatabricksSection
from ..stack import deployment_name
from ._results import Meta
from .framework import CheckContext, CheckResult, Stage, check

_DBX = frozenset({"databricks"})
#: Prefix lengths the private subnets may have (AWS allows /16 to /28; Databricks wants room).
SUBNET_MIN_PREFIX = 17
SUBNET_MAX_PREFIX = 26
#: Tags the workspace and its clusters must carry.
REQUIRED_TAGS = ("Project", "Environment", "CostCenter", "Owner", "ManagedBy", "Workspace")

_CIDR_FIELD = "projects.{p}.databricks.vpc_cidr"

CFG_001 = Meta(
    "DBX-CFG-001", "Derived private subnets fit and avoid the public ones", "network-cidrs"
)
CFG_002 = Meta(
    "DBX-CFG-002", "VPC CIDR overlaps no other Databricks project or the bot VPC", "network-cidrs"
)
CFG_003 = Meta("DBX-CFG-003", "Compute policies exist and are safe", "compute-policies")
CFG_004 = Meta("DBX-CFG-004", "Derived resource names fit their limits", "resource-names")
CFG_005 = Meta(
    "DBX-CFG-005", "Tags avoid reserved keys and include the required ones", "required-tags"
)


def _section(ctx: CheckContext) -> DatabricksSection:
    if ctx.settings.databricks is None:
        raise ConfigError("config.yaml has no databricks section")
    return ctx.settings.databricks


def _vpc(ctx: CheckContext) -> tuple[str, ipaddress.IPv4Network]:
    field = _CIDR_FIELD.format(p=ctx.project.project)
    approved = ctx.project.databricks_approved
    if approved is None:
        raise ConfigError(f"add {field} to config.yaml")
    return field, ipaddress.IPv4Network(approved.vpc_cidr)


def _subnet(network: ipaddress.IPv4Network, new_bits: int, index: int) -> ipaddress.IPv4Network:
    """Terraform's `cidrsubnet(network, new_bits, index)`."""
    prefix = network.prefixlen + new_bits
    size = 2 ** (32 - prefix)
    return ipaddress.IPv4Network((int(network.network_address) + index * size, prefix))


@check(CFG_001.check_id, CFG_001.title, Stage.CONFIG, _DBX, runbook=CFG_001.runbook)
def private_subnets(ctx: CheckContext) -> Iterable[CheckResult]:
    field, vpc = _vpc(ctx)
    private = [_subnet(vpc, 4, 8), _subnet(vpc, 4, 9)]
    # The vpc module's public subnets: consecutive /24s from the start of the range, one per zone.
    public = [
        ipaddress.IPv4Network((int(vpc.network_address) + i * 256, 24)) for i in range(len(ZONES))
    ]
    problems = []
    for net in private:
        if not SUBNET_MIN_PREFIX <= net.prefixlen <= SUBNET_MAX_PREFIX:
            problems.append(
                f"private subnet {net} is /{net.prefixlen}; it must be between "
                f"/{SUBNET_MIN_PREFIX} and /{SUBNET_MAX_PREFIX}"
            )
        clash = [str(p) for p in public if net.overlaps(p)]
        if clash:
            problems.append(
                f"private subnet {net} overlaps the public subnet(s) {', '.join(clash)}"
            )
    facts = {"private": ", ".join(map(str, private)), "public": ", ".join(map(str, public))}
    if problems:
        yield CFG_001.failed(
            f"change {field} in config.yaml to a /16 to /20 range: " + "; ".join(problems),
            problem="; ".join(problems),
            **facts,
        )
        return
    yield CFG_001.passed(**facts)


@check(CFG_002.check_id, CFG_002.title, Stage.CONFIG, _DBX, runbook=CFG_002.runbook)
def vpc_overlap(ctx: CheckContext) -> Iterable[CheckResult]:
    field, vpc = _vpc(ctx)
    others: list[tuple[str, ipaddress.IPv4Network]] = [
        ("the bot VPC default", ipaddress.IPv4Network(BOT_VPC_CIDR))
    ]
    for name, guardrails in sorted(ctx.settings.projects.items()):
        if name != ctx.project.project and guardrails.databricks is not None:
            others.append(
                (f"project {name}", ipaddress.IPv4Network(guardrails.databricks.vpc_cidr))
            )
    clashes = [f"{label} ({net})" for label, net in others if vpc.overlaps(net)]
    if clashes:
        yield CFG_002.failed(
            f"change {field} in config.yaml ({vpc}); it overlaps {', '.join(clashes)}",
            overlaps=", ".join(clashes),
        )
        return
    yield CFG_002.passed(vpc_cidr=str(vpc))


def _node_types(rule: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """(the node types a rule permits, defaults outside that set)."""
    kind = rule.get("type")
    if kind == "allowlist":
        allowed = [str(v) for v in rule.get("values", [])]
    elif kind == "fixed":
        allowed = [str(rule.get("value"))]
    else:
        allowed = []
    default = rule.get("defaultValue")
    stray = [str(default)] if default is not None and str(default) not in allowed else []
    return allowed, stray


def _policy_problems(definition: Mapping[str, Any]) -> list[str]:
    problems = []
    if "autotermination_minutes" not in definition:
        problems.append("set definition.autotermination_minutes (every policy needs it)")
    tags = sorted(k for k in definition if k.startswith("custom_tags."))
    if tags:
        problems.append(f"remove {', '.join(tags)} (tags come from the project)")
    for key in ("node_type_id", "driver_node_type_id"):
        rule = definition.get(key)
        if rule is None:
            if key == "node_type_id":
                problems.append("set node_type_id to an allowlist of node types")
            continue
        allowed, stray = _node_types(rule)
        if not allowed:
            problems.append(f"{key} must be an allowlist (or fixed) with at least one node type")
        for node in stray:
            problems.append(f"{key} default {node} is not in its allowlist")
    return problems


@check(CFG_003.check_id, CFG_003.title, Stage.CONFIG, _DBX, runbook=CFG_003.runbook)
def compute_policies(ctx: CheckContext) -> Iterable[CheckResult]:
    central = _section(ctx).compute_policies
    problems: list[str] = []
    for name in ctx.project.databricks.compute_policies:
        policy = central.get(name)
        if policy is None:
            problems.append(
                f"policy {name!r} is not defined: add it under databricks.compute_policies"
            )
            continue
        for issue in _policy_problems(policy.definition):
            problems.append(f"databricks.compute_policies.{name}: {issue}")
    if problems:
        yield CFG_003.failed("in config.yaml, " + "; ".join(problems), problems="; ".join(problems))
        return
    yield CFG_003.passed(policies=", ".join(ctx.project.databricks.compute_policies))


@check(CFG_004.check_id, CFG_004.title, Stage.CONFIG, _DBX, runbook=CFG_004.runbook)
def derived_name_limits(ctx: CheckContext) -> Iterable[CheckResult]:
    names = derived_names(ctx.project)
    too_long = [
        f"{label} {name} ({len(name)} > {limit})"
        for group, limit in ((names.buckets, S3_BUCKET_MAX), (names.roles, IAM_ROLE_MAX))
        for label, name in group.items()
        if len(name) > limit
    ]
    problems = list(too_long)
    section = _section(ctx)
    try:
        deployment_name(
            ctx.project.project,
            section.deployment_name_prefix,
            ctx.project.databricks.deployment_name,
        )
    except ConfigError as exc:
        problems.append(str(exc))
    if problems:
        hint = (
            "shorten the project name (the file's `project`)"
            if too_long
            else "set a shorter deployment_name on the databricks-workspace component or "
            "databricks.deployment_name_prefix in config.yaml"
        )
        yield CFG_004.failed(
            f"{hint}: " + "; ".join(problems),
            too_long=", ".join(too_long),
        )
        return
    yield CFG_004.passed(root_bucket=names.root_bucket, catalog_bucket=names.catalog_bucket)


@check(CFG_005.check_id, CFG_005.title, Stage.CONFIG, _DBX, runbook=CFG_005.runbook)
def tags(ctx: CheckContext) -> Iterable[CheckResult]:
    stated = ctx.project.config.tags
    reserved = sorted(set(stated) & set(DATABRICKS_RESERVED_TAGS))
    effective = ctx.project.databricks_tags
    missing = [k for k in REQUIRED_TAGS if not effective.get(k)]
    problems = []
    if reserved:
        problems.append(f"remove {', '.join(reserved)} from tags (Databricks reserves them)")
    if missing:
        problems.append(f"add {', '.join(missing)} to tags")
    if problems:
        yield CFG_005.failed("in the project file, " + "; ".join(problems))
        return
    yield CFG_005.passed(tags=", ".join(sorted(effective)))
