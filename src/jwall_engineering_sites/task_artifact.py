#!/usr/bin/python3
"""task-artifact: checksummed, write-once S3 transfers on a task instance.

Delivered to /usr/local/bin by the task script, so it runs on the instance's
Python 3.9 with only the standard library. Exits 0 on success, 1 on failure,
2 on bad usage and 4 if the target already exists.

`aws s3 cp --no-overwrite` exits 0 and writes nothing when the key exists, so a
write is proven by reading back the unique `upload-id` it set. S3 keeps no
full-object SHA-256 for multipart uploads, so ours travels in the metadata.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

USAGE = 2
EXISTS = 4
CHUNK = 1024 * 1024
MANIFEST = "_manifest.json"
CLIS = ("aws", "aws2")
MIN_CLI = (2, 33, 0)
SHA256 = re.compile(r"[0-9a-f]{64}")


class ArtifactError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def run_cli(binary: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "AWS_PAGER": ""},
    )


@functools.cache
def find_cli() -> str:
    """The first CLI new enough for --no-overwrite. Some images name v2 `aws2`."""
    found = []
    for binary in CLIS:
        try:
            result = run_cli(binary, "--version")
        except OSError:
            found.append(f"{binary}: not found")
            continue
        match = re.match(r"aws-cli/(\d+)\.(\d+)\.(\d+)", result.stdout + result.stderr)
        if result.returncode == 0 and match:
            if tuple(int(part) for part in match.groups()) >= MIN_CLI:
                return binary
            found.append(f"{binary}: {match.group(0)}")
        else:
            found.append(f"{binary}: no readable version")
    needed = ".".join(str(part) for part in MIN_CLI)
    raise ArtifactError(f"needs AWS CLI {needed} or later; found {', '.join(found)}")


def aws(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the CLI in the task's region: the instance has no default one."""
    region = os.environ.get("TASK_REGION")
    return run_cli(find_cli(), *args, *(("--region", region) if region else ()))


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def parse_object(uri: str) -> tuple[str, str]:
    match = re.fullmatch(r"s3://([^/]+)/(.+)", uri)
    if not match or uri.endswith("/"):
        raise ArtifactError(f"not an object URI (s3://BUCKET/KEY): {uri}", USAGE)
    return match.group(1), match.group(2)


def parse_prefix(uri: str) -> tuple[str, str]:
    match = re.fullmatch(r"s3://([^/]+)/(.*)", uri)
    if not match or not uri.endswith("/"):
        raise ArtifactError(f"not a prefix URI (s3://BUCKET/PREFIX/, with the /): {uri}", USAGE)
    return match.group(1), match.group(2)


def head(bucket: str, key: str) -> dict[str, Any] | None:
    """The object's head-object answer, or None on a 404. S3 answers 403 for a
    missing key when ListBucket doesn't apply, so 404 alone can't prove absence:
    use `exists` where absence matters."""
    result = aws("s3api", "head-object", "--bucket", bucket, "--key", key, "--output", "json")
    if result.returncode == 0:
        answer: dict[str, Any] = json.loads(result.stdout)
        return answer
    if "(404)" in result.stderr:
        return None
    raise ArtifactError(
        f"cannot read s3://{bucket}/{key}; it may not exist, or task-artifact needs read "
        f"on the prefix: "
        f"{result.stderr.strip()}"
    )


def list_sizes(bucket: str, prefix: str) -> dict[str, int]:
    result = aws(
        "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix, "--output", "json"
    )
    if result.returncode != 0:
        raise ArtifactError(
            f"cannot list s3://{bucket}/{prefix}; task-artifact needs read on the prefix: "
            f"{result.stderr.strip()}"
        )
    listing = (json.loads(result.stdout) if result.stdout.strip() else None) or {}
    return {entry["Key"]: entry["Size"] for entry in listing.get("Contents") or []}


def exists(bucket: str, key: str) -> bool:
    return key in list_sizes(bucket, key)


def upload(path: str, bucket: str, key: str) -> str:
    """Write a new object or raise; returns its SHA-256."""
    uri = f"s3://{bucket}/{key}"
    find_cli()  # before hashing what may be a very large file
    digest = sha256_file(path)
    size = os.path.getsize(path)
    upload_id = uuid.uuid4().hex
    copy = aws(
        "s3",
        "cp",
        "--no-overwrite",
        "--only-show-errors",
        "--metadata",
        f"sha256={digest},size={size},upload-id={upload_id}",
        os.path.abspath(path),
        uri,
    )
    if copy.returncode != 0:
        raise ArtifactError(f"uploading to {uri} failed: {copy.stderr.strip()}")
    written = head(bucket, key)
    if written is None:
        raise ArtifactError(f"cannot read back {uri}; task-artifact needs read on the prefix")
    metadata = written.get("Metadata") or {}
    if metadata.get("upload-id") != upload_id:
        raise ArtifactError(f"{uri} already exists; it was left as it is", EXISTS)
    if (
        written.get("ContentLength") != size
        or metadata.get("sha256") != digest
        or metadata.get("size") != str(size)
    ):
        raise ArtifactError(f"{uri} does not match what was uploaded")
    return digest


def download(bucket: str, key: str, dest: str, digest: str, size: int | None = None) -> None:
    """Download to a temporary name beside `dest`, verify, then rename."""
    uri = f"s3://{bucket}/{key}"
    dest = os.path.abspath(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    temporary = f"{dest}.{uuid.uuid4().hex}.part"
    try:
        copy = aws("s3", "cp", "--only-show-errors", uri, temporary)
        if copy.returncode != 0:
            raise ArtifactError(f"downloading {uri} failed: {copy.stderr.strip()}")
        if sha256_file(temporary) != digest:
            raise ArtifactError(f"{uri} does not match its SHA-256 {digest}")
        if size is not None and os.path.getsize(temporary) != size:
            raise ArtifactError(f"{uri} is not the expected {size} bytes")
        os.replace(temporary, dest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def recorded_digest(uri: str, answer: dict[str, Any]) -> str:
    digest = (answer.get("Metadata") or {}).get("sha256", "")
    if not SHA256.fullmatch(digest):
        raise ArtifactError(f"{uri} has no sha256 metadata; it was not written by task-artifact")
    return str(digest)


def check_relative(path: Any, code: int) -> None:
    """A manifest path: relative, with no `..`, and no segment named as a manifest."""
    parts = path.split("/") if isinstance(path, str) else [""]
    if any(part in ("", ".", "..") or "\0" in part for part in parts) or MANIFEST in parts:
        raise ArtifactError(f"not a safe path relative to the prefix: {path!r}", code)


def put(args: argparse.Namespace) -> None:
    bucket, key = parse_object(args.uri)
    digest = upload(args.file, bucket, key)
    print(f"{digest}  {args.uri}")


def get(args: argparse.Namespace) -> None:
    bucket, key = parse_object(args.uri)
    answer = head(bucket, key)
    if answer is None:
        raise ArtifactError(f"{args.uri} does not exist")
    digest = recorded_digest(args.uri, answer)
    size = (answer.get("Metadata") or {}).get("size")
    download(bucket, key, args.file, digest, int(size) if size is not None else None)


def read_expected(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            expected = [line for line in handle.read().splitlines() if line.strip()]
    except OSError as exc:
        raise ArtifactError(f"cannot read --expect file: {exc}", USAGE) from exc
    if not expected:
        raise ArtifactError(f"--expect file {path} names no files", USAGE)
    if len(set(expected)) != len(expected):
        raise ArtifactError(f"--expect file {path} repeats a path", USAGE)
    for entry in expected:
        check_relative(entry, USAGE)
    return expected


def read_metadata(path: str | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            metadata = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ArtifactError(f"cannot read --metadata file: {exc}", USAGE) from exc
    if not isinstance(metadata, dict):
        raise ArtifactError(f"--metadata file {path} is not a JSON object", USAGE)
    return metadata


def verify_expected(
    bucket: str, prefix: str, expected: list[str], sizes: dict[str, int]
) -> list[dict[str, Any]]:
    """Each expected file's manifest entry; raises naming every file that fails."""
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    for path in sorted(expected):
        key = prefix + path
        answer = head(bucket, key) if key in sizes else None
        if answer is None:
            problems.append(f"{path}: missing")
            continue
        metadata = answer.get("Metadata") or {}
        digest = metadata.get("sha256", "")
        if not SHA256.fullmatch(digest):
            problems.append(f"{path}: no sha256 metadata")
        elif metadata.get("size") != str(answer.get("ContentLength")):
            problems.append(f"{path}: size metadata {metadata.get('size')} is not the object's")
        else:
            entries.append({"path": path, "size": answer["ContentLength"], "sha256": digest})
    if problems:
        raise ArtifactError("not publishing; " + "; ".join(problems))
    return entries


def publish(args: argparse.Namespace) -> None:
    bucket, prefix = parse_prefix(args.uri)
    expected = read_expected(args.expect)
    metadata = read_metadata(args.metadata)
    key = prefix + MANIFEST
    uri = f"s3://{bucket}/{key}"
    sizes = list_sizes(bucket, prefix)
    if key in sizes:
        raise ArtifactError(f"{uri} already exists; this run is already published", EXISTS)
    manifest = {
        "schema": 1,
        "prefix": args.uri,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "run_id": os.environ.get("TASK_RUN_ID"),
        "task_id": os.environ.get("TASK_ID"),
        "commit": os.environ.get("TASK_COMMIT"),
        "files": verify_expected(bucket, prefix, expected, sizes),
        "metadata": metadata,
    }
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, MANIFEST)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        upload(path, bucket, key)
    print(uri)


def read_manifest(bucket: str, prefix: str) -> list[dict[str, Any]]:
    """The manifest's files, each checked in shape, from a verified download."""
    key = prefix + MANIFEST
    uri = f"s3://{bucket}/{key}"
    answer = head(bucket, key) if exists(bucket, key) else None
    if answer is None:
        raise ArtifactError(f"{uri} does not exist; the run is not complete")
    digest = recorded_digest(uri, answer)
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, MANIFEST)
        download(bucket, key, path, digest)
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or manifest.get("schema") != 1 or not isinstance(files, list):
        raise ArtifactError(f"{uri} is not a schema 1 manifest")
    if not files:
        raise ArtifactError(f"{uri} lists no files")
    for entry in files:
        if (
            not isinstance(entry, dict)
            or type(entry.get("size")) is not int
            or not isinstance(entry.get("sha256"), str)
            or "path" not in entry
        ):
            raise ArtifactError(f"{uri} has a malformed entry: {entry!r}")
    return files


def get_run(args: argparse.Namespace) -> None:
    bucket, prefix = parse_prefix(args.uri)
    files = read_manifest(bucket, prefix)
    root = os.path.realpath(args.dir)
    # Every path first: nothing is downloaded from a manifest with a bad one.
    targets: dict[str, dict[str, Any]] = {}
    for entry in files:
        check_relative(entry["path"], 1)
        target = os.path.realpath(os.path.join(root, entry["path"]))
        if os.path.commonpath([root, target]) != root or target == root:
            raise ArtifactError(f"{entry['path']!r} leaves {args.dir}")
        if target in targets:
            raise ArtifactError(f"{entry['path']!r} is listed twice")
        targets[target] = entry
    os.makedirs(root, exist_ok=True)
    for target, entry in targets.items():
        download(bucket, prefix + entry["path"], target, entry["sha256"], entry["size"])
    print(f"{len(targets)} files verified in {args.dir}")


def list_complete(args: argparse.Namespace) -> None:
    bucket, prefix = parse_prefix(args.uri)
    runs = []
    for key in list_sizes(bucket, prefix):
        parts = key[len(prefix) :].split("/")
        if len(parts) == 2 and parts[1] == MANIFEST:
            runs.append(f"s3://{bucket}/{prefix}{parts[0]}/")
    for run in sorted(runs):
        print(run)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="task-artifact", description=__doc__.split("\n")[0])
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("put", help="upload a new object with its SHA-256")
    command.add_argument("file")
    command.add_argument("uri")
    command.set_defaults(run=put)
    command = commands.add_parser("get", help="download and verify one object")
    command.add_argument("uri")
    command.add_argument("file")
    command.set_defaults(run=get)
    command = commands.add_parser("publish", help="verify a run and write its manifest, last")
    command.add_argument("uri")
    command.add_argument("--expect", required=True, help="file of paths relative to the prefix")
    command.add_argument("--metadata", help="JSON file recorded in the manifest")
    command.set_defaults(run=publish)
    command = commands.add_parser("get-run", help="download and verify a published run")
    command.add_argument("uri")
    command.add_argument("dir")
    command.set_defaults(run=get_run)
    command = commands.add_parser("list-complete", help="print the published runs under a prefix")
    command.add_argument("uri")
    command.set_defaults(run=list_complete)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        args.run(args)
    except ArtifactError as exc:
        print(f"task-artifact: {exc}", file=sys.stderr)
        return exc.code
    except (OSError, ValueError) as exc:
        print(f"task-artifact: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
