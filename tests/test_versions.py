"""Every version literal Release Please bumps must agree with __version__.

The composite action pins the image tag and the reusable workflows pin the
action ref; a site pins the workflow ref. If any of them drifted, a site
would run a different image from the one its workflow ref names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jwall_engineering_sites import __version__
from tests.conftest import REPO_ROOT

ANNOTATION = "# x-release-please-version"


def _annotated_versions(path: Path) -> list[str]:
    versions = []
    for line in path.read_text().splitlines():
        if ANNOTATION in line:
            match = re.search(r"v?(\d+\.\d+\.\d+)", line.split(ANNOTATION)[0])
            assert match, f"{path}: annotated line has no version: {line!r}"
            versions.append(match.group(1))
    return versions


def test_release_please_tracks_every_annotated_file() -> None:
    config = json.loads((REPO_ROOT / "release-please-config.json").read_text())
    extra = set(config["packages"]["."]["extra-files"])
    annotated = {
        str(p.relative_to(REPO_ROOT))
        for p in [*REPO_ROOT.glob(".github/**/*.yml"), *REPO_ROOT.glob("src/**/*.py")]
        if ANNOTATION in p.read_text()
    }
    assert annotated == extra


def test_annotated_versions_match_the_package_version() -> None:
    config = json.loads((REPO_ROOT / "release-please-config.json").read_text())
    for rel in config["packages"]["."]["extra-files"]:
        versions = _annotated_versions(REPO_ROOT / rel)
        assert versions, f"{rel} has no annotated version"
        assert set(versions) == {__version__}, f"{rel}: {versions} != {__version__}"


def test_workflows_pin_the_action_by_tag() -> None:
    for name in ("site-plan.yml", "site-preview.yml", "site-deploy.yml"):
        text = (REPO_ROOT / ".github" / "workflows" / name).read_text()
        pattern = (
            r"uses: (jwallace145/jwall-engineering-infra/"
            r"\.github/actions/jwall-engineering-sites@v[\d.]+)"
        )
        refs = re.findall(pattern, text)
        assert refs, f"{name} does not use the composite action"
        assert {r.rsplit("@v", 1)[1] for r in refs} == {__version__}
        assert "uses: ./.github/actions" not in text, "relative uses resolves against the caller"
