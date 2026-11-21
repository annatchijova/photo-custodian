"""Export a byte manifest of a bounded selection in a catalogued working copy."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .exact_duplicates import _catalog_fingerprint
from .manifest_verify import MAX_ENTRIES, MAX_MANIFEST_BYTES, _DIRECTORY_FLAGS, payload_digest, read_digest


def export(args) -> int:
    ids = sorted(set(args.ids))
    if not 1 <= len(ids) <= MAX_ENTRIES or any(not 1 <= item <= 2**63 - 1 for item in ids):
        raise ValueError("Select 1–1000 distinct positive catalog file IDs.")
    output = args.output.expanduser()
    if output.suffix.lower() != ".json" or os.path.lexists(output):
        raise ValueError("--output must be a new JSON file.")

    path = Path(args.catalog).expanduser().resolve(strict=True)
    placeholders = ",".join("?" for _ in ids)
    database = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        database.row_factory = sqlite3.Row
        rows = database.execute(f"""
            SELECT f.*, s.root_path_bytes FROM files f
            JOIN sources s ON s.id = f.source_id
            WHERE f.status = 'active' AND f.id IN ({placeholders})
            ORDER BY f.id
        """, ids).fetchall()
    finally:
        database.close()
    if len(rows) != len(ids):
        raise ValueError("One or more IDs are absent from the active catalog.")
    source_id = rows[0]["source_id"]
    if any(row["source_id"] != source_id for row in rows):
        raise ValueError("Select files from a single catalog source per manifest.")

    recorded_root = Path(os.fsdecode(rows[0]["root_path_bytes"]))
    output_parent = output.parent.resolve(strict=True)
    if output_parent.is_relative_to(recorded_root.resolve(strict=True)):
        raise ValueError("Write the manifest outside the catalogued source.")
    output = output_parent / output.name
    started = datetime.now(timezone.utc).isoformat()
    root = os.open(recorded_root, _DIRECTORY_FLAGS)
    entries = []
    try:
        for row in rows:
            relative = bytes(row["relative_path_bytes"])
            try:
                size, digest = read_digest(root, relative, expected=_catalog_fingerprint(row))
            except (OSError, ValueError):
                raise ValueError(f"Catalog file ID {row['id']} is unavailable or changed.") from None
            entries.append({
                "file_id": row["id"], "size_bytes": size, "sha256": digest,
                "relative_path_b64": base64.b64encode(relative).decode("ascii"),
            })
    finally:
        os.close(root)
    payload = {
        "schema_version": 1, "purpose": "working-copy-byte-manifest",
        "source_id": source_id, "files": entries,
    }
    digest = payload_digest(payload)
    raw = json.dumps({
        "payload": payload, "payload_sha256": digest, "tool_version": __version__,
        "started_utc": started, "completed_utc": datetime.now(timezone.utc).isoformat(),
    }, indent=2, sort_keys=True).encode("ascii") + b"\n"
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest exceeds the size limit.")

    descriptor, temporary = tempfile.mkstemp(prefix=".photo-custodian-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, output)
        parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.unlink(temporary)
    print(json.dumps({"files": len(entries), "payload_sha256": digest}))
    return 0
