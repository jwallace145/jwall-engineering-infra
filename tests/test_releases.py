"""GitHub release resolution, asset download and checksum verification."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import tarfile
from pathlib import Path

import pytest

from jwall_engineering_sites import releases
from jwall_engineering_sites.errors import PreflightError, RunError
from jwall_engineering_sites.project import Project

ASSET = "rusty-chess-v0.14.0-x86_64-unknown-linux-gnu.tar.gz"


def _release(tag: str, draft: bool = False, prerelease: bool = False) -> dict[str, object]:
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {"name": f"rusty-chess-{tag}.tar.gz", "browser_download_url": f"https://x/{tag}.tgz"}
        ],
    }


def _fetch_json(payload: object) -> releases.Fetch:
    def fetch(url: str, headers: dict[str, str]) -> bytes:
        assert (
            url == "https://api.github.com/repos/jwallace145/rusty-chess-v2/releases?per_page=100"
        )
        assert headers["Accept"] == "application/vnd.github+json"
        return json.dumps(payload).encode()

    return fetch


def test_list_releases_filters_and_sorts() -> None:
    payload = [
        _release("v0.14.0"),
        _release("v1.0.0-rc1"),
        _release("v0.9.2"),
        _release("v0.14.1"),
        _release("v2.0.0", draft=True),
        _release("v0.15.0", prerelease=True),
        _release("nightly"),
    ]
    found = releases.list_releases("jwallace145/rusty-chess-v2", fetch=_fetch_json(payload))
    assert [r.tag for r in found] == ["v0.14.1", "v0.14.0", "v0.9.2"]
    assert found[0].assets == {"rusty-chess-v0.14.1.tar.gz": "https://x/v0.14.1.tgz"}


def test_list_releases_sorts_numerically() -> None:
    payload = [_release("v0.9.0"), _release("v0.10.0")]
    found = releases.list_releases("jwallace145/rusty-chess-v2", fetch=_fetch_json(payload))
    assert [r.tag for r in found] == ["v0.10.0", "v0.9.0"]
    assert releases.newest(found) == found[0]
    assert releases.newest([]) is None


def test_github_token_is_sent_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    seen: dict[str, str] = {}

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        seen.update(headers)
        return b"[]"

    releases.list_releases("jwallace145/rusty-chess-v2", fetch=fetch)
    assert seen["Authorization"] == "Bearer t0ken"


def test_find_names_a_missing_tag() -> None:
    found = [releases.Release(tag="v0.14.0", assets={})]
    assert releases.find(found, "v0.14.0").tag == "v0.14.0"
    with pytest.raises(PreflightError, match=r"v9\.9\.9"):
        releases.find(found, "v9.9.9")


def test_asset_names(bot: Project) -> None:
    assert releases.asset_names(bot.image, "v0.14.0") == (ASSET, ASSET + ".sha256")


def test_release_asset_url_names_a_missing_asset() -> None:
    release = releases.Release(tag="v0.14.0", assets={"other.tar.gz": "https://x"})
    with pytest.raises(PreflightError, match=ASSET):
        releases.asset_url(release, ASSET)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_verify_sha256_ok(tmp_path: Path) -> None:
    archive = _write(tmp_path / ASSET, b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    checksum = _write(tmp_path / "c.sha256", f"{digest}  {ASSET}\n".encode())
    assert releases.verify_sha256(archive, checksum) == digest


def test_verify_sha256_ignores_filename_and_crlf(tmp_path: Path) -> None:
    archive = _write(tmp_path / ASSET, b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    checksum = _write(tmp_path / "c.sha256", f"\n{digest.upper()}  ./{ASSET}\r\n".encode())
    assert releases.verify_sha256(archive, checksum) == digest


def test_verify_sha256_mismatch(tmp_path: Path) -> None:
    archive = _write(tmp_path / ASSET, b"payload")
    checksum = _write(tmp_path / "c.sha256", f"{'0' * 64}  {ASSET}\n".encode())
    with pytest.raises(RunError, match="checksum mismatch"):
        releases.verify_sha256(archive, checksum)


def test_verify_sha256_rejects_garbage(tmp_path: Path) -> None:
    archive = _write(tmp_path / ASSET, b"payload")
    checksum = _write(tmp_path / "c.sha256", b"not a checksum\n")
    with pytest.raises(RunError, match="no SHA-256"):
        releases.verify_sha256(archive, checksum)


def _tarball(path: Path, members: list[tuple[str, bytes | None, str | None]]) -> Path:
    """members: (name, data or None, symlink target or None)."""
    with tarfile.open(path, "w:gz") as tar:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link
                tar.addfile(info)
            else:
                assert data is not None
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    return path


DIR = "rusty-chess-v0.14.0-x86_64-unknown-linux-gnu"


def test_extract_binary_ok(tmp_path: Path) -> None:
    archive = _tarball(
        tmp_path / ASSET,
        [
            (f"{DIR}/rusty-chess", b"\x7fELF", None),
            (f"{DIR}/LICENSE", b"MIT", None),
            (f"{DIR}/README.md", b"#", None),
        ],
    )
    dest = tmp_path / "out"
    binary = releases.extract_binary(archive, "rusty-chess", dest)
    assert binary == dest / "rusty-chess"
    assert binary.read_bytes() == b"\x7fELF"
    assert binary.stat().st_mode & 0o777 == 0o755
    assert sorted(p.name for p in dest.iterdir()) == ["rusty-chess"]


def test_extract_rejects_symlink(tmp_path: Path) -> None:
    archive = _tarball(tmp_path / ASSET, [(f"{DIR}/rusty-chess", None, "/bin/sh")])
    with pytest.raises(RunError, match="regular file"):
        releases.extract_binary(archive, "rusty-chess", tmp_path / "out")


def test_extract_rejects_path_traversal(tmp_path: Path) -> None:
    archive = _tarball(
        tmp_path / ASSET,
        [(f"{DIR}/rusty-chess", b"x", None), ("../escape", b"x", None)],
    )
    with pytest.raises(RunError, match="unsafe member"):
        releases.extract_binary(archive, "rusty-chess", tmp_path / "out")


def test_extract_rejects_missing_binary(tmp_path: Path) -> None:
    archive = _tarball(tmp_path / ASSET, [(f"{DIR}/LICENSE", b"MIT", None)])
    with pytest.raises(RunError, match="no rusty-chess"):
        releases.extract_binary(archive, "rusty-chess", tmp_path / "out")


def test_extract_rejects_two_binaries(tmp_path: Path) -> None:
    archive = _tarball(
        tmp_path / ASSET,
        [(f"{DIR}/rusty-chess", b"a", None), ("other/rusty-chess", b"b", None)],
    )
    with pytest.raises(RunError, match="more than one"):
        releases.extract_binary(archive, "rusty-chess", tmp_path / "out")


def test_download_writes_the_bytes(tmp_path: Path) -> None:
    def fetch(url: str, headers: dict[str, str]) -> bytes:
        assert url == "https://x/asset"
        return b"bytes"

    target = releases.download("https://x/asset", tmp_path / "a", fetch=fetch)
    assert target.read_bytes() == b"bytes"


# -- waiting for assets: releases are published before their assets are attached --

TAG_URL = "https://api.github.com/repos/jwallace145/rusty-chess-v2/releases/tags/v0.17.0"
NAMES = (
    "rusty-chess-v0.17.0-x86_64-unknown-linux-gnu.tar.gz",
    "rusty-chess-v0.17.0-x86_64-unknown-linux-gnu.tar.gz.sha256",
)


def _tag_fetch(responses: list[list[str]]) -> tuple[releases.Fetch, list[str]]:
    """Each call returns the next asset-name list (the last one repeats)."""
    calls: list[str] = []

    def fetch(url: str, headers: dict[str, str]) -> bytes:
        calls.append(url)
        names = responses[min(len(calls) - 1, len(responses) - 1)]
        return json.dumps(
            {
                "tag_name": "v0.17.0",
                "assets": [{"name": n, "browser_download_url": f"https://dl/{n}"} for n in names],
            }
        ).encode()

    return fetch, calls


def test_wait_for_assets_returns_at_once_when_attached() -> None:
    fetch, calls = _tag_fetch([list(NAMES)])
    sleeps: list[float] = []
    release = releases.wait_for_assets(
        "jwallace145/rusty-chess-v2", "v0.17.0", NAMES, 1800, logging.getLogger("t"),
        fetch=fetch, sleep=sleeps.append,
    )  # fmt: skip
    assert set(NAMES) <= set(release.assets)
    assert calls == [TAG_URL]
    assert sleeps == []


def test_wait_for_assets_waits_until_they_are_attached() -> None:
    fetch, calls = _tag_fetch([[], [NAMES[0]], list(NAMES)])
    sleeps: list[float] = []
    clock = iter(range(0, 10_000, 15))
    release = releases.wait_for_assets(
        "jwallace145/rusty-chess-v2", "v0.17.0", NAMES, 1800, logging.getLogger("t"),
        fetch=fetch, sleep=sleeps.append, clock=lambda: float(next(clock)), poll_seconds=15,
    )  # fmt: skip
    assert release.assets[NAMES[1]] == f"https://dl/{NAMES[1]}"
    assert len(calls) == 3
    assert sleeps == [15, 15]


def test_wait_for_assets_gives_up_naming_what_is_missing() -> None:
    fetch, _ = _tag_fetch([[NAMES[1]]])
    clock = iter(range(0, 100_000, 60))
    with pytest.raises(
        PreflightError,
        match=r"still has no rusty-chess-v0\.17\.0-x86_64-unknown-linux-gnu\.tar\.gz after 30m",
    ):
        releases.wait_for_assets(
            "jwallace145/rusty-chess-v2", "v0.17.0", NAMES, 1800, logging.getLogger("t"),
            fetch=fetch, sleep=lambda _: None, clock=lambda: float(next(clock)),
        )  # fmt: skip
