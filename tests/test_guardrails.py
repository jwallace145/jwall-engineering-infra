"""Project files are checked against the central `projects` section of config.yaml."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from jwall_engineering_sites import guardrails
from jwall_engineering_sites.components import DatasetAccess
from jwall_engineering_sites.errors import ConfigError
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import (
    DatasetRetention,
    ProjectGuardrails,
    Settings,
    TaskLimits,
)
from jwall_engineering_sites.site import SiteConfig, load_site_config
from tests.conftest import FIXTURES, with_guardrails

BOT_FILE = FIXTURES / "bots" / "rusty-chess.yaml"
BUCKET = "jwall-engineering-rusty-chess-nnue"


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(BOT_FILE.read_text())
    return copy.deepcopy(raw)


def _component(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    component: dict[str, Any] = next(c for c in raw["components"] if c["kind"] == kind)
    return component


def _runner(raw: dict[str, Any]) -> dict[str, Any]:
    return _component(raw, "task-runner")


def _config(raw: dict[str, Any]) -> SiteConfig:
    return SiteConfig.model_validate(raw)


def _central(settings: Settings, **retention: Any) -> Settings:
    """The fixture's central entry with the dataset's retention replaced."""
    if retention.get("write_once") or retention.get("expire_prefixes"):
        retention.setdefault("prefix", "data/")
    if retention.get("expire_noncurrent_days") is not None:
        retention.setdefault("prefix", "data/")
    entry = settings.projects["rusty-chess"]
    return with_guardrails(
        settings,
        "rusty-chess",
        entry.model_copy(update={"datasets": {BUCKET: DatasetRetention(**retention)}}),
    )


def _problems(raw: dict[str, Any], settings: Settings) -> list[str]:
    return guardrails.check(_config(raw), settings)


def test_the_rusty_chess_fixture_is_within_its_guardrails(settings: Settings) -> None:
    assert _problems(_raw(), settings) == []


def test_project_without_an_entry_is_refused(settings: Settings) -> None:
    bare = settings.model_copy(update={"projects": {}})
    (problem,) = _problems(_raw(), bare)
    assert "rusty-chess" in problem and "config.yaml" in problem


def test_a_missing_entry_fails_at_load(settings: Settings) -> None:
    bare = settings.model_copy(update={"projects": {}})
    with pytest.raises(ConfigError, match=r"config\.yaml"):
        Project.from_config(load_site_config(BOT_FILE), bare)


def test_a_violation_fails_at_load(settings: Settings) -> None:
    narrow = with_guardrails(settings, "rusty-chess", ProjectGuardrails())
    with pytest.raises(ConfigError, match=r"c8a\.xlarge.*config\.yaml"):
        Project.from_config(load_site_config(BOT_FILE), narrow)


def test_other_project_names_need_their_own_entry(settings: Settings) -> None:
    raw = _raw()
    raw["project"] = "other"
    raw["tags"]["Project"] = "other"
    dataset = _component(raw, "dataset")
    dataset["bucket"] = "jwall-engineering-other-nnue"
    other = settings.projects["rusty-chess"].model_copy(
        update={"datasets": {"jwall-engineering-other-nnue": DatasetRetention()}}
    )
    assert _problems(raw, settings) != []  # no entry for "other" yet
    assert _problems(raw, with_guardrails(settings, "other", other)) == []


def _assert_unlisted_type(problems: list[str], itype: str) -> None:
    hits = [p for p in problems if itype in p]
    assert hits, problems
    for hit in hits:
        assert "config.yaml" in hit and "bump the tool pin" in hit


def test_cpu_instance_type_outside_the_list_is_refused(settings: Settings) -> None:
    raw = _raw()
    _runner(raw)["instance_types"] = ["c8a.xlarge", "c8a.16xlarge", "c8a.4xlarge"]
    problems = _problems(raw, settings)
    _assert_unlisted_type(problems, "c8a.4xlarge")
    assert not any("c8a.xlarge" in p for p in problems)


def test_a_cpu_default_outside_the_list_is_refused(settings: Settings) -> None:
    raw = _raw()
    narrow = settings.projects["rusty-chess"].model_copy(update={"instance_types": ("c8a.xlarge",)})
    problems = _problems(raw, with_guardrails(settings, "rusty-chess", narrow))
    _assert_unlisted_type(problems, "c8a.16xlarge")


def test_profile_instance_type_outside_the_list_is_refused(settings: Settings) -> None:
    raw = _raw()
    gpu = _runner(raw)["profiles"]["gpu"]
    gpu["instance_types"] = ["g6.xlarge", "g6e.12xlarge"]
    problems = _problems(raw, settings)
    _assert_unlisted_type(problems, "g6e.12xlarge")
    assert any("gpu" in p for p in problems)


def test_an_unlisted_profile_name_is_refused(settings: Settings) -> None:
    raw = _raw()
    _runner(raw)["profiles"]["sneaky"] = copy.deepcopy(_runner(raw)["profiles"]["match"])
    (problem,) = _problems(raw, settings)
    assert "sneaky" in problem and "task_profiles" in problem and "config.yaml" in problem


def test_the_cpu_profile_must_be_listed_too(settings: Settings) -> None:
    entry = settings.projects["rusty-chess"]
    no_cpu = entry.model_copy(update={"task_profiles": ("gpu",)})
    problems = _problems(_raw(), with_guardrails(settings, "rusty-chess", no_cpu))
    assert any("cpu" in p and "task_profiles" in p for p in problems)


def test_an_unlisted_dataset_bucket_is_refused(settings: Settings) -> None:
    entry = settings.projects["rusty-chess"].model_copy(update={"datasets": {}})
    (problem,) = _problems(_raw(), with_guardrails(settings, "rusty-chess", entry))
    assert BUCKET in problem and "config.yaml" in problem


@pytest.mark.parametrize(
    ("field", "value", "limit"),
    [("max_hours", 7, 6), ("max_vcpus", 257, 256)],
)
def test_cpu_limits_above_the_central_ceiling_are_refused(
    settings: Settings, field: str, value: int, limit: int
) -> None:
    raw = _raw()
    _runner(raw)[field] = value
    (problem,) = _problems(raw, settings)
    assert field in problem and f"{value}" in problem and f"{limit}" in problem
    assert "cpu" in problem


@pytest.mark.parametrize(
    ("field", "value", "limit"),
    [("max_hours", 7, 6), ("max_vcpus", 257, 256)],
)
def test_profile_limits_above_the_central_ceiling_are_refused(
    settings: Settings, field: str, value: int, limit: int
) -> None:
    raw = _raw()
    _runner(raw)["profiles"]["match"][field] = value
    (problem,) = _problems(raw, settings)
    assert field in problem and f"{value}" in problem and f"{limit}" in problem
    assert "match" in problem


def test_limits_at_the_ceiling_pass(settings: Settings) -> None:
    raw = _raw()
    _runner(raw)["max_hours"] = 6
    _runner(raw)["max_vcpus"] = 256
    assert _problems(raw, settings) == []


def _lower_limits(settings: Settings, **limits: int) -> Settings:
    new = settings.tasks.limits.model_copy(update=limits)
    return settings.model_copy(update={"tasks": settings.tasks.model_copy(update={"limits": new})})


def test_root_volume_above_the_ceiling_is_refused_on_cpu_and_profile(settings: Settings) -> None:
    # The gpu profile asks for 200 GB, the cpu profile for the default 40 GB.
    both = _problems(_raw(), _lower_limits(settings, root_volume_gb=30))
    assert any("cpu" in p and "root_volume_gb" in p and "40" in p for p in both)
    assert any("gpu" in p and "root_volume_gb" in p and "200" in p for p in both)
    gpu_only = _problems(_raw(), _lower_limits(settings, root_volume_gb=100))
    assert len(gpu_only) == 1 and "gpu" in gpu_only[0]


def test_the_ceiling_comes_from_settings(settings: Settings) -> None:
    assert settings.tasks.limits == TaskLimits(max_hours=6, max_vcpus=256, root_volume_gb=200)
    raised = _lower_limits(settings, max_hours=24, max_vcpus=1024)
    raw = _raw()
    _runner(raw)["max_hours"] = 24
    _runner(raw)["max_vcpus"] = 1024
    assert _problems(raw, raised) == []


# Retention


def test_expire_prefixes_must_equal_the_central_ones(settings: Settings) -> None:
    raw = _raw()
    _component(raw, "dataset")["expire_prefixes"] = {"tmp/": 7}
    (problem,) = _problems(raw, settings)  # central has none
    assert "expire_prefixes" in problem and "config.yaml" in problem and BUCKET in problem
    approved = _central(settings, expire_prefixes={"tmp/": 7})
    assert _problems(raw, approved) == []
    assert _problems(raw, _central(settings, expire_prefixes={"tmp/": 8})) != []


def test_central_expire_prefixes_the_file_leaves_out_are_refused(settings: Settings) -> None:
    (problem,) = _problems(_raw(), _central(settings, expire_prefixes={"tmp/": 7}))
    assert "expire_prefixes" in problem


def test_expire_noncurrent_days_must_equal_the_central_value(settings: Settings) -> None:
    raw = _raw()
    dataset = _component(raw, "dataset")
    dataset.update(versioning=True, expire_noncurrent_days=30)
    (problem,) = _problems(raw, _central(settings, versioning=True))
    assert "expire_noncurrent_days" in problem and "config.yaml" in problem
    assert _problems(raw, _central(settings, versioning=True, expire_noncurrent_days=30)) == []
    assert _problems(raw, _central(settings, versioning=True, expire_noncurrent_days=31)) != []


def test_versioning_cannot_be_turned_on_by_the_file(settings: Settings) -> None:
    raw = _raw()
    _component(raw, "dataset")["versioning"] = True
    (problem,) = _problems(raw, settings)  # central default is off
    assert "versioning" in problem and "config.yaml" in problem


def test_versioning_cannot_be_turned_off_by_the_file(settings: Settings) -> None:
    (problem,) = _problems(_raw(), _central(settings, versioning=True))
    assert "versioning" in problem and "config.yaml" in problem


def test_versioning_matching_central_passes(settings: Settings) -> None:
    raw = _raw()
    _component(raw, "dataset")["versioning"] = True
    assert _problems(raw, _central(settings, versioning=True)) == []


def _protect(settings: Settings, *prefixes: str, **retention: Any) -> Settings:
    retention.setdefault("prefix", "data/")
    return _central(settings, write_once=prefixes, **retention)


def _no_task_runner_writer(raw: dict[str, Any]) -> None:
    _component(raw, "dataset")["writers"] = []


def test_a_profile_cannot_write_a_protected_prefix(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write": ["runs/"]}
    (problem,) = _problems(raw, _protect(settings, "runs/"))
    assert "datagen" in problem and "runs/" in problem and "write_once" in problem


def test_a_profile_cannot_write_below_a_protected_prefix(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write": ["runs/x/"]}
    (problem,) = _problems(raw, _protect(settings, "runs/"))
    assert "runs/x/" in problem


def test_non_overlapping_prefixes_pass(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write": ["r/"]}
    assert _problems(raw, _protect(settings, "runs/")) == []
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write": ["run/"]}
    assert _problems(raw, _protect(settings, "runs/")) == []
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write": ["ru/"]}
    assert _problems(raw, _protect(settings, "runs/")) == []


def test_a_profile_may_write_a_protected_prefix_through_write_once(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    access = {"write_once": ["runs/"], "write": ["selfplay/"]}
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = access
    assert _problems(raw, _protect(settings, "runs/")) == []


def test_write_once_below_a_central_prefix_is_covered(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write_once": ["runs/x/"]}
    assert _problems(raw, _protect(settings, "runs/")) == []


def test_write_once_outside_the_central_list_is_refused(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write_once": ["other/"]}
    (problem,) = _problems(raw, _protect(settings, "runs/"))
    assert "other/" in problem and "write_once" in problem and "config.yaml" in problem
    (problem,) = _problems(raw, settings)  # no central protection at all
    assert "other/" in problem


def test_task_runner_writers_are_refused_on_a_protected_dataset(settings: Settings) -> None:
    (problem,) = _problems(_raw(), _protect(settings, "runs/"))
    assert "writers" in problem and "task-runner" in problem and BUCKET in problem


def test_task_runner_writers_pass_when_nothing_is_protected(settings: Settings) -> None:
    assert _component(_raw(), "dataset")["writers"] == ["task-runner"]
    assert _problems(_raw(), settings) == []


def test_central_expiry_overlapping_a_protected_prefix_is_refused(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _component(raw, "dataset")["expire_prefixes"] = {"runs/old/": 1}
    central = _protect(settings, "runs/", expire_prefixes={"runs/old/": 1})
    (problem,) = _problems(raw, central)
    assert "runs/old/" in problem and "runs/" in problem and "write_once" in problem


def test_expiry_beside_a_protected_prefix_passes(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _component(raw, "dataset")["expire_prefixes"] = {"tmp/": 1}
    central = _protect(settings, "runs/", expire_prefixes={"tmp/": 1})
    assert _problems(raw, central) == []


# Component rules that need no config.yaml


def test_expire_noncurrent_days_needs_versioning() -> None:
    raw = _raw()
    _component(raw, "dataset")["expire_noncurrent_days"] = 30
    with pytest.raises(ValidationError, match="versioning"):
        _config(raw)
    _component(raw, "dataset")["versioning"] = True
    assert _component_value(_config(raw), "expire_noncurrent_days") == 30


def _component_value(config: SiteConfig, field: str) -> Any:
    return getattr(config.datasets[0], field)


def test_dataset_defaults_have_no_retention() -> None:
    dataset = _config(_raw()).datasets[0]
    assert (dataset.versioning, dataset.expire_noncurrent_days, dataset.expire_prefixes) == (
        False,
        None,
        {},
    )


def test_write_once_alone_is_valid_access() -> None:
    assert DatasetAccess(write_once=("runs/",)).write_once == ("runs/",)
    with pytest.raises(ValidationError):
        DatasetAccess()


def test_write_once_takes_sub_prefixes() -> None:
    with pytest.raises(ValidationError):
        DatasetAccess(write_once=("/runs",))


@pytest.mark.parametrize("field", ["max_hours", "max_vcpus"])
def test_components_accept_values_the_central_ceiling_decides(
    settings: Settings, field: str
) -> None:
    raw = _raw()
    _runner(raw)[field] = {"max_hours": 24, "max_vcpus": 1024}[field]
    _runner(raw)["profiles"]["match"][field] = {"max_hours": 24, "max_vcpus": 1024}[field]
    _config(raw)  # schema accepts; guardrails.check refuses against the fixture ceiling
    assert len(_problems(raw, settings)) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_hours", 25), ("max_hours", 0), ("max_vcpus", 1025), ("max_vcpus", 0)],
)
def test_components_still_bound_the_schema(field: str, value: int) -> None:
    raw = _raw()
    _runner(raw)[field] = value
    with pytest.raises(ValidationError, match=field):
        _config(raw)
    raw = _raw()
    _runner(raw)["profiles"]["match"][field] = value
    with pytest.raises(ValidationError, match=field):
        _config(raw)


def test_root_volume_keeps_its_schema_bound() -> None:
    raw = _raw()
    _runner(raw)["profiles"]["gpu"]["root_volume_gb"] = 201
    with pytest.raises(ValidationError, match="root_volume_gb"):
        _config(raw)


# A parent of a protected prefix overlaps it too


def test_a_profile_cannot_write_a_parent_of_a_protected_prefix(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write": ["runs/"]}
    (problem,) = _problems(raw, _protect(settings, "runs/x/"))
    assert "runs/" in problem and "runs/x/" in problem


def test_expiry_on_a_parent_of_a_protected_prefix_is_refused(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _component(raw, "dataset")["expire_prefixes"] = {"runs/": 1}
    central = _protect(settings, "runs/x/", expire_prefixes={"runs/": 1})
    (problem,) = _problems(raw, central)
    assert "expire_prefixes" in problem and "runs/x/" in problem


def test_consumer_write_once_on_a_parent_is_not_covered(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write_once": ["runs/"]}
    (problem,) = _problems(raw, _protect(settings, "runs/x/"))
    assert "write_once" in problem and "'runs/'" in problem


# The dataset's prefix is centrally approved when retention depends on it


def test_a_deeper_prefix_cannot_dodge_write_once(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    dataset = _component(raw, "dataset")
    dataset["prefix"] = "data/runs/"
    _runner(raw)["profiles"]["datagen"]["datasets"]["nnue"] = {"write": ["x/"]}
    (problem,) = _problems(raw, _protect(settings, "runs/"))
    assert "prefix" in problem and "data/runs/" in problem and "data/" in problem
    assert "config.yaml" in problem


def test_a_deeper_prefix_cannot_move_expiry(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    dataset = _component(raw, "dataset")
    dataset["prefix"] = "data/runs/"
    dataset["expire_prefixes"] = {"tmp/": 1}
    (problem,) = _problems(raw, _central(settings, expire_prefixes={"tmp/": 1}))
    assert "prefix" in problem and "config.yaml" in problem


def test_the_approved_prefix_passes(settings: Settings) -> None:
    raw = _raw()
    _no_task_runner_writer(raw)
    assert _problems(raw, _protect(settings, "runs/")) == []


def test_an_empty_central_entry_imposes_no_prefix(settings: Settings) -> None:
    raw = _raw()
    _component(raw, "dataset")["prefix"] = "anything/"
    assert _problems(raw, settings) == []
