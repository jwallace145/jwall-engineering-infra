"""The composite action pins the image tag; Release Please bumps it with the
package version. The reusable workflows carry no version at all: they check
this repository out at `github.job_workflow_sha`, the commit of the reusable
workflow itself, and use the action from that checkout, so a site's one pin
(the workflow ref) selects the action and the image together.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from jwall_engineering_sites import __version__
from tests.conftest import REPO_ROOT

ANNOTATION = "# x-release-please-version"
WORKFLOWS = ("site-plan.yml", "site-preview.yml", "site-deploy.yml")
LOCAL_ACTION = "uses: ./.jwall-engineering-infra/.github/actions/jwall-engineering-sites"


def _annotated_versions(path: Path) -> list[str]:
    versions = []
    for line in path.read_text().splitlines():
        if ANNOTATION in line:
            match = re.search(r"v?(\d+\.\d+\.\d+)", line.split(ANNOTATION)[0])
            assert match, f"{path}: annotated line has no version: {line!r}"
            versions.append(match.group(1))
    return versions


def _extra_file_paths() -> set[str]:
    config = json.loads((REPO_ROOT / "release-please-config.json").read_text())
    return {
        entry if isinstance(entry, str) else entry["path"]
        for entry in config["packages"]["."]["extra-files"]
    }


def test_release_please_tracks_every_annotated_file() -> None:
    annotated = {
        str(p.relative_to(REPO_ROOT))
        for p in [*REPO_ROOT.glob(".github/**/*.yml"), *REPO_ROOT.glob("src/**/*.py")]
        if ANNOTATION in p.read_text()
    }
    assert annotated == _extra_file_paths()


def test_no_workflow_carries_a_version_literal() -> None:
    """The release bot cannot commit under .github/workflows/, and must not need to."""
    for rel in _extra_file_paths():
        assert not rel.startswith(".github/workflows/"), rel


def test_annotated_versions_match_the_package_version() -> None:
    for rel in _extra_file_paths():
        versions = _annotated_versions(REPO_ROOT / rel)
        assert versions, f"{rel} has no annotated version"
        assert set(versions) == {__version__}, f"{rel}: {versions} != {__version__}"


def test_action_yml_uses_the_generic_updater() -> None:
    config = json.loads((REPO_ROOT / "release-please-config.json").read_text())
    yaml_entries = [
        e
        for e in config["packages"]["."]["extra-files"]
        if (e if isinstance(e, str) else e["path"]).endswith((".yml", ".yaml"))
    ]
    assert yaml_entries, "action.yml must be an extra-file"
    assert all(isinstance(e, dict) and e["type"] == "generic" for e in yaml_entries), (
        "a .yml extra-file given as a plain string gets the YAML ($.version) updater"
    )


def test_workflows_use_the_action_from_their_own_commit() -> None:
    for name in WORKFLOWS:
        text = (REPO_ROOT / ".github" / "workflows" / name).read_text()
        assert "ref: ${{ github.job_workflow_sha }}" in text, name
        assert LOCAL_ACTION in text, name
        assert "jwall-engineering-sites@v" not in text, f"{name} pins the action by tag"


def test_site_stack_pins_the_configured_library_version() -> None:
    """stacks/site pins the library like any consumer; config.yaml names the same version."""
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    main_tf = (REPO_ROOT / "stacks" / "site" / "main.tf").read_text()
    match = re.search(
        r'source\s*=\s*"jwallace145/jwall-engineering/aws"\s*\n\s*version\s*=\s*"([\d.]+)"', main_tf
    )
    assert match, "stacks/site/main.tf must pin the library root module"
    assert match.group(1) == config["library"]["version"]
    assert config["library"]["source"] == "jwallace145/jwall-engineering/aws"


_MODULE_PIN = re.compile(
    r'source\s*=\s*"jwallace145/jwall-engineering/aws(?://modules/[a-z-]+)?"\s*\n\s*version\s*=\s*"([\d.]+)"'
)


def test_bot_stack_pins_the_configured_library_version() -> None:
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    pins = _MODULE_PIN.findall((REPO_ROOT / "stacks" / "bot" / "main.tf").read_text())
    assert len(pins) == 6, (
        "stacks/bot composes ecr-repository, vpc, private-bucket (records and task results), "
        "container-instance and task-runner"
    )
    assert set(pins) == {config["library"]["version"]}


def test_every_registry_pin_matches_config() -> None:
    """AGENTS.md: every module pin in the repository moves together with library.version."""
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    files = [*(REPO_ROOT / "foundation").glob("*.tf"), *(REPO_ROOT / "stacks").glob("*/*.tf")]
    pins = [pin for f in files for pin in _MODULE_PIN.findall(f.read_text())]
    assert pins, "no registry module pins found"
    assert set(pins) == {config["library"]["version"]}
