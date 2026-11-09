from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable


CATALOG_SCHEMA_VERSION = 1


SCHEMA_V1 = """
CREATE TABLE schema_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE sources (
    id INTEGER PRIMARY KEY,
    root_path TEXT NOT NULL,
    root_path_bytes BLOB NOT NULL UNIQUE,
    device INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'unavailable'))
);

CREATE TABLE scan_runs (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'complete', 'warning', 'failed')),
    summary_json TEXT
);

CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    relative_path TEXT NOT NULL,
    relative_path_bytes BLOB NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    mode INTEGER NOT NULL,
    media_kind TEXT NOT NULL
        CHECK (media_kind IN ('image', 'video', 'other')),
    extension TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'missing')),
    last_seen_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
    UNIQUE (source_id, relative_path_bytes)
);

CREATE INDEX files_source_status_idx ON files(source_id, status);
CREATE INDEX files_kind_size_idx ON files(media_kind, size_bytes);
CREATE INDEX files_fingerprint_idx ON files(size_bytes, mtime_ns, ctime_ns);

CREATE TABLE scan_errors (
    id INTEGER PRIMARY KEY,
    scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
    path_display TEXT NOT NULL,
    error_type TEXT NOT NULL,
    message TEXT NOT NULL
);
"""


class Catalog:
    """Versioned SQLite catalog for read-only source inventory."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self._initialize_or_migrate()

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        self.connection.close()

    def _initialize_or_migrate(self) -> None:
        stored_version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if stored_version > CATALOG_SCHEMA_VERSION:
            self.connection.close()
            raise RuntimeError(
                f"catalog schema version {stored_version} is newer than supported "
                f"version {CATALOG_SCHEMA_VERSION}"
            )

        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if stored_version == 0:
            if tables:
                self.connection.close()
                raise RuntimeError(
                    "catalog has tables but no supported schema version; refusing "
                    "an implicit migration"
                )
            with self.connection:
                self.connection.executescript(SCHEMA_V1)
                self.connection.execute(
                    "INSERT INTO schema_metadata(singleton, schema_version) VALUES (1, ?)",
                    (CATALOG_SCHEMA_VERSION,),
                )
                self.connection.execute(f"PRAGMA user_version = {CATALOG_SCHEMA_VERSION}")
            return

        metadata = self.connection.execute(
            "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None or int(metadata[0]) != stored_version:
            self.connection.close()
            raise RuntimeError("catalog schema version metadata is missing or inconsistent")

        if stored_version < CATALOG_SCHEMA_VERSION:
            self.connection.close()
            raise RuntimeError(
                f"catalog schema version {stored_version} requires a migration that is "
                "not implemented"
            )

    def ensure_source(self, root: Path) -> int:
        root_bytes = os.fsencode(root)
        root_display = root_bytes.decode("utf-8", errors="backslashreplace")
        stat_result = root.stat()
        self.connection.execute(
            """
            INSERT INTO sources(root_path, root_path_bytes, device, status)
            VALUES (?, ?, ?, 'active')
            ON CONFLICT(root_path_bytes) DO UPDATE SET
                root_path = excluded.root_path,
                device = excluded.device,
                status = 'active'
            """,
            (root_display, root_bytes, stat_result.st_dev),
        )
        row = self.connection.execute(
            "SELECT id FROM sources WHERE root_path_bytes = ?", (root_bytes,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"failed to create catalog source for {root_display}")
        return int(row[0])

    def begin_scan(self, source_id: int) -> int:
        cursor = self.connection.execute(
            "INSERT INTO scan_runs(source_id) VALUES (?)", (source_id,)
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_file(
        self,
        *,
        source_id: int,
        scan_run_id: int,
        relative_path_bytes: bytes,
        stat_result: os.stat_result,
        media_kind: str,
        extension: str,
    ) -> str:
        existing = self.connection.execute(
            """
            SELECT size_bytes, mtime_ns, ctime_ns, device, inode, mode, status
            FROM files
            WHERE source_id = ? AND relative_path_bytes = ?
            """,
            (source_id, relative_path_bytes),
        ).fetchone()
        fingerprint = (
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_mode,
        )
        if existing is None:
            outcome = "new"
        elif tuple(existing[key] for key in existing.keys()[:6]) == fingerprint and existing[
            "status"
        ] == "active":
            outcome = "unchanged"
        else:
            outcome = "changed"

        relative_display = relative_path_bytes.decode("utf-8", errors="backslashreplace")
        self.connection.execute(
            """
            INSERT INTO files(
                source_id, relative_path, relative_path_bytes, size_bytes, mtime_ns,
                ctime_ns, device, inode, mode, media_kind, extension, status,
                last_seen_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            ON CONFLICT(source_id, relative_path_bytes) DO UPDATE SET
                relative_path = excluded.relative_path,
                size_bytes = excluded.size_bytes,
                mtime_ns = excluded.mtime_ns,
                ctime_ns = excluded.ctime_ns,
                device = excluded.device,
                inode = excluded.inode,
                mode = excluded.mode,
                media_kind = excluded.media_kind,
                extension = excluded.extension,
                status = 'active',
                last_seen_run_id = excluded.last_seen_run_id
            """,
            (
                source_id,
                relative_display,
                relative_path_bytes,
                *fingerprint,
                media_kind,
                extension,
                scan_run_id,
            ),
        )
        return outcome

    def record_error(
        self, scan_run_id: int, path: str | os.PathLike[str], error: OSError
    ) -> None:
        path_display = os.fsencode(path).decode("utf-8", errors="backslashreplace")
        self.connection.execute(
            """
            INSERT INTO scan_errors(scan_run_id, path_display, error_type, message)
            VALUES (?, ?, ?, ?)
            """,
            (scan_run_id, path_display, type(error).__name__, str(error)),
        )

    def mark_missing(self, source_id: int, scan_run_id: int) -> int:
        cursor = self.connection.execute(
            """
            UPDATE files
            SET status = 'missing'
            WHERE source_id = ? AND status = 'active' AND last_seen_run_id != ?
            """,
            (source_id, scan_run_id),
        )
        return int(cursor.rowcount)

    def complete_scan(self, scan_run_id: int, status: str, summary: dict[str, Any]) -> None:
        if status not in {"complete", "warning", "failed"}:
            raise ValueError(f"invalid terminal scan status: {status}")
        self.connection.execute(
            """
            UPDATE scan_runs
            SET completed_at = CURRENT_TIMESTAMP, status = ?, summary_json = ?
            WHERE id = ?
            """,
            (status, json.dumps(summary, sort_keys=True), scan_run_id),
        )
        self.connection.commit()

    def flush(self) -> None:
        self.connection.commit()

    def list_files(self, *, include_missing: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM files"
        parameters: Iterable[object] = ()
        if not include_missing:
            query += " WHERE status = ?"
            parameters = ("active",)
        query += " ORDER BY relative_path_bytes"
        return list(self.connection.execute(query, parameters))

    def inventory_summary(self) -> list[dict[str, int | str]]:
        rows = self.connection.execute(
            """
            SELECT media_kind, COUNT(*) AS file_count, COALESCE(SUM(size_bytes), 0) AS total_bytes
            FROM files
            WHERE status = 'active'
            GROUP BY media_kind
            ORDER BY media_kind
            """
        )
        return [
            {
                "media_kind": str(row["media_kind"]),
                "file_count": int(row["file_count"]),
                "total_bytes": int(row["total_bytes"]),
            }
            for row in rows
        ]
