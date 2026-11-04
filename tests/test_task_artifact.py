"""The task-artifact helper, run as a subprocess against the fake aws binary."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from tests.conftest import FIXTURES

HELPER = Path(__file__).parents[1] / "src" / "jwall_engineering_sites" / "task_artifact.py"
#: TEST_HELPER_PYTHON runs the suite under another interpreter, such as a real 3.9.
PYTHON = os.environ.get("TEST_HELPER_PYTHON", sys.executable)
BUCKET = "research"
PREFIX = f"s3://{BUCKET}/runs/1/"
MANIFEST_KEY = "runs/1/_manifest.json"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class Env:
    """A fake S3 and a working directory, with the helper's ways in and out."""

    root: Path
    work: Path

    def _environ(self, extra: dict[str, str]) -> dict[str, str]:
        task = {"TASK_RUN_ID", "TASK_ID", "TASK_COMMIT", "TASK_REGION"}
        base = {k: v for k, v in os.environ.items() if k not in task and not k.startswith("FAKE_")}
        return {
            **base,
            "PATH": f"{FIXTURES / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "FAKE_S3_ROOT": str(self.root),
            **extra,
        }

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PYTHON, str(HELPER), *args],
            capture_output=True,
            text=True,
            cwd=self.work,
            env=self._environ(env),
        )

    def seed(self, key: str, data: bytes, metadata: str | None = None) -> None:
        """Put an object in place directly, as any other writer might."""
        source = self.work / "seed.tmp"
        source.write_bytes(data)
        args = ["aws", "s3", "cp", str(source), f"s3://{BUCKET}/{key}"]
        if metadata is not None:
            args[3:3] = ["--metadata", metadata]
        subprocess.run(args, check=True, capture_output=True, env=self._environ({}))
        source.unlink()

    def seed_verified(self, key: str, data: bytes) -> None:
        self.seed(key, data, f"sha256={_sha(data)},size={len(data)}")

    def body(self, key: str) -> bytes | None:
        path = self.root / BUCKET / quote(key, safe="")
        return path.read_bytes() if path.exists() else None

    def metadata(self, key: str) -> dict[str, str]:
        loaded: dict[str, str] = json.loads(
            (self.root / ".meta" / BUCKET / quote(key, safe="")).read_text()
        )
        return loaded

    def calls(self) -> list[list[str]]:
        log = self.root / "calls.log"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def binaries_used(self) -> set[str]:
        """Which CLI ran each transfer: every call but the `--version` probes."""
        log = (self.root / "binaries.log").read_text().splitlines()
        calls = [json.loads(line) for line in log]
        return {call[0] for call in calls if call[1:] != ["--version"]}

    def uploads(self) -> list[str]:
        """The destination of every `s3 cp` that wrote to S3, in order."""
        return [c[-1] for c in self.calls() if c[:2] == ["s3", "cp"] and c[-1].startswith("s3://")]

    def downloads(self) -> list[str]:
        return [c[-2] for c in self.calls() if c[:2] == ["s3", "cp"] and c[-2].startswith("s3://")]

    def file(self, name: str, data: bytes = b"payload") -> Path:
        path = self.work / name
        path.write_bytes(data)
        return path

    def expect(self, *paths: str) -> Path:
        return self.file("expect.txt", "".join(f"{p}\n" for p in paths).encode())

    def manifest(self) -> dict[str, Any]:
        body = self.body(MANIFEST_KEY)
        assert body is not None
        loaded: dict[str, Any] = json.loads(body)
        return loaded

    def seed_manifest(self, files: list[dict[str, Any]], **fields: Any) -> None:
        document = {"schema": 1, "files": files, **fields}
        self.seed_verified(MANIFEST_KEY, json.dumps(document).encode())


@pytest.fixture
def s3(tmp_path: Path) -> Env:
    work = tmp_path / "work"
    work.mkdir()
    return Env(tmp_path / "s3", work)


def _entry(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "size": len(data), "sha256": _sha(data)}


# --- the fake ------------------------------------------------------------------


def test_the_fake_skips_an_existing_key_silently_like_the_real_cli(s3: Env) -> None:
    s3.seed("k", b"first")
    source = s3.file("f", b"second")
    result = subprocess.run(
        ["aws", "s3", "cp", "--no-overwrite", str(source), f"s3://{BUCKET}/k"],
        capture_output=True,
        text=True,
        env=s3._environ({}),
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert s3.body("k") == b"first"


# --- put -----------------------------------------------------------------------


def test_put_then_get_round_trips_with_metadata(s3: Env) -> None:
    data = os.urandom(3 * 1024 * 1024 + 7)
    source = s3.file("data.bin", data)
    put = s3.run("put", str(source), f"s3://{BUCKET}/a/data.bin")
    assert put.returncode == 0, put.stderr
    assert s3.body("a/data.bin") == data
    metadata = s3.metadata("a/data.bin")
    assert metadata["sha256"] == _sha(data)
    assert metadata["size"] == str(len(data))
    assert re.fullmatch(r"[0-9a-f]{32}", metadata["upload-id"])

    dest = s3.work / "out" / "copy.bin"
    get = s3.run("get", f"s3://{BUCKET}/a/data.bin", str(dest))
    assert get.returncode == 0, get.stderr
    assert dest.read_bytes() == data


def test_put_uploads_with_no_overwrite_then_reads_back(s3: Env) -> None:
    source = s3.file("f", b"x")
    assert s3.run("put", str(source), f"s3://{BUCKET}/k").returncode == 0
    work = [c for c in s3.calls() if c != ["--version"]]
    assert [c[:2] for c in work] == [["s3", "cp"], ["s3api", "head-object"]]
    cp = work[0]
    assert "--no-overwrite" in cp and "--only-show-errors" in cp
    assert cp.count("--metadata") == 1
    assert cp[-2:] == [str(source), f"s3://{BUCKET}/k"]


def _exercise_every_command(s3: Env, **env: str) -> list[list[str]]:
    """Put, get, publish, get-run and list-complete; every AWS call but the version probes."""
    source = s3.file("f")
    expect = s3.expect("a.bin")
    work = s3.work
    steps = (
        ("put", str(source), f"{PREFIX}a.bin"),
        ("get", f"{PREFIX}a.bin", str(work / "got")),
        ("publish", PREFIX, "--expect", str(expect)),
        ("get-run", PREFIX, str(work / "run")),
        ("list-complete", f"s3://{BUCKET}/runs/"),
    )
    for step in steps:
        result = s3.run(*step, **env)
        assert result.returncode == 0, (step, result.stderr)
    return [c for c in s3.calls() if c != ["--version"]]


def test_every_call_names_the_task_region(s3: Env) -> None:
    calls = _exercise_every_command(s3, TASK_REGION="eu-west-1")
    assert {c[0] for c in calls} == {"s3", "s3api"} and len(calls) > 5
    for call in calls:
        assert call.count("--region") == 1, call
        assert call[call.index("--region") + 1] == "eu-west-1", call


@pytest.mark.parametrize("env", [{}, {"TASK_REGION": ""}], ids=["unset", "empty"])
def test_no_region_is_passed_without_a_task_region(s3: Env, env: dict[str, str]) -> None:
    for call in _exercise_every_command(s3, **env):
        assert "--region" not in call, call


def test_the_version_probe_needs_no_region(s3: Env) -> None:
    s3.run("put", str(s3.file("f")), f"s3://{BUCKET}/a", TASK_REGION="eu-west-1")
    assert ["--version"] in s3.calls()


def test_two_puts_use_different_upload_ids(s3: Env) -> None:
    source = s3.file("f")
    s3.run("put", str(source), f"s3://{BUCKET}/a")
    s3.run("put", str(source), f"s3://{BUCKET}/b")
    assert s3.metadata("a")["upload-id"] != s3.metadata("b")["upload-id"]


def test_put_on_an_existing_key_exits_4_and_leaves_the_object(s3: Env) -> None:
    s3.seed_verified("k", b"first")
    before = s3.metadata("k")
    result = s3.run("put", str(s3.file("f", b"second")), f"s3://{BUCKET}/k")
    assert result.returncode == 4
    assert f"s3://{BUCKET}/k" in result.stderr
    assert s3.body("k") == b"first"
    assert s3.metadata("k") == before


def test_put_of_identical_content_onto_an_existing_key_still_exits_4(s3: Env) -> None:
    source = s3.file("f", b"same")
    assert s3.run("put", str(source), f"s3://{BUCKET}/k").returncode == 0
    before = s3.metadata("k")
    assert s3.run("put", str(source), f"s3://{BUCKET}/k").returncode == 4
    assert s3.metadata("k") == before


def test_put_that_fails_mid_upload_leaves_no_object_and_a_retry_succeeds(s3: Env) -> None:
    source = s3.file("f", b"data")
    failed = s3.run("put", str(source), f"s3://{BUCKET}/k", FAKE_AWS_FAIL_ON="s3 cp")
    assert failed.returncode == 1
    assert s3.body("k") is None
    assert s3.run("put", str(source), f"s3://{BUCKET}/k").returncode == 0
    assert s3.body("k") == b"data"


def test_put_says_it_needs_read_when_it_cannot_read_back(s3: Env) -> None:
    result = s3.run("put", str(s3.file("f")), f"s3://{BUCKET}/k", FAKE_AWS_FAIL_ON="head-object")
    assert result.returncode == 1
    assert "needs read" in result.stderr


def test_put_refuses_an_object_shorter_than_the_file(s3: Env) -> None:
    result = s3.run("put", str(s3.file("f", b"data")), f"s3://{BUCKET}/k", FAKE_AWS_TRUNCATE="k")
    assert result.returncode == 1
    assert "s3://research/k" in result.stderr


@pytest.mark.parametrize("override", [{"sha256": "0" * 64}, {"size": "999"}])
def test_put_refuses_metadata_that_does_not_match(s3: Env, override: dict[str, str]) -> None:
    result = s3.run(
        "put", str(s3.file("f")), f"s3://{BUCKET}/k", FAKE_AWS_HEAD_METADATA=json.dumps(override)
    )
    assert result.returncode == 1


@pytest.mark.parametrize(
    "version",
    ["aws-cli/2.32.9 Python/3.13.0", "aws-cli/1.44.0 Python/3.9", "something else", ""],
)
def test_an_unsupported_or_unreadable_cli_uploads_nothing(s3: Env, version: str) -> None:
    result = s3.run("put", str(s3.file("f")), f"s3://{BUCKET}/k", FAKE_AWS_VERSION=version)
    assert result.returncode == 1
    assert "2.33.0" in result.stderr
    assert s3.uploads() == []
    assert s3.body("k") is None


@pytest.mark.parametrize("version", ["aws-cli/2.33.0 Python/3.13.0", "aws-cli/2.100.0 Python/3"])
def test_a_supported_cli_is_compared_numerically(s3: Env, version: str) -> None:
    result = s3.run("put", str(s3.file("f")), f"s3://{BUCKET}/k", FAKE_AWS_VERSION=version)
    assert result.returncode == 0, result.stderr


def test_a_missing_aws_binary_fails_clearly(s3: Env, tmp_path: Path) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = s3.run("put", str(s3.file("f")), f"s3://{BUCKET}/k", PATH=str(empty))
    assert result.returncode == 1
    assert "aws" in result.stderr


def test_a_key_is_never_shell_interpreted(s3: Env) -> None:
    key = "a b/$(touch pwned); `touch pwned2` 'q'"
    assert s3.run("put", str(s3.file("my file")), f"s3://{BUCKET}/{key}").returncode == 0
    assert s3.body(key) == b"payload"
    assert sorted(p.name for p in s3.work.iterdir()) == ["my file"]


def test_a_relative_file_starting_with_a_dash_is_not_an_option(s3: Env) -> None:
    s3.file("-file", b"data")
    assert s3.run("put", "--", "-file", f"s3://{BUCKET}/k").returncode == 0
    assert s3.body("k") == b"data"


@pytest.mark.parametrize("uri", ["not-s3", "s3://bucket", "s3://bucket/", "s3://bucket/dir/"])
def test_put_and_get_refuse_a_uri_that_names_no_object(s3: Env, uri: str) -> None:
    assert s3.run("put", str(s3.file("f")), uri).returncode == 2
    assert s3.run("get", uri, str(s3.work / "out")).returncode == 2
    assert s3.calls() == []


def test_put_of_a_missing_file_exits_1(s3: Env) -> None:
    assert s3.run("put", str(s3.work / "absent"), f"s3://{BUCKET}/k").returncode == 1
    assert s3.uploads() == []


# --- get -----------------------------------------------------------------------


def test_get_of_a_corrupted_object_exits_1_and_removes_the_temporary(s3: Env) -> None:
    s3.seed_verified("k", b"contents")
    dest = s3.file("existing", b"old")
    result = s3.run("get", f"s3://{BUCKET}/k", str(dest), FAKE_AWS_CORRUPT="k")
    assert result.returncode == 1
    assert dest.read_bytes() == b"old"
    assert [p.name for p in s3.work.iterdir()] == ["existing"]


def test_get_with_no_sha256_metadata_exits_1_without_downloading(s3: Env) -> None:
    s3.seed("k", b"contents")
    dest = s3.work / "dest"
    result = s3.run("get", f"s3://{BUCKET}/k", str(dest))
    assert result.returncode == 1
    assert "sha256" in result.stderr
    assert not dest.exists()
    assert s3.downloads() == []


def test_get_of_a_missing_key_exits_1(s3: Env) -> None:
    assert s3.run("get", f"s3://{BUCKET}/absent", str(s3.work / "dest")).returncode == 1
    assert list(s3.work.iterdir()) == []


def test_get_whose_download_fails_leaves_nothing(s3: Env) -> None:
    s3.seed_verified("k", b"contents")
    result = s3.run("get", f"s3://{BUCKET}/k", str(s3.work / "dest"), FAKE_AWS_FAIL_ON="s3 cp")
    assert result.returncode == 1
    assert list(s3.work.iterdir()) == []


def test_get_replaces_an_existing_file_once_verified(s3: Env) -> None:
    s3.seed_verified("k", b"new")
    dest = s3.file("dest", b"old")
    assert s3.run("get", f"s3://{BUCKET}/k", str(dest)).returncode == 0
    assert dest.read_bytes() == b"new"
    assert [p.name for p in s3.work.iterdir()] == ["dest"]


# --- publish -------------------------------------------------------------------


def _seed_run(s3: Env) -> dict[str, bytes]:
    files = {"model.bin": b"weights", "logs/train log.txt": b"loss"}
    for path, data in files.items():
        s3.seed_verified(f"runs/1/{path}", data)
    return files


def test_publish_writes_the_manifest(s3: Env) -> None:
    files = _seed_run(s3)
    metadata = s3.file("meta.json", b'{"epochs": 3, "nested": {"a": [1]}}')
    result = s3.run(
        "publish",
        PREFIX,
        "--expect",
        str(s3.expect(*files)),
        "--metadata",
        str(metadata),
        TASK_RUN_ID="123-1",
        TASK_ID="train-0",
        TASK_COMMIT="abc",
    )
    assert result.returncode == 0, result.stderr
    manifest = s3.manifest()
    created_at = manifest.pop("created_at")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", created_at)
    assert manifest == {
        "schema": 1,
        "prefix": PREFIX,
        "run_id": "123-1",
        "task_id": "train-0",
        "commit": "abc",
        "files": [_entry(p, files[p]) for p in sorted(files)],
        "metadata": {"epochs": 3, "nested": {"a": [1]}},
    }


def test_publish_records_null_for_an_absent_task_environment(s3: Env) -> None:
    files = _seed_run(s3)
    assert s3.run("publish", PREFIX, "--expect", str(s3.expect(*files))).returncode == 0
    manifest = s3.manifest()
    assert (manifest["run_id"], manifest["task_id"], manifest["commit"]) == (None, None, None)
    assert manifest["metadata"] == {}


def test_publish_writes_the_manifest_last_after_verifying_every_file(s3: Env) -> None:
    files = _seed_run(s3)
    before = len(s3.calls())
    assert s3.run("publish", PREFIX, "--expect", str(s3.expect(*files))).returncode == 0
    calls = s3.calls()[before:]
    assert s3.uploads()[-1] == f"{PREFIX}_manifest.json"
    write = next(i for i, c in enumerate(calls) if c[:2] == ["s3", "cp"])
    heads = [c[c.index("--key") + 1] for c in calls[:write] if c[:2] == ["s3api", "head-object"]]
    assert {f"runs/1/{p}" for p in files} <= set(heads)
    assert "--no-overwrite" in calls[write]
    # Only the read-back follows the write.
    assert [c[1] for c in calls[write + 1 :]] == ["head-object"]


def test_publish_ignores_objects_it_does_not_expect(s3: Env) -> None:
    files = _seed_run(s3)
    s3.seed("runs/1/checkpoints/epoch-1", b"partial")
    assert s3.run("publish", PREFIX, "--expect", str(s3.expect(*files))).returncode == 0
    assert [f["path"] for f in s3.manifest()["files"]] == sorted(files)


def test_publish_with_a_missing_file_exits_1_and_writes_no_manifest(s3: Env) -> None:
    _seed_run(s3)
    result = s3.run("publish", PREFIX, "--expect", str(s3.expect("model.bin", "absent.bin")))
    assert result.returncode == 1
    assert "absent.bin" in result.stderr
    assert s3.body(MANIFEST_KEY) is None


@pytest.mark.parametrize(
    "metadata", [None, "size=7", "sha256=nothex,size=7", f"sha256={'a' * 64},size=8"]
)
def test_publish_refuses_a_file_with_bad_metadata(s3: Env, metadata: str | None) -> None:
    s3.seed("runs/1/model.bin", b"weights", metadata)
    result = s3.run("publish", PREFIX, "--expect", str(s3.expect("model.bin")))
    assert result.returncode == 1
    assert "model.bin" in result.stderr
    assert s3.body(MANIFEST_KEY) is None


def test_a_second_publish_exits_4_and_leaves_the_manifest(s3: Env) -> None:
    files = _seed_run(s3)
    expect = str(s3.expect(*files))
    assert s3.run("publish", PREFIX, "--expect", expect).returncode == 0
    before, metadata = s3.body(MANIFEST_KEY), s3.metadata(MANIFEST_KEY)
    assert s3.run("publish", PREFIX, "--expect", expect).returncode == 4
    assert s3.body(MANIFEST_KEY) == before
    assert s3.metadata(MANIFEST_KEY) == metadata


def test_publish_of_a_published_run_exits_4_even_if_its_files_changed(s3: Env) -> None:
    files = _seed_run(s3)
    assert s3.run("publish", PREFIX, "--expect", str(s3.expect(*files))).returncode == 0
    before = s3.body(MANIFEST_KEY)
    result = s3.run("publish", PREFIX, "--expect", str(s3.expect("gone.bin")))
    assert result.returncode == 4
    assert s3.body(MANIFEST_KEY) == before


def test_publish_that_loses_a_race_for_the_manifest_exits_4(s3: Env) -> None:
    files = _seed_run(s3)
    s3.seed_verified(MANIFEST_KEY, b'{"winner": true}')
    result = s3.run(
        "publish",
        PREFIX,
        "--expect",
        str(s3.expect(*files)),
        FAKE_AWS_HIDE_FROM_LIST=MANIFEST_KEY,
    )
    assert result.returncode == 4
    assert s3.body(MANIFEST_KEY) == b'{"winner": true}'


def test_publish_says_it_needs_read_when_denied(s3: Env) -> None:
    files = _seed_run(s3)
    result = s3.run(
        "publish", PREFIX, "--expect", str(s3.expect(*files)), FAKE_AWS_FAIL_ON="head-object"
    )
    assert result.returncode == 1
    assert "needs read" in result.stderr
    assert s3.uploads()[-1:] != [f"{PREFIX}_manifest.json"]


def test_publish_on_an_unsupported_cli_writes_no_manifest(s3: Env) -> None:
    files = _seed_run(s3)
    result = s3.run(
        "publish",
        PREFIX,
        "--expect",
        str(s3.expect(*files)),
        FAKE_AWS_VERSION="aws-cli/2.32.0 Python/3.13.0",
    )
    assert result.returncode == 1
    assert "2.33.0" in result.stderr
    assert s3.body(MANIFEST_KEY) is None


@pytest.mark.parametrize("content", [b"", b"\n  \n\n"])
def test_an_empty_expect_file_exits_2(s3: Env, content: bytes) -> None:
    _seed_run(s3)
    result = s3.run("publish", PREFIX, "--expect", str(s3.file("expect.txt", content)))
    assert result.returncode == 2
    assert s3.body(MANIFEST_KEY) is None


def test_a_missing_expect_file_exits_2(s3: Env) -> None:
    assert s3.run("publish", PREFIX, "--expect", str(s3.work / "absent")).returncode == 2


@pytest.mark.parametrize(
    "path",
    ["../x", "/x", "a/../b", "_manifest.json", "sub/_manifest.json", "_manifest.json/x", "a//b"],
)
def test_publish_refuses_a_path_get_run_would_refuse(s3: Env, path: str) -> None:
    assert s3.run("publish", PREFIX, "--expect", str(s3.expect(path))).returncode == 2
    assert s3.calls() == []


def test_publish_refuses_a_duplicated_path(s3: Env) -> None:
    assert s3.run("publish", PREFIX, "--expect", str(s3.expect("a", "a"))).returncode == 2


@pytest.mark.parametrize("content", [b"not json", b"[1]"])
def test_publish_refuses_metadata_that_is_not_an_object(s3: Env, content: bytes) -> None:
    files = _seed_run(s3)
    expect = str(s3.expect(*files))
    metadata = str(s3.file("meta.json", content))
    assert s3.run("publish", PREFIX, "--expect", expect, "--metadata", metadata).returncode == 2
    assert s3.body(MANIFEST_KEY) is None


def test_publish_refuses_a_missing_metadata_file(s3: Env) -> None:
    files = _seed_run(s3)
    expect = str(s3.expect(*files))
    absent = str(s3.work / "absent.json")
    assert s3.run("publish", PREFIX, "--expect", expect, "--metadata", absent).returncode == 2


@pytest.mark.parametrize(
    "command",
    [
        ("publish", f"s3://{BUCKET}/runs/1", "--expect", "e"),
        ("get-run", f"s3://{BUCKET}/runs/1", "d"),
        ("list-complete", f"s3://{BUCKET}/runs/1"),
        ("list-complete", "not-s3/"),
    ],
)
def test_a_prefix_must_end_with_a_slash(s3: Env, command: tuple[str, ...]) -> None:
    assert s3.run(*command).returncode == 2
    assert s3.calls() == []


# --- get-run -------------------------------------------------------------------


def test_get_run_downloads_and_verifies_a_published_run(s3: Env) -> None:
    files = _seed_run(s3)
    assert s3.run("publish", PREFIX, "--expect", str(s3.expect(*files))).returncode == 0
    dest = s3.work / "run dir"
    result = s3.run("get-run", PREFIX, str(dest))
    assert result.returncode == 0, result.stderr
    found = {p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    assert found == files


def test_get_run_with_no_manifest_exits_1_and_says_so(s3: Env) -> None:
    _seed_run(s3)
    dest = s3.work / "dest"
    result = s3.run("get-run", PREFIX, str(dest))
    assert result.returncode == 1
    assert "_manifest.json" in result.stderr
    assert not dest.exists()


@pytest.mark.parametrize(
    "evil", ["../evil", "{abs}", "a/../../evil", "..", ".", "", "a//b", "_manifest.json/x"]
)
def test_get_run_validates_every_path_before_downloading_anything(
    s3: Env, tmp_path: Path, evil: str
) -> None:
    outside = tmp_path / "outside-evil"
    good = b"good"
    s3.seed_verified("runs/1/good.txt", good)
    s3.seed_verified("runs/1/evil", b"evil")
    s3.seed_manifest(
        [
            _entry("good.txt", good),
            {"path": evil.format(abs=outside), "size": 4, "sha256": _sha(b"evil")},
        ]
    )
    dest = s3.work / "dest"
    before = sorted(p.name for p in tmp_path.iterdir())
    result = s3.run("get-run", PREFIX, str(dest))
    assert result.returncode == 1
    assert s3.downloads() == [f"{PREFIX}_manifest.json"]
    assert not dest.exists() or not any(dest.iterdir())
    assert not outside.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert not (s3.work / "evil").exists()


def test_get_run_refuses_a_path_through_a_symlink_out_of_the_directory(
    s3: Env, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = s3.work / "dest"
    dest.mkdir()
    (dest / "link").symlink_to(outside)
    s3.seed_verified("runs/1/link/x", b"x")
    s3.seed_manifest([_entry("link/x", b"x")])
    assert s3.run("get-run", PREFIX, str(dest)).returncode == 1
    assert list(outside.iterdir()) == []
    assert s3.downloads() == [f"{PREFIX}_manifest.json"]


def test_get_run_of_a_corrupted_file_exits_1_and_keeps_only_verified_files(s3: Env) -> None:
    s3.seed_verified("runs/1/a", b"aaa")
    s3.seed_manifest([_entry("a", b"aaa")])
    dest = s3.work / "dest"
    assert s3.run("get-run", PREFIX, str(dest), FAKE_AWS_CORRUPT="runs/1/a").returncode == 1
    assert not dest.exists() or list(dest.iterdir()) == []


def test_get_run_verifies_against_the_manifest_not_the_objects_metadata(s3: Env) -> None:
    # The object's metadata is wrong and the manifest is right: it verifies.
    s3.seed("runs/1/a", b"aaa", f"sha256={'0' * 64},size=99")
    s3.seed_manifest([_entry("a", b"aaa")])
    dest = s3.work / "dest"
    result = s3.run("get-run", PREFIX, str(dest))
    assert result.returncode == 0, result.stderr
    assert (dest / "a").read_bytes() == b"aaa"


def test_get_run_refuses_an_object_that_differs_from_the_manifest(s3: Env) -> None:
    # The object's metadata agrees with the object; the manifest does not.
    s3.seed_verified("runs/1/a", b"aaa")
    s3.seed_manifest([_entry("a", b"bbb")])
    dest = s3.work / "dest"
    assert s3.run("get-run", PREFIX, str(dest)).returncode == 1
    assert not (dest / "a").exists()


def test_get_run_refuses_a_size_that_differs_from_the_manifest(s3: Env) -> None:
    s3.seed_verified("runs/1/a", b"aaa")
    s3.seed_manifest([{"path": "a", "size": 4, "sha256": _sha(b"aaa")}])
    dest = s3.work / "dest"
    assert s3.run("get-run", PREFIX, str(dest)).returncode == 1
    assert not (dest / "a").exists()


def test_get_run_refuses_a_corrupted_manifest(s3: Env) -> None:
    s3.seed_verified("runs/1/a", b"aaa")
    s3.seed_manifest([_entry("a", b"aaa")])
    dest = s3.work / "dest"
    assert s3.run("get-run", PREFIX, str(dest), FAKE_AWS_CORRUPT=MANIFEST_KEY).returncode == 1
    assert s3.downloads() == [f"{PREFIX}_manifest.json"]


@pytest.mark.parametrize(
    "document",
    [
        b"not json",
        b"[]",
        b'{"schema": 2, "files": []}',
        b'{"schema": 1}',
        b'{"schema": 1, "files": []}',
        b'{"schema": 1, "files": [{"path": "a"}]}',
        b'{"schema": 1, "files": [{"path": "a", "size": true, "sha256": "x"}]}',
        b'{"schema": 1, "files": ["a"]}',
    ],
)
def test_get_run_refuses_a_manifest_it_cannot_read(s3: Env, document: bytes) -> None:
    s3.seed_verified(MANIFEST_KEY, document)
    assert s3.run("get-run", PREFIX, str(s3.work / "dest")).returncode == 1
    assert s3.downloads() == [f"{PREFIX}_manifest.json"]


def test_get_run_refuses_a_duplicated_path(s3: Env) -> None:
    s3.seed_verified("runs/1/a", b"aaa")
    s3.seed_manifest([_entry("a", b"aaa"), _entry("a", b"aaa")])
    assert s3.run("get-run", PREFIX, str(s3.work / "dest")).returncode == 1
    assert s3.downloads() == [f"{PREFIX}_manifest.json"]


def test_an_unsupported_cli_downloads_nothing_either(s3: Env) -> None:
    s3.seed_verified("runs/1/a", b"aaa")
    s3.seed_manifest([_entry("a", b"aaa")])
    old = "aws-cli/2.0.0 Python/3"
    commands = [
        ("get-run", PREFIX, str(s3.work / "run")),
        ("get", f"s3://{BUCKET}/runs/1/a", str(s3.work / "one")),
        ("list-complete", f"s3://{BUCKET}/runs/"),
    ]
    before = len(s3.calls())
    for command in commands:
        result = s3.run(*command, FAKE_AWS_VERSION=old)
        assert result.returncode == 1, command
        assert "2.33.0" in result.stderr
    assert {tuple(c) for c in s3.calls()[before:]} == {("--version",)}
    assert not (s3.work / "run").exists() and not (s3.work / "one").exists()


# --- list-complete -------------------------------------------------------------


def test_list_complete_prints_only_immediate_sub_prefixes_with_a_manifest(s3: Env) -> None:
    s3.seed("runs/_manifest.json", b"{}")  # the prefix's own, not a run under it
    s3.seed("runs/c/_manifest.json", b"{}")
    s3.seed("runs/a b/_manifest.json", b"{}")
    s3.seed("runs/a b/model", b"m")
    s3.seed("runs/partial/model", b"m")
    s3.seed("runs/deep/er/_manifest.json", b"{}")
    s3.seed("runs/not/x_manifest.json", b"{}")
    s3.seed("other/z/_manifest.json", b"{}")
    result = s3.run("list-complete", f"s3://{BUCKET}/runs/")
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"s3://{BUCKET}/runs/a b/\ns3://{BUCKET}/runs/c/\n"


def test_list_complete_of_nothing_prints_nothing(s3: Env) -> None:
    result = s3.run("list-complete", f"s3://{BUCKET}/runs/")
    assert (result.returncode, result.stdout) == (0, "")


@pytest.mark.parametrize("stdout", ["null", "{}", "", '{"Contents": null}'])
def test_a_listing_with_no_contents_is_an_empty_listing(s3: Env, stdout: str) -> None:
    result = s3.run("list-complete", f"s3://{BUCKET}/runs/", FAKE_AWS_LIST_STDOUT=stdout)
    assert (result.returncode, result.stdout) == (0, ""), result.stderr


def test_list_complete_reports_a_listing_failure(s3: Env) -> None:
    result = s3.run("list-complete", f"s3://{BUCKET}/runs/", FAKE_AWS_FAIL_ON="list-objects-v2")
    assert result.returncode == 1


# --- a caller for whom S3 answers 403 on a missing key -----------------------------


def test_first_and_second_publish_when_a_missing_key_answers_403(s3: Env) -> None:
    files = _seed_run(s3)
    expect = str(s3.expect(*files))
    first = s3.run("publish", PREFIX, "--expect", expect, FAKE_AWS_HEAD_MISSING="403")
    assert first.returncode == 0, first.stderr
    before = s3.body(MANIFEST_KEY)
    assert before is not None
    second = s3.run("publish", PREFIX, "--expect", expect, FAKE_AWS_HEAD_MISSING="403")
    assert second.returncode == 4
    assert s3.body(MANIFEST_KEY) == before


def test_publish_names_a_missing_file_when_a_missing_key_answers_403(s3: Env) -> None:
    _seed_run(s3)
    expect = str(s3.expect("model.bin", "absent.bin"))
    result = s3.run("publish", PREFIX, "--expect", expect, FAKE_AWS_HEAD_MISSING="403")
    assert result.returncode == 1
    assert "absent.bin: missing" in result.stderr
    assert s3.body(MANIFEST_KEY) is None


def test_get_run_with_no_manifest_when_a_missing_key_answers_403(s3: Env) -> None:
    _seed_run(s3)
    dest = s3.work / "dest"
    result = s3.run("get-run", PREFIX, str(dest), FAKE_AWS_HEAD_MISSING="403")
    assert result.returncode == 1
    assert "does not exist" in result.stderr
    assert s3.downloads() == []
    assert not dest.exists()


def test_absence_of_a_manifest_is_not_fooled_by_a_longer_key(s3: Env) -> None:
    files = _seed_run(s3)
    s3.seed(f"{MANIFEST_KEY}.bak", b"{}")
    expect = str(s3.expect(*files))
    assert s3.run("get-run", PREFIX, str(s3.work / "dest")).returncode == 1
    assert s3.run("publish", PREFIX, "--expect", expect).returncode == 0


def test_publish_says_it_needs_read_when_it_cannot_list(s3: Env) -> None:
    files = _seed_run(s3)
    expect = str(s3.expect(*files))
    result = s3.run("publish", PREFIX, "--expect", expect, FAKE_AWS_FAIL_ON="list-objects-v2")
    assert result.returncode == 1
    assert "needs read" in result.stderr
    assert s3.body(MANIFEST_KEY) is None


def test_get_of_a_missing_key_when_it_answers_403_exits_1(s3: Env) -> None:
    result = s3.run(
        "get", f"s3://{BUCKET}/absent", str(s3.work / "dest"), FAKE_AWS_HEAD_MISSING="403"
    )
    assert result.returncode == 1
    assert "may not exist" in result.stderr


# --- which CLI -------------------------------------------------------------------

OLD = "aws-cli/1.44.0 Python/3.9"
GOOD = "aws-cli/2.38.0 Python/3.13.0"


def _every_command(s3: Env, **env: str) -> None:
    """Run all five commands; each must succeed."""
    data = s3.file("data", b"weights")
    key = "runs/1/model.bin"
    expect = str(s3.expect("model.bin"))
    commands = [
        ("put", str(data), f"s3://{BUCKET}/{key}"),
        ("get", f"s3://{BUCKET}/{key}", str(s3.work / "one")),
        ("publish", PREFIX, "--expect", expect),
        ("get-run", PREFIX, str(s3.work / "run")),
        ("list-complete", f"s3://{BUCKET}/runs/"),
    ]
    for command in commands:
        result = s3.run(*command, **env)
        assert result.returncode == 0, (command, result.stderr)
    assert (s3.work / "run" / "model.bin").read_bytes() == b"weights"


def test_a_too_old_aws_falls_back_to_a_good_aws2_for_every_command(s3: Env) -> None:
    _every_command(s3, FAKE_AWS_VERSION=OLD, FAKE_AWS2_VERSION=GOOD)
    assert s3.binaries_used() == {"aws2"}


def test_a_good_aws_is_preferred_over_a_good_aws2(s3: Env) -> None:
    _every_command(s3, FAKE_AWS2_VERSION=GOOD)
    assert s3.binaries_used() == {"aws"}


def test_a_missing_aws_falls_back_to_a_good_aws2(s3: Env, tmp_path: Path) -> None:
    only_aws2 = tmp_path / "only-aws2"
    only_aws2.mkdir()
    (only_aws2 / "aws2").symlink_to(FIXTURES / "bin" / "aws2")
    (only_aws2 / "python3").symlink_to(sys.executable)
    _every_command(s3, PATH=str(only_aws2), FAKE_AWS2_VERSION=GOOD)
    assert s3.binaries_used() == {"aws2"}


def test_both_clis_too_old_fails_naming_both_and_transfers_nothing(s3: Env) -> None:
    source = s3.file("f")
    result = s3.run(
        "put",
        str(source),
        f"s3://{BUCKET}/k",
        FAKE_AWS_VERSION="aws-cli/2.32.9 Python/3",
        FAKE_AWS2_VERSION="aws-cli/1.44.0 Python/3",
    )
    assert result.returncode == 1
    assert "2.33.0" in result.stderr
    assert "aws: aws-cli/2.32.9" in result.stderr
    assert "aws2: aws-cli/1.44.0" in result.stderr
    assert s3.uploads() == []
    assert s3.body("k") is None


def test_the_message_says_which_cli_is_absent(s3: Env) -> None:
    result = s3.run("put", str(s3.file("f")), f"s3://{BUCKET}/k", FAKE_AWS_VERSION=OLD)
    assert result.returncode == 1
    assert "aws: aws-cli/1.44.0" in result.stderr
    assert "aws2:" in result.stderr


# --- usage and the source ------------------------------------------------------


@pytest.mark.parametrize("args", [(), ("unknown",), ("put", "only-one"), ("publish", PREFIX)])
def test_bad_usage_exits_2(s3: Env, args: tuple[str, ...]) -> None:
    assert s3.run(*args).returncode == 2


def test_helper_runs_on_python_39() -> None:
    source = HELPER.read_text()
    assert source.startswith("#!/usr/bin/python3\n")
    tree = ast.parse(source, feature_version=(3, 9))
    assert "annotations" in {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "__future__"
        for alias in node.names
    }
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module is not None
            imported.add(node.module.split(".")[0])
    assert imported <= sys.stdlib_module_names, imported - sys.stdlib_module_names


def test_hashing_reads_in_one_mib_chunks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jwall_engineering_sites import task_artifact

    data = b"x" * (3 * 1024 * 1024 + 1)
    path = tmp_path / "f"
    path.write_bytes(data)
    sizes: list[int] = []

    class Recording:
        def __init__(self, real: Any) -> None:
            self.real = real

        def __enter__(self) -> Recording:
            return self

        def __exit__(self, *_: object) -> None:
            self.real.close()

        def read(self, size: int) -> bytes:
            sizes.append(size)
            chunk: bytes = self.real.read(size)
            return chunk

    monkeypatch.setattr(
        task_artifact, "open", lambda p, mode: Recording(path.open(mode)), raising=False
    )
    assert task_artifact.sha256_file(str(path)) == _sha(data)
    assert set(sizes) == {1024 * 1024}


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced only on Linux")
def test_hash_is_streamed(tmp_path: Path) -> None:
    size = 2 * 1024 * 1024 * 1024
    path = tmp_path / "sparse"
    with path.open("wb") as handle:
        handle.truncate(size)
    expected = hashlib.sha256()
    block = bytes(1024 * 1024)
    for _ in range(size // len(block)):
        expected.update(block)
    probe = (
        "import resource, sys\n"
        "limit = 1024 ** 3\n"
        "resource.setrlimit(resource.RLIMIT_AS, (limit, limit))\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import task_artifact\n"
        "print(task_artifact.sha256_file(sys.argv[2]))\n"
    )
    result = subprocess.run(
        [PYTHON, "-c", probe, str(HELPER.parent), str(path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected.hexdigest()
