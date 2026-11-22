"""Conservative, human-declared timestamp semantics for catalog sources."""

from __future__ import annotations

import json

_SEMANTICS = ("unknown", "filesystem_metadata")


def set_profile(catalog, args) -> int:
    if not 1 <= args.source_id <= 2**63 - 1 or args.semantics not in _SEMANTICS:
        raise ValueError("Use a catalog source ID and a supported timestamp semantic.")
    if catalog.connection.execute("SELECT 1 FROM sources WHERE id = ?", (args.source_id,)).fetchone() is None:
        raise ValueError("Source ID is absent from the catalog.")
    catalog.connection.execute("""
        INSERT INTO source_time_profiles(source_id, timestamp_semantics, declared_by)
        VALUES (?, ?, 'authorized_reviewer')
        ON CONFLICT(source_id) DO UPDATE SET
            timestamp_semantics = excluded.timestamp_semantics,
            declared_by = excluded.declared_by
    """, (args.source_id, args.semantics))
    catalog.flush()
    print(json.dumps({"source_id": args.source_id, "timestamp_semantics": args.semantics,
                      "declared_by": "authorized_reviewer"}))
    return 0
