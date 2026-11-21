"""Bind catalogued derivative bytes to bounded local staging receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from collections import Counter
from contextlib import ExitStack

from .exact_duplicates import _catalog_fingerprint
from .manifest_verify import _DIRECTORY_FLAGS, _FILE_FLAGS, file_fingerprint, read_digest

_MAX_RECEIPT = 4 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MIME = re.compile(rb"([0-9a-f]{64})/(part-([0-9]{4})\.[a-z0-9]{1,10})")
_TRANSCRIPT = re.compile(rb"(media-([1-9][0-9]{0,18}))/(window-([0-9]{7}))/transcript\.txt")
_IDENTITY_SQL = "json_array(f.size_bytes, f.mtime_ns, f.ctime_ns, f.device, f.inode)"


def _sha(value: object) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError("Invalid receipt hash.")
    return value


def _integer(value: object, lower: int, upper: int) -> int:
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError("Invalid receipt locator.")
    return value


def _object_pairs(pairs: list[tuple[str, object]]) -> dict:
    record = dict(pairs)
    if len(record) != len(pairs):
        raise ValueError("Duplicate receipt field.")
    return record


def _read_receipt(root: int, *parts: bytes) -> tuple[dict, str]:
    with ExitStack() as stack:
        directory = root
        for part in parts[:-1]:
            directory = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(parts[-1], _FILE_FLAGS, dir_fd=directory)
        handle = stack.enter_context(os.fdopen(descriptor, "rb"))
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_RECEIPT:
            raise ValueError("Invalid receipt file.")
        raw = handle.read(_MAX_RECEIPT + 1)
        if (
            len(raw) != before.st_size
            or file_fingerprint(os.fstat(handle.fileno())) != file_fingerprint(before)
            or file_fingerprint(os.stat(parts[-1], dir_fd=directory, follow_symlinks=False))
               != file_fingerprint(before)
        ):
            raise ValueError("Receipt changed while being read.")
    result = json.loads(raw, object_pairs_hook=_object_pairs)
    if not isinstance(result, dict) or type(result.get("schema_version")) is not int or result["schema_version"] != 1:
        raise ValueError("Unsupported receipt schema.")
    return result, hashlib.sha256(raw).hexdigest()


def _mime(root: int, match: re.Match[bytes], size: int) -> dict:
    folder, filename, index_bytes = match.groups()
    record, receipt_hash = _read_receipt(root, folder, b"mime-intake.json")
    name_hex = record["source_eml_name_hex"]
    if not isinstance(name_hex, str) or hashlib.sha256(bytes.fromhex(name_hex)).hexdigest().encode() != folder:
        raise ValueError("MIME receipt points to a different staging directory.")
    entries = record["working_derivatives"]
    if not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError("Invalid MIME entries.")
    matches = [item for item in entries if isinstance(item, dict)
               and item.get("working_filename") == filename.decode("ascii")]
    if len(matches) != 1:
        raise ValueError("MIME part is missing or ambiguous.")
    item = matches[0]
    index = _integer(item.get("part_index"), 1, 1000)
    if index != int(index_bytes) or _integer(item.get("bytes"), 0, 64 * 1024**2) != size:
        raise ValueError("MIME part metadata differs from the catalogue.")
    return {
        "kind": "mime_part", "parent_sha256": _sha(record["source_eml_sha256"]),
        "derivative_sha256": _sha(item["sha256"]), "receipt_sha256": receipt_hash,
        "part_index": index,
    }


def _transcript(root: int, match: re.Match[bytes]) -> dict:
    media, file_id, window, offset = match.groups()
    source, source_receipt_hash = _read_receipt(root, media, b"source.json")
    receipt, receipt_hash = _read_receipt(root, media, window, b"window.json")
    parent = _sha(receipt["source_sha256"])
    model = _sha(receipt["model_sha256"])
    start = _integer(receipt["requested_start_seconds"], 0, 604_800)
    duration = _integer(receipt["requested_duration_seconds"], 1, 300)
    media_id = _integer(source["file_id"], 1, 2**63 - 1)
    source_id = _integer(source["source_id"], 1, 2**63 - 1)
    span = _integer(source["declared_duration_rounded_up"], 1, 604_800)
    if (
        media_id != int(file_id) or start != int(offset) or start % 300
        or start >= span or duration != min(300, span - start)
        or parent != _sha(source["source_sha256"]) or model != _sha(source["model_sha256"])
        or receipt.get("contains_timed_transcript") is not True
        or receipt.get("text_timestamp_basis") != "relative to decoded PCM window"
    ):
        raise ValueError("Transcript receipt does not match its source record.")
    return {
        "kind": "transcript_window", "parent_sha256": parent,
        "derivative_sha256": _sha(receipt["text_sha256"]), "receipt_sha256": receipt_hash,
        "source_receipt_sha256": source_receipt_hash, "parent_file_id": media_id,
        "parent_source_id": source_id, "model_sha256": model,
        "requested_start_seconds": start, "requested_duration_seconds": duration,
        "decoded_frames": _integer(receipt["decoded_frames"], 1, 4_800_000),
        "sample_rate": _integer(receipt["sample_rate"], 16_000, 16_000),
        "timestamp_basis": "decoded_window_relative",
    }


def link_row(connection: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    relative = bytes(row["relative_path_bytes"])
    mime = _MIME.fullmatch(relative)
    transcript = _TRANSCRIPT.fullmatch(relative)
    if mime is None and transcript is None:
        return None
    result: dict = {}
    try:
        root = os.open(bytes(row["root_path_bytes"]), _DIRECTORY_FLAGS)
        try:
            marker = b".mime-source.json" if mime else b".transcript-source.json"
            _read_receipt(root, marker)
            result = _mime(root, mime, row["size_bytes"]) if mime else _transcript(root, transcript)
            _, digest = read_digest(root, relative, expected=_catalog_fingerprint(row))
            if digest != result["derivative_sha256"]:
                raise ValueError("Derivative bytes do not match the receipt.")
        finally:
            os.close(root)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        status = "unverified"
        result = {}
    else:
        status = "derivative_matched_receipt"
    connection.execute("""
        INSERT INTO file_lineage(file_id, fingerprint, status, record)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(file_id, fingerprint) DO UPDATE SET status = excluded.status, record = excluded.record
    """, (row["id"], json.dumps(_catalog_fingerprint(row), separators=(",", ":")),
          status, json.dumps(result, sort_keys=True)))
    return status


def backfill(catalog, args) -> int:
    if not 1 <= args.source_id <= 2**63 - 1 or not 1 <= args.limit <= 1000:
        raise ValueError("Use a positive source ID and a batch limit of 1–1000.")
    connection = catalog.connection
    if connection.execute("SELECT 1 FROM sources WHERE id = ?", (args.source_id,)).fetchone() is None:
        raise ValueError("Source ID is absent from the catalog.")
    condition = "l.file_id IS NULL"
    if args.retry_unverified:
        condition = "(l.file_id IS NULL OR l.status = 'unverified')"
    rows = connection.execute(f"""
        SELECT f.*, s.root_path_bytes FROM files f
        JOIN sources s ON s.id = f.source_id
        LEFT JOIN file_lineage l ON l.file_id = f.id AND l.fingerprint = {_IDENTITY_SQL}
        WHERE f.status = 'active' AND f.source_id = ? AND {condition}
          AND (f.relative_path GLOB '*/part-[0-9][0-9][0-9][0-9].*'
               OR f.relative_path GLOB 'media-*/window-*/transcript.txt')
        ORDER BY f.id
        LIMIT ?
    """, (args.source_id, args.limit + 1)).fetchall()
    counts = Counter()
    for row in rows[:args.limit]:
        status = link_row(connection, row)
        if status is None:
            connection.execute("""
                INSERT OR IGNORE INTO file_lineage(file_id, fingerprint, status, record)
                VALUES (?, ?, 'not_applicable', '{}')
            """, (row["id"], json.dumps(_catalog_fingerprint(row), separators=(",", ":"))))
        counts[status or "not_applicable"] += 1
        catalog.flush()
    print(json.dumps({"counts": dict(counts), "more_items": len(rows) > args.limit}))
    return int(bool(counts["unverified"]))


def public_lineage(status: str | None, raw: str | None) -> dict:
    """Project a fixed metadata-only schema into a handoff."""
    if status != "derivative_matched_receipt":
        return {"status": status if status in ("unverified", "not_applicable") else "unlinked"}
    try:
        record = json.loads(raw)
        fields = {
            "status": status, "kind": record["kind"], "parent_sha256_basis": "declared_by_receipt",
            **{key: _sha(record[key]) for key in ("parent_sha256", "derivative_sha256", "receipt_sha256")},
        }
        if record["kind"] == "mime_part":
            fields["part_index"] = _integer(record["part_index"], 1, 1000)
        elif record["kind"] == "transcript_window":
            fields.update({key: _sha(record[key])
                           for key in ("model_sha256", "source_receipt_sha256")})
            for key, low, high in (
                ("parent_file_id", 1, 2**63 - 1), ("parent_source_id", 1, 2**63 - 1),
                ("requested_start_seconds", 0, 604_800), ("requested_duration_seconds", 1, 300),
                ("decoded_frames", 1, 4_800_000), ("sample_rate", 16_000, 16_000),
            ):
                fields[key] = _integer(record[key], low, high)
            fields["timestamp_basis"] = "decoded_window_relative"
        else:
            raise ValueError("Unknown lineage type.")
    except (ValueError, TypeError, KeyError, RecursionError):
        return {"status": "unverified"}
    return fields
