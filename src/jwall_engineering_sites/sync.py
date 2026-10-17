"""Syncing a built site into an S3 prefix with boto3; no AWS CLI in the image.

Planning is a pure function of the local and remote listings so it can be
unit-tested without AWS. Execution uploads assets first and HTML last, so a
page never references an asset that is not there yet, and deletes at the end.
"""

from __future__ import annotations

import hashlib
import mimetypes
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from logging import Logger
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .errors import PreflightError, RunError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

#: Types `mimetypes` gets wrong or does not know on a slim image.
CONTENT_TYPE_OVERRIDES = {
    ".avif": "image/avif",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".ico": "image/x-icon",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
    ".webp": "image/webp",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".xml": "application/xml; charset=utf-8",
}
DEFAULT_CONTENT_TYPE = "application/octet-stream"


def content_type_for(path: PurePosixPath) -> str:
    override = CONTENT_TYPE_OVERRIDES.get(path.suffix.lower())
    if override:
        return override
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or DEFAULT_CONTENT_TYPE


def is_html(key: str) -> bool:
    return key.endswith((".html", ".htm")) or "." not in PurePosixPath(key).name


@dataclass(frozen=True)
class LocalFile:
    rel_path: str
    path: Path
    size: int
    md5: str
    content_type: str
    cache_control: str


@dataclass(frozen=True)
class RemoteObject:
    key: str
    etag: str


@dataclass
class SyncPlan:
    prefix: str
    upload: list[LocalFile] = field(default_factory=list)
    skip: list[LocalFile] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)

    @property
    def upload_bytes(self) -> int:
        return sum(item.size for item in self.upload)


@dataclass
class SyncResult:
    uploaded: int
    unchanged: int
    deleted: int
    bytes_uploaded: int


def scan_local(
    dist: Path,
    cache_control: str,
    immutable_cache_control: str,
    immutable_paths: Sequence[str],
) -> list[LocalFile]:
    """Every file under `dist` with its headers decided. Sorted by path."""
    if not dist.is_dir():
        raise PreflightError(f"{dist} is not a directory")
    if not (dist / "index.html").is_file():
        raise PreflightError(f"{dist} does not contain index.html; is it the built site?")
    files: list[LocalFile] = []
    for path in sorted(p for p in dist.rglob("*") if p.is_file()):
        rel = PurePosixPath(path.relative_to(dist).as_posix())
        immutable = any(rel.full_match(pattern) for pattern in immutable_paths)
        data = path.read_bytes()
        files.append(
            LocalFile(
                rel_path=str(rel),
                path=path,
                size=len(data),
                md5=hashlib.md5(data).hexdigest(),
                content_type=content_type_for(rel),
                cache_control=immutable_cache_control if immutable else cache_control,
            )
        )
    return files


def plan_sync(prefix: str, local: Iterable[LocalFile], remote: Iterable[RemoteObject]) -> SyncPlan:
    """Decide uploads, skips and deletes. Pure.

    An object whose ETag is not a plain MD5 (a multipart upload from an earlier
    tool) never matches, so it is re-uploaded once; after that every object is
    single-part and comparisons are exact.
    """
    if not prefix.endswith("/"):
        raise ValueError("prefix must end with /")
    remote_etags = {obj.key: obj.etag.strip('"') for obj in remote}
    plan = SyncPlan(prefix=prefix)
    seen: set[str] = set()
    for item in local:
        key = prefix + item.rel_path
        seen.add(key)
        if remote_etags.get(key) == item.md5:
            plan.skip.append(item)
        else:
            plan.upload.append(item)
    plan.delete = sorted(key for key in remote_etags if key not in seen)
    return plan


def list_remote(s3: S3Client, bucket: str, prefix: str) -> list[RemoteObject]:
    objects: list[RemoteObject] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            objects.append(RemoteObject(key=obj["Key"], etag=obj["ETag"]))
    return objects


def execute_sync(s3: S3Client, bucket: str, plan: SyncPlan, logger: Logger) -> SyncResult:
    """Upload non-HTML first, HTML last, then delete. Single-part uploads only."""
    ordered = sorted(plan.upload, key=lambda item: (is_html(item.rel_path), item.rel_path))
    for item in ordered:
        key = plan.prefix + item.rel_path
        logger.debug(
            "upload %s (%s, %s, %d bytes)", key, item.content_type, item.cache_control, item.size
        )
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=item.path.read_bytes(),
                ContentType=item.content_type,
                CacheControl=item.cache_control,
            )
        except Exception as exc:  # boto3 raises several ClientError subclasses
            raise RunError(f"upload of s3://{bucket}/{key} failed: {exc}") from exc
    for batch in _batches(plan.delete, 1000):
        logger.debug("delete %d objects", len(batch))
        try:
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
        except Exception as exc:
            raise RunError(f"delete under s3://{bucket}/{plan.prefix} failed: {exc}") from exc
    result = SyncResult(
        uploaded=len(plan.upload),
        unchanged=len(plan.skip),
        deleted=len(plan.delete),
        bytes_uploaded=plan.upload_bytes,
    )
    logger.info(
        "synced s3://%s/%s: %d uploaded (%d bytes), %d unchanged, %d deleted",
        bucket,
        plan.prefix,
        result.uploaded,
        result.bytes_uploaded,
        result.unchanged,
        result.deleted,
    )
    return result


def delete_prefix(s3: S3Client, bucket: str, prefix: str, logger: Logger) -> int:
    """Delete every object under a prefix. Returns the count. Idempotent."""
    keys = [obj.key for obj in list_remote(s3, bucket, prefix)]
    for batch in _batches(keys, 1000):
        try:
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
        except Exception as exc:
            raise RunError(f"delete under s3://{bucket}/{prefix} failed: {exc}") from exc
    logger.info("removed %d objects under s3://%s/%s", len(keys), bucket, prefix)
    return len(keys)


def _batches(items: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
