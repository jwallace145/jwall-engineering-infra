from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings, load_settings
from jwall_engineering_sites.site import Site, load_site_config

FIXTURES = Path(__file__).parent / "fixtures"
SITE_FIXTURES = sorted((FIXTURES / "sites").glob("*.yaml"))
REPO_ROOT = Path(__file__).resolve().parent.parent


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
    monkeypatch.setenv("JES_STACK_DIR", str(REPO_ROOT / "stacks" / "site"))
    monkeypatch.setenv("JES_PLUGIN_DIR", "")
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
