from __future__ import annotations

import hashlib
import logging
from pathlib import Path, PurePosixPath

import boto3
import pytest
from moto import mock_aws

from jwall_engineering_sites.errors import PreflightError
from jwall_engineering_sites.sync import (
    LocalFile,
    RemoteObject,
    content_type_for,
    delete_prefix,
    execute_sync,
    is_html,
    list_remote,
    plan_sync,
    scan_local,
)

CACHE = "public, max-age=3600"
IMMUTABLE = "public, max-age=31536000, immutable"


def test_scan_local_headers(dist: Path) -> None:
    files = {f.rel_path: f for f in scan_local(dist, CACHE, IMMUTABLE, ["_astro/**"])}
    assert set(files) == {"index.html", "about.html", "_astro/app.abc123.js", "robots.txt"}
    assert files["index.html"].content_type == "text/html; charset=utf-8"
    assert files["index.html"].cache_control == CACHE
    assert files["_astro/app.abc123.js"].cache_control == IMMUTABLE
    assert files["_astro/app.abc123.js"].content_type == "text/javascript; charset=utf-8"
    assert files["robots.txt"].md5 == hashlib.md5(b"User-agent: *").hexdigest()


def test_scan_local_requires_index(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    with pytest.raises(PreflightError, match=r"index\.html"):
        scan_local(tmp_path / "empty", CACHE, IMMUTABLE, [])
    with pytest.raises(PreflightError, match="not a directory"):
        scan_local(tmp_path / "missing", CACHE, IMMUTABLE, [])


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.html", "text/html; charset=utf-8"),
        ("a.css", "text/css; charset=utf-8"),
        ("a.webmanifest", "application/manifest+json"),
        ("a.woff2", "font/woff2"),
        ("a.wasm", "application/wasm"),
        ("a.png", "image/png"),
        ("a.unknownext", "application/octet-stream"),
    ],
)
def test_content_types(name: str, expected: str) -> None:
    assert content_type_for(PurePosixPath(name)) == expected


def test_is_html() -> None:
    assert is_html("index.html")
    assert is_html("docs/guide")  # extensionless pretty URL
    assert not is_html("app.js")


def _local(rel: str, md5: str, size: int = 1) -> LocalFile:
    return LocalFile(rel, Path(rel), size, md5, "text/plain", CACHE)


def test_plan_sync_is_pure_and_exact() -> None:
    local = [_local("index.html", "aaa", 10), _local("new.html", "bbb"), _local("same.txt", "ccc")]
    remote = [
        RemoteObject("prod/index.html", '"old"'),
        RemoteObject("prod/same.txt", '"ccc"'),
        RemoteObject("prod/gone.txt", '"ddd"'),
        RemoteObject("prod/multipart.bin", '"abc-3"'),
    ]
    plan = plan_sync("prod/", local, remote)
    assert [f.rel_path for f in plan.upload] == ["index.html", "new.html"]
    assert [f.rel_path for f in plan.skip] == ["same.txt"]
    assert plan.delete == ["prod/gone.txt", "prod/multipart.bin"]
    assert plan.upload_bytes == 11


def test_plan_sync_requires_trailing_slash() -> None:
    with pytest.raises(ValueError, match="must end with /"):
        plan_sync("prod", [], [])


@mock_aws
def test_round_trip_uploads_html_last_and_deletes(dist: Path, logger: logging.Logger) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="jwall-engineering-guitar-knowledge-base")
    bucket = "jwall-engineering-guitar-knowledge-base"
    s3.put_object(Bucket=bucket, Key="prod/stale.html", Body=b"old")
    s3.put_object(Bucket=bucket, Key="previews/pr-1/index.html", Body=b"other prefix")

    local = scan_local(dist, CACHE, IMMUTABLE, ["_astro/**"])
    plan = plan_sync("prod/", local, list_remote(s3, bucket, "prod/"))
    result = execute_sync(s3, bucket, plan, logger)

    assert (result.uploaded, result.unchanged, result.deleted) == (4, 0, 1)
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix="prod/")["Contents"])
    assert keys == [
        "prod/_astro/app.abc123.js",
        "prod/about.html",
        "prod/index.html",
        "prod/robots.txt",
    ]
    head = s3.head_object(Bucket=bucket, Key="prod/_astro/app.abc123.js")
    assert head["CacheControl"] == IMMUTABLE
    assert head["ContentType"] == "text/javascript; charset=utf-8"
    # the other prefix is untouched
    assert s3.head_object(Bucket=bucket, Key="previews/pr-1/index.html")

    # a second sync is a no-op
    plan2 = plan_sync("prod/", local, list_remote(s3, bucket, "prod/"))
    assert not plan2.upload and not plan2.delete and len(plan2.skip) == 4


@mock_aws
def test_delete_prefix_is_idempotent(logger: logging.Logger) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    bucket = "jwall-engineering-guitar-knowledge-base"
    s3.create_bucket(Bucket=bucket)
    s3.put_object(Bucket=bucket, Key="previews/pr-9/index.html", Body=b"x")
    s3.put_object(Bucket=bucket, Key="previews/pr-10/index.html", Body=b"y")
    assert delete_prefix(s3, bucket, "previews/pr-9/", logger) == 1
    assert delete_prefix(s3, bucket, "previews/pr-9/", logger) == 0
    assert s3.head_object(Bucket=bucket, Key="previews/pr-10/index.html")
