"""Record independent working-copy byte checks for declared parent hashes."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from .manifest_verify import check_entries, read_manifest


def verify_parents(catalog, args) -> int:
    payload, manifest_digest = read_manifest(args.manifest)
    if manifest_digest != args.expected_sha256:
        raise ValueError("Manifest does not match the independently supplied digest.")
    if payload["source_id"] > 2**63 - 1 or any(entry["file_id"] > 2**63 - 1
                                              for entry in payload["files"]):
        raise ValueError("Manifest IDs exceed the catalog integer range.")
    # Stage observations in memory. Each manifest has a bounded entry count.
    results = check_entries(payload, args.root)
    finished = datetime.now(timezone.utc).isoformat()
    connection = catalog.connection
    with connection:
        connection.executemany("""
            INSERT INTO parent_byte_checks
                (manifest_sha256, manifest_file_id, manifest_source_id, parent_sha256, status, checked_utc)
            VALUES (?, ?, ?, ?, ?, ?)
        """, [
            (manifest_digest, entry["file_id"], payload["source_id"], entry["sha256"], result["status"], finished)
            for entry, result in zip(payload["files"], results, strict=True)
        ])
    counts = Counter(item["status"] for item in results)
    print(json.dumps({
        "payload_sha256": manifest_digest, "counts": dict(sorted(counts.items())),
    }))
    return int(any(item["status"] != "match" for item in results))
