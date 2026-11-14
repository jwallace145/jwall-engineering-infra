from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import (
    DatasetRetention,
    ProjectGuardrails,
    Settings,
    load_settings,
)
from jwall_engineering_sites.site import Site, load_site_config

FIXTURES = Path(__file__).parent / "fixtures"
SITE_FIXTURES = sorted((FIXTURES / "sites").glob("*.yaml"))
REPO_ROOT = Path(__file__).resolve().parent.parent


#: The central entry the math-prover sample would have once it is onboarded.
MATH_PROVER_GUARDRAILS = ProjectGuardrails(
    instance_types=("c8a.xlarge", "g6e.xlarge", "g6e.2xlarge", "g6e.4xlarge"),
    task_profiles=("cpu", "train"),
    datasets={
        "jwall-engineering-math-prover-artifacts": DatasetRetention(
            prefix="data/",
            versioning=True,
            expire_noncurrent_days=90,
            expire_prefixes={"tmp/": 30},
            write_once=("runs/", "models/"),
        )
    },
)


def with_guardrails(settings: Settings, project: str, guardrails: ProjectGuardrails) -> Settings:
    """`settings` with `guardrails` as the central entry for `project`."""
    return settings.model_copy(update={"projects": {**settings.projects, project: guardrails}})


@pytest.fixture
def settings() -> Settings:
    return load_settings(FIXTURES / "config.yaml", environ={})


@pytest.fixture(params=SITE_FIXTURES, ids=[p.stem for p in SITE_FIXTURES])
def site(request: pytest.FixtureRequest, settings: Settings) -> Site:
    return Site.from_config(load_site_config(request.param), settings)


@pytest.fixture
def guitar(settings: Settings) -> Site:
    return Site.from_config(
        load_site_config(FIXTURES / "sites" / "guitar-knowledge-base.yaml"), settings
    )


@pytest.fixture
def bot(settings: Settings) -> Project:
    return Project.from_config(load_site_config(FIXTURES / "bots" / "rusty-chess.yaml"), settings)


@pytest.fixture
def research(settings: Settings) -> Project:
    """The math-prover sample: a dataset and a task-runner, no bot, with its central entry."""
    return Project.from_config(
        load_site_config(FIXTURES / "bots" / "math-prover.yaml"),
        with_guardrails(settings, "math-prover", MATH_PROVER_GUARDRAILS),
    )


@pytest.fixture
def logger() -> logging.Logger:
    log = logging.getLogger("test-jwall-engineering-sites")
    log.setLevel(logging.DEBUG)
    return log


@pytest.fixture
def fake_terraform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the fake terraform first on PATH; returns its invocation log."""
    log = tmp_path / "terraform.log"
    outputs = tmp_path / "outputs.json"
    outputs.write_text(
        json.dumps(
            {
                "bucket_name": {"value": "jwall-engineering-guitar-knowledge-base"},
                "prod_url": {"value": "https://guitar.jwall.engineering/"},
                "prod_distribution_id": {"value": "E1PROD"},
                "dev_url": {"value": None},
                "dev_distribution_id": {"value": None},
                "preview_url_pattern": {"value": "https://pr-<n>.preview.guitar.jwall.engineering"},
                "preview_distribution_id": {"value": "E1PREVIEW"},
            }
        )
    )
    monkeypatch.setenv("PATH", f"{FIXTURES / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_TF_LOG", str(log))
    monkeypatch.setenv("FAKE_TF_OUTPUTS", str(outputs))
    monkeypatch.delenv("JES_STACK_DIR", raising=False)
    monkeypatch.setenv("JES_STACKS_ROOT", str(REPO_ROOT / "stacks"))
    monkeypatch.setenv("FAKE_TF_TFVARS", str(tmp_path / "rendered.tfvars.json"))
    monkeypatch.setenv("JES_PLUGIN_DIR", "")
    return log


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the fake docker first on PATH; returns its invocation log."""
    log = tmp_path / "docker.log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{FIXTURES / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    return log


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """A small built site."""
    root = tmp_path / "dist"
    (root / "_astro").mkdir(parents=True)
    (root / "index.html").write_text("<h1>home</h1>")
    (root / "about.html").write_text("<h1>about</h1>")
    (root / "_astro" / "app.abc123.js").write_text("console.log(1)")
    (root / "robots.txt").write_text("User-agent: *")
    return root


@pytest.fixture
def bot_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory holding the rusty-chess bot file, its lichess config and config.yaml."""
    root = tmp_path / "bot-workspace"
    (root / "lichess").mkdir(parents=True)
    shutil.copy(FIXTURES / "bots" / "rusty-chess.yaml", root / "jwall-engineering-sites.yaml")
    shutil.copy(FIXTURES / "config.yaml", root / "config.yaml")
    (root / "lichess" / "config.yml").write_text(
        "token: LICHESS_TOKEN_PLACEHOLDER\nurl: https://lichess.org/\n"
        "engine:\n  dir: ./engines/\n  name: rusty-chess\n  protocol: uci\n"
        "  uci_options:\n    Hash: 16\n"
    )
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    yield root


@pytest.fixture
def research_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory holding the math-prover file and a config.yaml approving it."""
    root = tmp_path / "research-workspace"
    root.mkdir()
    shutil.copy(FIXTURES / "bots" / "math-prover.yaml", root / "jwall-engineering-sites.yaml")
    config = yaml.safe_load((FIXTURES / "config.yaml").read_text())
    config["projects"]["math-prover"] = MATH_PROVER_GUARDRAILS.model_dump(mode="json")
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    yield root


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory holding the guitar site file and config.yaml."""
    root = tmp_path / "workspace"
    root.mkdir()
    shutil.copy(
        FIXTURES / "sites" / "guitar-knowledge-base.yaml", root / "jwall-engineering-sites.yaml"
    )
    shutil.copy(FIXTURES / "config.yaml", root / "config.yaml")
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    yield root


@pytest.fixture
def databricks_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A working directory with the databricks-poc file and a config.yaml enabling the account."""
    root = tmp_path / "databricks-workspace"
    root.mkdir()
    shutil.copy(FIXTURES / "bots" / "databricks-poc.yaml", root / "jwall-engineering-sites.yaml")
    config = yaml.safe_load((FIXTURES / "config.yaml").read_text())
    config["databricks"]["enabled"] = True
    config["databricks"]["account_id"] = "11111111-1111-1111-1111-111111111111"
    config["databricks"]["client_id"] = "22222222-2222-2222-2222-222222222222"
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    monkeypatch.chdir(root)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    yield root
