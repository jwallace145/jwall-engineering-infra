"""The components schema: container-image and lichess-bot projects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jwall_engineering_sites.errors import ConfigError
from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings
from jwall_engineering_sites.site import load_site_config
from tests.conftest import FIXTURES

BOT_FILE = FIXTURES / "bots" / "rusty-chess.yaml"


def _raw() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(BOT_FILE.read_text())
    return raw


def _bot(raw: dict[str, Any]) -> dict[str, Any]:
    component: dict[str, Any] = next(c for c in raw["components"] if c["kind"] == "lichess-bot")
    return component


def _image(raw: dict[str, Any]) -> dict[str, Any]:
    component: dict[str, Any] = next(c for c in raw["components"] if c["kind"] == "container-image")
    return component


def _load(tmp_path: Path, raw: dict[str, Any], settings: Settings) -> Project:
    path = tmp_path / "jwall-engineering-sites.yaml"
    path.write_text(yaml.safe_dump(raw))
    return Project.from_config(load_site_config(path), settings)


def test_bot_fixture_loads(bot: Project) -> None:
    assert bot.kind == "bot"
    assert bot.project == "rusty-chess"
    assert bot.image_repository == "jwall-engineering-rusty-chess"
    assert bot.bot_name == "jwall-engineering-rusty-chess-bot"
    assert bot.records_bucket == "jwall-engineering-rusty-chess-records"
    assert bot.vpc_name == "jwall-engineering-rusty-chess"
    assert bot.log_group == "/jwall-engineering/rusty-chess/bot"
    assert bot.parameter_prefix == "/jwall-engineering/rusty-chess/bot"
    assert bot.parameter("desired-version") == "/jwall-engineering/rusty-chess/bot/desired-version"
    assert bot.state_key == "project=rusty-chess/env=prod/rusty-chess-prod.tfstate"
    assert bot.role_name("release") == "jwall-engineering-rusty-chess-ci-release"
    assert bot.role_name("apply") == "jwall-engineering-rusty-chess-ci-apply"
    assert bot.profile_url == "https://lichess.org/@/rusty-chess-bot"
    assert bot.bot.engine.hash_mb == 128
    assert bot.bot.engine.move_overhead_ms == 500
    assert bot.bot.secrets == {"LICHESS_TOKEN": "/jwall-engineering/rusty-chess/lichess-token"}
    assert bot.image.artifact.binary == "rusty-chess"
    assert bot.image.architecture == "x86_64"
    assert bot.zone_id is None


def test_site_accessors_refuse_on_a_bot(bot: Project) -> None:
    with pytest.raises(ConfigError, match="not a site"):
        _ = bot.site_bucket


def test_bot_accessors_refuse_on_a_site(settings: Settings) -> None:
    site = Project.from_config(
        load_site_config(FIXTURES / "sites" / "guitar-knowledge-base.yaml"), settings
    )
    assert site.kind == "site"
    with pytest.raises(ConfigError, match="not a bot"):
        _ = site.bot


def test_unknown_field_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["extra"] = 1
    with pytest.raises(ConfigError, match="extra"):
        _load(tmp_path, raw, settings)


def test_secret_outside_project_path_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["secrets"] = {"LICHESS_TOKEN": "/other/token"}
    with pytest.raises(ConfigError, match="must start with /jwall-engineering/rusty-chess/"):
        _load(tmp_path, raw, settings)


def test_secret_under_bot_prefix_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["secrets"] = {"LICHESS_TOKEN": "/jwall-engineering/rusty-chess/bot/token"}
    with pytest.raises(ConfigError, match="bot/"):
        _load(tmp_path, raw, settings)


def test_missing_lichess_token_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["secrets"] = {"OTHER": "/jwall-engineering/rusty-chess/other"}
    with pytest.raises(ConfigError, match="LICHESS_TOKEN"):
        _load(tmp_path, raw, settings)


def test_two_bots_fail(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"].append(dict(_bot(raw)))
    with pytest.raises(ConfigError, match="exactly one lichess-bot"):
        _load(tmp_path, raw, settings)


def test_bot_without_image_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [_bot(raw)]
    with pytest.raises(ConfigError, match="exactly one container-image"):
        _load(tmp_path, raw, settings)


def test_components_with_domain_fail(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["domain"] = "chess.jwall.engineering"
    with pytest.raises(ConfigError, match="either"):
        _load(tmp_path, raw, settings)


def test_site_without_site_section_fails(tmp_path: Path, settings: Settings) -> None:
    raw: dict[str, Any] = yaml.safe_load(
        (FIXTURES / "sites" / "guitar-knowledge-base.yaml").read_text()
    )
    del raw["site"]
    with pytest.raises(ConfigError, match="either"):
        _load(tmp_path, raw, settings)


def test_asset_needs_version_placeholder(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _image(raw)["artifact"]["asset"] = "rusty-chess-linux.tar.gz"
    with pytest.raises(ConfigError, match=r"\{version\}"):
        _load(tmp_path, raw, settings)


def test_binary_is_one_path_component(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _image(raw)["artifact"]["binary"] = "../rusty-chess"
    with pytest.raises(ConfigError, match="binary"):
        _load(tmp_path, raw, settings)


def test_config_must_be_a_relative_yaml_path(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["config"] = "/etc/lichess.yml"
    with pytest.raises(ConfigError, match="config"):
        _load(tmp_path, raw, settings)


def test_bad_retention_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _bot(raw)["logs"] = {"retention_days": 31}
    with pytest.raises(ConfigError, match="retention_days"):
        _load(tmp_path, raw, settings)


def test_bot_tags_are_cross_checked(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["tags"]["Project"] = "other"
    with pytest.raises(ConfigError, match="Project must be 'rusty-chess'"):
        _load(tmp_path, raw, settings)


def test_bot_organization_tag_is_checked(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["tags"]["Organization"] = "someone-else"
    with pytest.raises(ConfigError, match=r"tags\.Organization"):
        _load(tmp_path, raw, settings)


def _without_task_runner(raw: dict[str, Any]) -> dict[str, Any]:
    raw["components"] = [c for c in raw["components"] if c["kind"] != "task-runner"]
    return raw


def _task_runner(raw: dict[str, Any]) -> dict[str, Any]:
    component: dict[str, Any] = next(c for c in raw["components"] if c["kind"] == "task-runner")
    return component


def test_task_runner_loads_and_names_derive(bot: Project) -> None:
    runner = bot.task_runner
    assert runner is not None
    assert runner.instance_types == ("c8a.xlarge", "c8a.8xlarge", "c8a.16xlarge")
    assert runner.default_instance_type == "c8a.16xlarge"
    assert (runner.market, runner.max_price_fraction, runner.on_demand_fallback) == (
        "spot",
        0.58,
        True,
    )
    assert (runner.spot_wait_minutes, runner.max_vcpus, runner.slot_wait_minutes) == (5, 192, 30)
    assert (runner.max_hours, runner.results_days, runner.log_retention_days) == (6, 7, 14)
    assert bot.task_name == "jwall-engineering-rusty-chess-task"
    assert bot.task_template == "jwall-engineering-rusty-chess-task"
    assert bot.task_log_group == "/jwall-engineering/rusty-chess/tasks"
    assert bot.task_bucket == "jwall-engineering-rusty-chess-tasks"
    assert bot.task_prefix == "tasks/"
    assert bot.role_name("task") == "jwall-engineering-rusty-chess-ci-task"


def test_task_runner_defaults(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [c for c in raw["components"] if c["kind"] != "task-runner"]
    raw["components"].append({"kind": "task-runner"})
    runner = _load(tmp_path, raw, settings).task_runner
    assert runner is not None
    assert runner.instance_types == ("c8a.xlarge", "c8a.8xlarge", "c8a.16xlarge")
    assert runner.default_instance_type == "c8a.16xlarge"
    assert (runner.market, runner.max_price_fraction, runner.on_demand_fallback) == (
        "spot",
        0.58,
        True,
    )
    assert (runner.spot_wait_minutes, runner.max_vcpus, runner.slot_wait_minutes) == (5, 192, 30)
    assert runner.max_hours == 6


def test_project_without_task_runner_loads(tmp_path: Path, settings: Settings) -> None:
    raw = _without_task_runner(_raw())
    raw["components"] = [c for c in raw["components"] if c["kind"] != "dataset"]
    assert _load(tmp_path, raw, settings).task_runner is None


@pytest.mark.parametrize("bad", ["t3.large", "t4g.xlarge", "c8a xlarge", "C8A.xlarge"])
def test_task_runner_rejects_burstable_or_malformed_types(
    tmp_path: Path, settings: Settings, bad: str
) -> None:
    raw = _raw()
    _task_runner(raw)["instance_types"] = ["c8a.xlarge", bad]
    with pytest.raises(ConfigError, match="instance_types"):
        _load(tmp_path, raw, settings)


def test_task_runner_rejects_empty_types(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _task_runner(raw)["instance_types"] = []
    with pytest.raises(ConfigError, match="instance_types"):
        _load(tmp_path, raw, settings)


def test_task_runner_default_must_be_allowed(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _task_runner(raw)["default_instance_type"] = "c8a.4xlarge"
    with pytest.raises(ConfigError, match="default_instance_type"):
        _load(tmp_path, raw, settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_hours", 0), ("max_hours", 25), ("max_vcpus", 0), ("max_vcpus", 1025),
     ("results_days", 0), ("results_days", 31), ("log_retention_days", 10),
     ("max_price_fraction", 0), ("max_price_fraction", 1.01), ("spot_wait_minutes", 0),
     ("spot_wait_minutes", 31), ("slot_wait_minutes", -1), ("slot_wait_minutes", 121),
     ("market", "reserved")],
)  # fmt: skip
def test_task_runner_bounds(tmp_path: Path, settings: Settings, field: str, value: object) -> None:
    raw = _raw()
    _task_runner(raw)[field] = value
    with pytest.raises(ConfigError, match=field):
        _load(tmp_path, raw, settings)


def test_two_task_runners_fail(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"].append(dict(_task_runner(raw)))
    with pytest.raises(ConfigError, match="at most one task-runner"):
        _load(tmp_path, raw, settings)


def test_task_runner_alone_is_a_research_project(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [{**_task_runner(raw), "profiles": {}}]
    project = _load(tmp_path, raw, settings)
    assert project.config.kind == "research"
    assert project.task_runner is not None


def _dataset(raw: dict[str, Any]) -> dict[str, Any]:
    component: dict[str, Any] = next(c for c in raw["components"] if c["kind"] == "dataset")
    return component


def test_dataset_loads(bot: Project) -> None:
    (dataset,) = bot.datasets
    assert dataset.name == "nnue"
    assert dataset.bucket == "jwall-engineering-rusty-chess-nnue"
    assert dataset.prefix == "data/"
    assert dataset.writers == ("task-runner",)
    assert dataset.readers == ()
    assert dataset.transition_days == 0
    assert bot.dataset_bucket("nnue") == "jwall-engineering-rusty-chess-nnue"


def test_project_without_dataset_loads(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [c for c in raw["components"] if c["kind"] != "dataset"]
    for component in raw["components"]:
        for profile in component.get("profiles", {}).values():
            profile.pop("datasets", None)
    assert _load(tmp_path, raw, settings).datasets == ()


def test_dataset_bucket_must_be_the_derived_name(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _dataset(raw)["bucket"] = "my-own-bucket"
    with pytest.raises(ConfigError, match="jwall-engineering-rusty-chess-nnue"):
        _load(tmp_path, raw, settings)


def test_dataset_bucket_is_required(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    del _dataset(raw)["bucket"]
    with pytest.raises(ConfigError, match="bucket"):
        _load(tmp_path, raw, settings)


@pytest.mark.parametrize("bad", ["data", "/data/", "", "Data/", "data//", "../data/"])
def test_dataset_prefix_shape(tmp_path: Path, settings: Settings, bad: str) -> None:
    raw = _raw()
    _dataset(raw)["prefix"] = bad
    with pytest.raises(ConfigError, match="prefix"):
        _load(tmp_path, raw, settings)


def test_dataset_nested_prefix_loads(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _dataset(raw)["prefix"] = "data/selfplay=v1/"
    assert _load(tmp_path, raw, settings).datasets[0].prefix == "data/selfplay=v1/"


@pytest.mark.parametrize("bad", ["records", "tasks", "NNUE", "nn_ue", "-nnue"])
def test_dataset_name_shape_and_reserved_names(
    tmp_path: Path, settings: Settings, bad: str
) -> None:
    raw = _raw()
    _dataset(raw)["name"] = bad
    _dataset(raw)["bucket"] = f"jwall-engineering-rusty-chess-{bad}"
    with pytest.raises(ConfigError, match="name"):
        _load(tmp_path, raw, settings)


def test_dataset_task_runner_writer_needs_a_task_runner(tmp_path: Path, settings: Settings) -> None:
    raw = _without_task_runner(_raw())
    with pytest.raises(ConfigError, match="task-runner"):
        _load(tmp_path, raw, settings)


def test_dataset_without_writers_needs_no_task_runner(tmp_path: Path, settings: Settings) -> None:
    raw = _without_task_runner(_raw())
    _dataset(raw)["writers"] = []
    assert _load(tmp_path, raw, settings).datasets[0].writers == ()


def test_dataset_unknown_writer_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _dataset(raw)["writers"] = ["lichess-bot"]
    with pytest.raises(ConfigError, match="writers"):
        _load(tmp_path, raw, settings)


def test_dataset_readers_are_role_arns(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _dataset(raw)["readers"] = ["*"]
    with pytest.raises(ConfigError, match="readers"):
        _load(tmp_path, raw, settings)
    _dataset(raw)["readers"] = ["arn:aws:iam::123456789012:role/trainer"]
    assert _load(tmp_path, raw, settings).datasets[0].readers == (
        "arn:aws:iam::123456789012:role/trainer",
    )


def test_dataset_transition_days_bounds(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _dataset(raw)["transition_days"] = -1
    with pytest.raises(ConfigError, match="transition_days"):
        _load(tmp_path, raw, settings)


def test_two_datasets_with_one_name_fail(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"].append(dict(_dataset(raw)))
    with pytest.raises(ConfigError, match="dataset names must be unique"):
        _load(tmp_path, raw, settings)


# -- task profiles ----------------------------------------------------------------


def _gpu(raw: dict[str, Any]) -> dict[str, Any]:
    profile: dict[str, Any] = _task_runner(raw)["profiles"]["gpu"]
    return profile


def test_the_gpu_profile_loads(bot: Project) -> None:
    runner = bot.task_runner
    assert runner is not None
    assert runner.profile_names == ("cpu", "gpu", "sprt", "match", "datagen", "relabel")
    gpu = runner.profile("gpu")
    assert (gpu.default_instance_type, gpu.market, gpu.max_vcpus) == ("g6.xlarge", "on-demand", 4)
    assert (gpu.image, gpu.root_volume_gb, gpu.whole_cores, gpu.gpu) == (
        "deep-learning-gpu", 200, False, True,
    )  # fmt: skip
    assert gpu.datasets["nnue"].read == ("selfplay/",)
    assert gpu.datasets["nnue"].write == ("nets/",)
    assert bot.profile_task_name("gpu") == "jwall-engineering-rusty-chess-task-gpu"
    assert bot.profile_task_name("cpu") == "jwall-engineering-rusty-chess-task"


def test_the_cpu_profile_is_the_task_runners_own_fields(bot: Project) -> None:
    runner = bot.task_runner
    assert runner is not None
    cpu = runner.profile("cpu")
    assert cpu.instance_types == runner.instance_types
    assert (cpu.market, cpu.max_vcpus, cpu.whole_cores, cpu.gpu, cpu.image) == (
        "spot", 192, True, False, None,
    )  # fmt: skip


@pytest.mark.parametrize("name", ["sprt", "match", "datagen", "relabel"])
def test_a_cpu_workflow_profile_has_the_cpu_profiles_instance_setup(
    bot: Project, name: str
) -> None:
    """No image and no gpu: the pinned Amazon Linux image, whole cores, the same disk."""
    runner = bot.task_runner
    assert runner is not None
    cpu, profile = runner.profile("cpu"), runner.profile(name)
    assert (profile.image, profile.gpu, profile.whole_cores) == (None, False, True)
    assert (profile.root_volume_gb, profile.scratch) == (cpu.root_volume_gb, cpu.scratch)
    assert profile.max_hours == cpu.max_hours


@pytest.mark.parametrize(
    ("name", "market", "max_vcpus"),
    [
        ("sprt", "on-demand", 128),
        ("match", "on-demand", 64),
        ("datagen", "spot", 192),
        ("relabel", "spot", 192),
    ],
)
def test_each_workflow_profile_has_its_market_and_budget(
    bot: Project, name: str, market: str, max_vcpus: int
) -> None:
    runner = bot.task_runner
    assert runner is not None
    profile = runner.profile(name)
    assert (profile.market, profile.max_vcpus) == (market, max_vcpus)
    assert profile.default_instance_type == "c8a.16xlarge"
    assert (profile.max_price_fraction, profile.on_demand_fallback) == (0.58, True)


def test_the_workflow_profiles_dataset_grants(bot: Project) -> None:
    """A gating or match task touches no data; relabel writes only its own part."""
    runner = bot.task_runner
    assert runner is not None
    assert runner.profile("sprt").datasets == {} and runner.profile("match").datasets == {}
    datagen = runner.profile("datagen").datasets["nnue"]
    assert (datagen.read, datagen.write) == ((), ("selfplay/",))
    relabel = runner.profile("relabel").datasets["nnue"]
    assert (relabel.read, relabel.write) == (("selfplay/",), ("selfplay/relabel/",))


def test_scratch_defaults_to_the_instance_store_for_gpu_profiles_only(bot: Project) -> None:
    """The rusty-chess file sets no `scratch`: gpu reads from the NVMe, cpu stays put."""
    runner = bot.task_runner
    assert runner is not None
    assert runner.profile("gpu").scratch == "instance-store"
    assert runner.profile("cpu").scratch == "root-volume"


@pytest.mark.parametrize(("gpu", "scratch"), [(True, "root-volume"), (False, "instance-store")])
def test_a_profile_may_choose_its_scratch(
    tmp_path: Path, settings: Settings, gpu: bool, scratch: str
) -> None:
    raw = _raw()
    _gpu(raw).update(gpu=gpu, scratch=scratch)
    runner = _load(tmp_path, raw, settings).task_runner
    assert runner is not None and runner.profile("gpu").scratch == scratch


def test_scratch_takes_only_the_two_places(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _gpu(raw)["scratch"] = "tmpfs"
    with pytest.raises(ConfigError, match="scratch"):
        _load(tmp_path, raw, settings)


def test_the_allow_list_covers_every_profile(bot: Project) -> None:
    runner = bot.task_runner
    assert runner is not None
    assert runner.all_instance_types == (
        "c8a.xlarge", "c8a.8xlarge", "c8a.16xlarge", "g6.xlarge", "g6.2xlarge", "g6e.xlarge",
    )  # fmt: skip


def test_a_file_without_profiles_is_unchanged(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    del _task_runner(raw)["profiles"]
    runner = _load(tmp_path, raw, settings).task_runner
    assert runner is not None and runner.profile_names == ("cpu",)


def test_cpu_is_not_a_listed_profile(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _task_runner(raw)["profiles"]["cpu"] = _gpu(raw)
    with pytest.raises(ConfigError, match="cpu is the task-runner's own fields"):
        _load(tmp_path, raw, settings)


def test_a_profile_needs_a_pinned_image(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _gpu(raw)["image"] = "unpinned"
    with pytest.raises(ConfigError, match="image 'unpinned' is not pinned"):
        _load(tmp_path, raw, settings)


def test_a_profile_may_only_reach_the_files_datasets(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _gpu(raw)["datasets"] = {"other": {"read": ["x/"]}}
    with pytest.raises(ConfigError, match="datasets other are not dataset components"):
        _load(tmp_path, raw, settings)


@pytest.mark.parametrize("prefix", ["/selfplay/", "selfplay", "../nets/"])
def test_dataset_access_takes_sub_prefixes(tmp_path: Path, settings: Settings, prefix: str) -> None:
    raw = _raw()
    _gpu(raw)["datasets"] = {"nnue": {"read": [prefix]}}
    with pytest.raises(ConfigError, match="sub-prefix"):
        _load(tmp_path, raw, settings)


def test_dataset_access_grants_something(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _gpu(raw)["datasets"] = {"nnue": {}}
    with pytest.raises(ConfigError, match="at least one read or write"):
        _load(tmp_path, raw, settings)


def test_a_profile_refuses_burstable_types(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _gpu(raw)["instance_types"] = ["t3.large"]
    _gpu(raw)["default_instance_type"] = "t3.large"
    with pytest.raises(ConfigError, match="burstable"):
        _load(tmp_path, raw, settings)


def test_a_profile_default_type_must_be_allowed(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    _gpu(raw)["default_instance_type"] = "g6.12xlarge"
    with pytest.raises(ConfigError, match="must be one of instance_types"):
        _load(tmp_path, raw, settings)


# -- research projects: a dataset and a task-runner, no bot -------------------


def test_the_research_fixture_is_a_research_project(research: Project, bot: Project) -> None:
    assert research.kind == "research"
    assert research.config.kind == "research"
    assert research.stack_kind == "bot"
    assert research.has_components
    assert bot.kind == "bot"
    assert bot.stack_kind == "bot"
    assert bot.has_components


def test_a_site_has_no_components_and_the_site_stack(settings: Settings) -> None:
    site = Project.from_config(
        load_site_config(FIXTURES / "sites" / "guitar-knowledge-base.yaml"), settings
    )
    assert not site.has_components
    assert site.stack_kind == "site"


def test_a_file_with_only_a_dataset_is_research(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [_dataset(raw)]
    _dataset(raw)["writers"] = []
    project = _load(tmp_path, raw, settings)
    assert project.kind == "research"
    assert project.task_runner is None
    assert [d.name for d in project.datasets] == ["nnue"]


def test_an_image_without_a_bot_still_fails(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"] = [_image(raw), _task_runner(raw)]
    with pytest.raises(ConfigError, match="exactly one lichess-bot"):
        _load(tmp_path, raw, settings)


def test_the_research_project_uses_the_task_names(research: Project) -> None:
    assert research.task_bucket == "jwall-engineering-math-prover-tasks"
    assert research.task_log_group == "/jwall-engineering/math-prover/tasks"
    assert research.profile_task_name("cpu") == "jwall-engineering-math-prover-task"
    assert research.profile_task_name("train") == "jwall-engineering-math-prover-task-train"
    assert research.task_runner is not None
    assert research.task_runner.profile_names == ("cpu", "train")
    (dataset,) = research.datasets
    assert dataset.bucket == "jwall-engineering-math-prover-artifacts"
    assert research.dataset_bucket("artifacts") == dataset.bucket


def test_bot_accessors_refuse_on_research(research: Project) -> None:
    with pytest.raises(ConfigError, match="not a bot"):
        _ = research.bot
    with pytest.raises(ConfigError, match="not a bot"):
        _ = research.image
    with pytest.raises(ConfigError, match="not a site"):
        _ = research.site_bucket


def test_research_secrets_are_not_read(research: Project) -> None:
    # A research file has no lichess-bot to hold secrets; loading never asks for one.
    assert not any(c.kind == "lichess-bot" for c in research.config.components)


def test_rust_defaults_to_true_and_the_cpu_profile_follows_it(
    bot: Project, research: Project
) -> None:
    runner = bot.task_runner
    assert runner is not None
    assert runner.rust is True
    assert runner.profile("cpu").rust is True
    assert runner.profile("gpu").rust is True
    research_runner = research.task_runner
    assert research_runner is not None
    assert research_runner.rust is False
    assert research_runner.profile("cpu").rust is False
    assert research_runner.profile("train").rust is False


# -- databricks projects: only a databricks-workspace --------------------------

DATABRICKS_FILE = FIXTURES / "bots" / "databricks-poc.yaml"


@pytest.fixture
def databricks(settings: Settings) -> Project:
    return Project.from_config(load_site_config(DATABRICKS_FILE), settings)


def test_databricks_only_project_is_kind_databricks(databricks: Project) -> None:
    assert databricks.kind == "databricks"
    assert databricks.config.kind == "databricks"
    assert databricks.stack_kind == "databricks"
    assert databricks.has_components
    assert databricks.databricks.compute_policies == ("small-interactive", "jobs")
    assert databricks.databricks.deployment_name is None
    assert databricks.state_key == "project=databricks-poc/env=prod/databricks-poc-prod.tfstate"


def test_databricks_with_bot_rejected(tmp_path: Path, settings: Settings) -> None:
    raw = _raw()
    raw["components"].append({"kind": "databricks-workspace", "compute_policies": ["jobs"]})
    with pytest.raises(ConfigError, match="only component"):
        _load(tmp_path, raw, settings)


def test_databricks_with_dataset_rejected(tmp_path: Path, settings: Settings) -> None:
    raw: dict[str, Any] = yaml.safe_load(DATABRICKS_FILE.read_text())
    raw["components"].append(
        {
            "kind": "dataset",
            "name": "nnue",
            "bucket": "jwall-engineering-databricks-poc-nnue",
            "prefix": "data/",
        }
    )
    with pytest.raises(ConfigError, match="only component"):
        _load(tmp_path, raw, settings)


def test_two_databricks_workspaces_fail(tmp_path: Path, settings: Settings) -> None:
    raw: dict[str, Any] = yaml.safe_load(DATABRICKS_FILE.read_text())
    raw["components"].append(dict(raw["components"][0]))
    with pytest.raises(ConfigError, match="at most one databricks-workspace"):
        _load(tmp_path, raw, settings)


def test_policy_outside_guardrails_rejected(tmp_path: Path, settings: Settings) -> None:
    raw: dict[str, Any] = yaml.safe_load(DATABRICKS_FILE.read_text())
    raw["components"][0]["compute_policies"] = ["small-interactive", "gpu-everything"]
    with pytest.raises(ConfigError, match=r"gpu-everything.*config\.yaml"):
        _load(tmp_path, raw, settings)


def test_databricks_project_without_guardrail_entry_rejected(
    tmp_path: Path, settings: Settings
) -> None:
    bare = settings.model_copy(
        update={
            "projects": {
                **settings.projects,
                "databricks-poc": settings.projects["databricks-poc"].model_copy(
                    update={"databricks": None}
                ),
            }
        }
    )
    raw: dict[str, Any] = yaml.safe_load(DATABRICKS_FILE.read_text())
    with pytest.raises(ConfigError, match=r"projects\.databricks-poc\.databricks.*config\.yaml"):
        _load(tmp_path, raw, bare)


def test_databricks_tags_include_cost_center_and_workspace(databricks: Project) -> None:
    tags = databricks.databricks_tags
    assert tags["CostCenter"] == "cc-0000"
    assert tags["Workspace"] == "databricks-poc-prod"
    required = {"Project", "Environment", "CostCenter", "Owner", "ManagedBy", "Workspace"}
    assert required <= tags.keys()
    assert not {"Vendor", "Creator", "ClusterName", "ClusterId", "Name"} & tags.keys()
    assert tags["Project"] == "databricks-poc"


def test_databricks_reserved_tag_keys_rejected(tmp_path: Path, settings: Settings) -> None:
    raw: dict[str, Any] = yaml.safe_load(DATABRICKS_FILE.read_text())
    raw["tags"]["Creator"] = "someone"
    with pytest.raises(ConfigError, match="Creator"):
        _load(tmp_path, raw, settings)


def test_databricks_accessors_refuse_on_other_kinds(bot: Project) -> None:
    with pytest.raises(ConfigError, match="not a databricks project"):
        _ = bot.databricks
    with pytest.raises(ConfigError, match="not a databricks project"):
        _ = bot.databricks_tags


def test_organization_tag_is_still_checked_for_databricks(
    tmp_path: Path, settings: Settings
) -> None:
    raw: dict[str, Any] = yaml.safe_load(DATABRICKS_FILE.read_text())
    raw["tags"]["Organization"] = "someone-else"
    with pytest.raises(ConfigError, match="Organization"):
        _load(tmp_path, raw, settings)
