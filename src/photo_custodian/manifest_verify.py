"""Standard-library verification for a Photo Custodian working-copy manifest."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from contextlib import ExitStack
from pathlib import Path

MAX_ENTRIES = 1000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _json(data: object) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(_json(payload)).hexdigest()


def file_fingerprint(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns,
            metadata.st_dev, metadata.st_ino)


def read_digest(root_fd: int, relative: bytes, *, expected: tuple | None = None) -> tuple[int, str]:
    parts = relative.split(b"/")
    if len(parts) > 256 or b"\x00" in relative or any(part in (b"", b".", b"..") for part in parts):
        raise ValueError("Manifest path is not a confined relative path.")
    with ExitStack() as opened:
        parent = root_fd
        for part in parts[:-1]:
            parent = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
            opened.callback(os.close, parent)
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=parent)
        source = opened.enter_context(os.fdopen(descriptor, "rb"))
        before = os.fstat(source.fileno())
        fingerprint = file_fingerprint(before)
        if not stat.S_ISREG(before.st_mode) or (expected is not None and fingerprint != expected):
            raise ValueError("File is not a matching regular file.")
        digest = hashlib.sha256()
        count = 0
        while block := source.read(1024 * 1024):
            digest.update(block)
            count += len(block)
        current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (count != before.st_size or file_fingerprint(os.fstat(source.fileno())) != fingerprint
                or file_fingerprint(current) != fingerprint):
            raise ValueError("File changed while being read.")
    return count, digest.hexdigest()


def _digest_text(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _unique_object(items: list[tuple[str, object]]) -> dict:
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("Duplicate manifest field.")
    return result


def read_manifest(path: Path) -> tuple[dict, str]:
    descriptor = os.open(path.expanduser(), _FILE_FLAGS)
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("Manifest must be a regular file.")
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest exceeds the size limit.")
    try:
        envelope = json.loads(raw, object_pairs_hook=_unique_object)
        payload = envelope["payload"]
        digest = envelope["payload_sha256"]
        entries = payload["files"]
        if (
            set(payload) != {"schema_version", "purpose", "source_id", "files"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or payload["purpose"] != "working-copy-byte-manifest"
            or type(payload["source_id"]) is not int or payload["source_id"] < 1
            or not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES
            or not _digest_text(digest)
        ):
            raise ValueError
        previous_id = 0
        paths = set()
        for entry in entries:
            if (
                set(entry) != {"file_id", "relative_path_b64", "size_bytes", "sha256"}
                or type(entry["file_id"]) is not int or entry["file_id"] <= previous_id
                or type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0
                or not _digest_text(entry["sha256"])
            ):
                raise ValueError
            relative = base64.b64decode(entry["relative_path_b64"], validate=True)
            if not relative or relative in paths:
                raise ValueError
            paths.add(relative)
            previous_id = entry["file_id"]
        if payload_digest(payload) != digest:
            raise ValueError
    except (TypeError, KeyError, ValueError, RecursionError, UnicodeError):
        raise ValueError("Manifest payload or digest is invalid.") from None
    return payload, digest


def verify(args) -> int:
    payload, digest = read_manifest(args.manifest)
    expected = args.expected_sha256
    if expected is not None and (not _digest_text(expected) or expected != digest):
        raise ValueError("Manifest does not match the independently supplied digest.")
    root = os.open(args.root.expanduser(), _DIRECTORY_FLAGS)
    results = []
    try:
        for entry in payload["files"]:
            relative = base64.b64decode(entry["relative_path_b64"], validate=True)
            try:
                size, actual = read_digest(root, relative)
            except (OSError, ValueError):
                status = "unavailable"
            else:
                status = "match" if (size, actual) == (entry["size_bytes"], entry["sha256"]) else "mismatch"
            results.append({"file_id": entry["file_id"], "status": status})
    finally:
        os.close(root)
    print(json.dumps({
        "payload_sha256": digest, "external_digest_matched": expected is not None, "files": results,
    }, indent=2))
    return int(any(item["status"] != "match" for item in results))
