"""A consumer's GitHub releases: which tags exist, and the verified engine binary.

The image ships the exact binary attached to a release (the one its benchmark
and rating describe), never a rebuild: download the tarball and its .sha256,
verify, and extract the one binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .components import ContainerImage
from .errors import PreflightError, RunError

#: A release tag the tool deploys: vX.Y.Z, no prerelease suffix.
RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")

#: (url, headers) -> body. Injected in tests; urllib by default.
Fetch = Callable[[str, dict[str, str]], bytes]

API = "https://api.github.com"


@dataclass(frozen=True)
class Release:
    tag: str
    assets: dict[str, str] = field(default_factory=dict)  # name -> download URL


def _http_get(url: str, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "jwall-engineering-sites", **headers}
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body: bytes = response.read()
            return body
    except OSError as exc:
        raise RunError(f"GET {url} failed: {exc}") from exc


def _api_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _version_key(tag: str) -> tuple[int, int, int]:
    match = RELEASE_TAG.match(tag)
    assert match
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def list_releases(repository: str, fetch: Fetch | None = None) -> list[Release]:
    """Published vX.Y.Z releases of `owner/repo`, newest first. Drafts,
    prereleases and other tags are left out."""
    fetch = fetch or _http_get
    raw = json.loads(fetch(f"{API}/repos/{repository}/releases?per_page=100", _api_headers()))
    found = [
        Release(
            tag=item["tag_name"],
            assets={a["name"]: a["browser_download_url"] for a in item.get("assets", [])},
        )
        for item in raw
        if not item.get("draft")
        and not item.get("prerelease")
        and RELEASE_TAG.match(item.get("tag_name", ""))
    ]
    return sorted(found, key=lambda r: _version_key(r.tag), reverse=True)


def newest(found: list[Release]) -> Release | None:
    return found[0] if found else None


def find(found: list[Release], tag: str) -> Release:
    for release in found:
        if release.tag == tag:
            return release
    raise PreflightError(
        f"{tag} is not a published release (known: {', '.join(r.tag for r in found[:5]) or 'none'})"
    )


def asset_names(image: ContainerImage, tag: str) -> tuple[str, str]:
    return image.artifact.asset_name(tag), image.artifact.checksum_name(tag)


def asset_url(release: Release, name: str) -> str:
    try:
        return release.assets[name]
    except KeyError:
        raise PreflightError(f"release {release.tag} has no asset {name}") from None


def download(url: str, target: Path, fetch: Fetch | None = None) -> Path:
    fetch = fetch or _http_get
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(fetch(url, {"Accept": "application/octet-stream"}))
    return target


def verify_sha256(archive: Path, checksum_file: Path) -> str:
    """Compare the archive's SHA-256 with the first token of the checksum
    file's first non-empty line (`<hex>  <name>`, any line ending, any case)."""
    lines = [line.strip() for line in checksum_file.read_text().splitlines() if line.strip()]
    expected = lines[0].split()[0].lower() if lines else ""
    if not _SHA256.match(expected):
        raise RunError(f"{checksum_file.name} holds no SHA-256 digest")
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        raise RunError(f"checksum mismatch for {archive.name}: expected {expected}, got {actual}")
    return actual


def extract_binary(archive: Path, binary: str, dest_dir: Path) -> Path:
    """Extract exactly one regular file named `binary`, one directory deep."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RunError(f"{archive.name} has an unsafe member {member.name!r}")
        matches = [m for m in members if PurePosixPath(m.name).name == binary]
        if not matches:
            raise RunError(f"{archive.name} holds no {binary}")
        if len(matches) > 1:
            raise RunError(f"{archive.name} holds more than one {binary}")
        member = matches[0]
        if not member.isreg() or len(PurePosixPath(member.name).parts) != 2:
            raise RunError(f"{binary} in {archive.name} must be a regular file one directory deep")
        source = tar.extractfile(member)
        assert source is not None
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / binary
        target.write_bytes(source.read())
    target.chmod(0o755)
    return target
