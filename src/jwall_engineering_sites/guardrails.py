"""Check a project file against the central `projects` section of config.yaml.

A consumer's file asks for instance types, profiles, limits and dataset
retention; `config.yaml` (reviewed in this repository) says what that project
is approved for. The file cannot widen any of it. A violation is a problem
string; `Project.from_config` raises them all as one `ConfigError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from itertools import combinations

from .components import Dataset, TaskProfile
from .settings import DatasetRetention, ProjectGuardrails, Settings, TaskLimits
from .site import SiteConfig


def _overlap(a: str, b: str) -> bool:
    """Two prefixes overlap when either one starts with the other."""
    return a.startswith(b) or b.startswith(a)


def _rule_id(prefix: str) -> str:
    """The lifecycle rule the dataset-bucket module names after an expiring prefix."""
    return f"expire-{prefix.replace('/', '-')}"


def check(config: SiteConfig, settings: Settings) -> list[str]:
    """Every way `config` asks for more than `settings` approves; empty if none."""
    project = config.project
    approved = settings.projects.get(project)
    if approved is None:
        return [
            f"project {project!r} has no entry under projects in config.yaml; add its "
            "instance types, task profiles and datasets there (reviewed in "
            "jwall-engineering-infra)"
        ]
    problems: list[str] = []
    runner = config.task_runner
    if runner is not None:
        limits = settings.tasks.limits
        for name in runner.profile_names:
            profile = runner.profile(name)
            if name not in approved.task_profiles:
                problems.append(
                    f"task-runner profile {name!r} is not in projects.{project}.task_profiles "
                    "in config.yaml"
                )
            problems += _profile_problems(name, profile, approved, limits, project)
    problems += _dataset_problems(config, approved)
    return problems


def _profile_problems(
    name: str,
    profile: TaskProfile,
    approved: ProjectGuardrails,
    limits: TaskLimits,
    project: str,
) -> list[str]:
    problems: list[str] = []
    for instance_type in profile.instance_types:
        if instance_type not in approved.instance_types:
            problems.append(
                f"task-runner profile {name}: instance type {instance_type!r} is not in "
                f"projects.{project}.instance_types in config.yaml; add it there, or if it "
                "is already listed, bump the tool pin (the consumer's pin decides which "
                "config.yaml applies)"
            )
    for field in ("max_hours", "max_vcpus", "root_volume_gb"):
        value: int = getattr(profile, field)
        ceiling: int = getattr(limits, field)
        if value > ceiling:
            problems.append(
                f"task-runner profile {name}: {field} {value} is above tasks.limits.{field} "
                f"({ceiling}) in config.yaml"
            )
    return problems


def _dataset_problems(config: SiteConfig, approved: ProjectGuardrails) -> list[str]:
    problems: list[str] = []
    protected_by_name: dict[str, tuple[str, ...]] = {}
    for dataset in config.datasets:
        retention = approved.datasets.get(dataset.bucket)
        if retention is None:
            problems.append(
                f"dataset {dataset.name}: bucket {dataset.bucket!r} is not in "
                f"projects.{config.project}.datasets in config.yaml"
            )
            continue
        protected_by_name[dataset.name] = retention.write_once
        problems += _retention_problems(dataset, retention)

    runner = config.task_runner
    for profile_name in runner.profile_names if runner else ():
        assert runner is not None
        for dataset_name, access in runner.profile(profile_name).datasets.items():
            protected = protected_by_name.get(dataset_name, ())
            who = f"task-runner profile {profile_name}: dataset {dataset_name}"
            for prefix in access.write:
                for guarded in protected:
                    if _overlap(prefix, guarded):
                        problems.append(
                            f"{who}: write {prefix!r} overlaps write_once {guarded!r} in "
                            "config.yaml; use write_once for protected prefixes"
                        )
            for prefix in access.write_once:
                if not any(prefix.startswith(guarded) for guarded in protected):
                    problems.append(
                        f"{who}: write_once {prefix!r} is not under a write_once prefix "
                        "approved in config.yaml"
                    )
                if not any(prefix.startswith(granted) for granted in access.read):
                    problems.append(
                        f"{who}: write_once {prefix!r} is not covered by its read grants; add "
                        "it to read, because task-artifact put reads the object back and "
                        "would fail after uploading"
                    )
    return problems


def _retention_problems(dataset: Dataset, retention: DatasetRetention) -> list[str]:
    where = f"dataset {dataset.name} ({dataset.bucket})"
    problems: list[str] = []
    if retention.prefix is not None and dataset.prefix != retention.prefix:
        problems.append(
            f"{where}: prefix {dataset.prefix!r} must equal the {retention.prefix!r} approved "
            "in config.yaml, which its write_once and expire_prefixes are relative to"
        )
    if dataset.expire_prefixes != retention.expire_prefixes:
        problems.append(
            f"{where}: expire_prefixes {dataset.expire_prefixes} must equal the "
            f"{retention.expire_prefixes} approved in config.yaml"
        )
    if dataset.expire_noncurrent_days != retention.expire_noncurrent_days:
        problems.append(
            f"{where}: expire_noncurrent_days {dataset.expire_noncurrent_days} must equal "
            f"the {retention.expire_noncurrent_days} approved in config.yaml"
        )
    if dataset.versioning != retention.versioning:
        problems.append(
            f"{where}: versioning {dataset.versioning} must equal the "
            f"{retention.versioning} approved in config.yaml"
        )
    if retention.write_once and "task-runner" in dataset.writers:
        problems.append(
            f"{where}: writers: [task-runner] grants PutObject on the whole prefix, which "
            f"config.yaml protects with write_once {list(retention.write_once)}; use "
            "write_once in the profiles' dataset access instead"
        )
    for expiring in retention.expire_prefixes:
        for guarded in retention.write_once:
            if _overlap(expiring, guarded):
                problems.append(
                    f"{where}: expire_prefixes {expiring!r} overlaps write_once {guarded!r} "
                    "in config.yaml"
                )
    return problems + _expiry_rule_problems(where, retention.expire_prefixes)


def _expiry_rule_problems(where: str, expire_prefixes: Mapping[str, int]) -> list[str]:
    """What the dataset-bucket module refuses at plan, caught here at load."""
    problems: list[str] = []
    for one, other in combinations(sorted(expire_prefixes), 2):
        if _overlap(one, other):
            problems.append(
                f"{where}: expire_prefixes {one!r} and {other!r} overlap in config.yaml; S3 "
                "would apply the shorter expiry to the nested prefix too"
            )
        elif _rule_id(one) == _rule_id(other):
            problems.append(
                f"{where}: expire_prefixes {one!r} and {other!r} in config.yaml both give the "
                f"lifecycle rule id {_rule_id(one)!r}"
            )
    return problems
