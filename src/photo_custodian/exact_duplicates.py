from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from dataclasses import asdict, dataclass, field
from typing import Any, Iterator

from .catalog import Catalog


HASH_ALGORITHM = "sha256"
HASH_CHUNK_SIZE = 8 * 1024 * 1024
MAX_GROUP_LIMIT = 10_000
MAX_FILES_PER_GROUP = 1_000


class StaleFileError(RuntimeError):
    """Raised when a file no longer matches the catalog fingerprint."""


@dataclass(frozen=True)
class ExactDuplicateFile:
    file_id: int
    path: str
    relative_path: str
    device: int
    inode: int

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


@dataclass(frozen=True)
class ExactDuplicateGroup:
    sha256: str
    size_bytes: int
    file_count: int
    distinct_storage_instances: int
    reclaimable_bytes: int
    files: tuple[ExactDuplicateFile, ...]
    files_truncated: int = 0

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["files"] = [item.as_dict() for item in self.files]
        return result


@dataclass
class ExactDuplicateReport:
    status: str = "complete"
    candidate_files: int = 0
    hashed_files: int = 0
    reused_hashes: int = 0
    stale_files: int = 0
    error_count: int = 0
    total_group_count: int = 0
    returned_group_count: int = 0
    groups: list[ExactDuplicateGroup] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["groups"] = [group.as_dict() for group in self.groups]
        return result


def _catalog_fingerprint(row: sqlite3.Row) -> tuple[int, int, int, int, int]:
    return (
        int(row["size_bytes"]),
        int(row["mtime_ns"]),
        int(row["ctime_ns"]),
        int(row["device"]),
        int(row["inode"]),
    )


def _stat_fingerprint(result: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
        result.st_dev,
        result.st_ino,
    )


def _iter_candidates(connection: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT
            f.id AS file_id,
            f.relative_path,
            f.relative_path_bytes,
            f.size_bytes,
            f.mtime_ns,
            f.ctime_ns,
            f.device,
            f.inode,
            s.root_path,
            s.root_path_bytes
        FROM files AS f
        JOIN sources AS s ON s.id = f.source_id
        WHERE f.status = 'active'
          AND f.size_bytes IN (
              SELECT size_bytes
              FROM files
              WHERE status = 'active'
              GROUP BY size_bytes
              HAVING COUNT(*) > 1
          )
        ORDER BY f.size_bytes, f.id
        """
    )
    yield from rows


def _cached_digest(connection: sqlite3.Connection, row: sqlite3.Row) -> bytes | None:
    cached = connection.execute(
        """
        SELECT digest
        FROM file_hashes
        WHERE file_id = ? AND algorithm = ?
          AND size_bytes = ? AND mtime_ns = ? AND ctime_ns = ?
          AND device = ? AND inode = ?
        """,
        (
            row["file_id"],
            HASH_ALGORITHM,
            *_catalog_fingerprint(row),
        ),
    ).fetchone()
    return None if cached is None else bytes(cached[0])


def _full_path_bytes(row: sqlite3.Row) -> bytes:
    return os.path.join(bytes(row["root_path_bytes"]), bytes(row["relative_path_bytes"]))


def _hash_file(row: sqlite3.Row) -> bytes:
    path = _full_path_bytes(row)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise StaleFileError("catalog target is no longer a regular file")
        if _stat_fingerprint(before) != _catalog_fingerprint(row):
            raise StaleFileError("file fingerprint changed after inventory scan")

        digest = hashlib.sha256()
        buffer = bytearray(HASH_CHUNK_SIZE)
        view = memoryview(buffer)
        while read_count := handle.readinto(buffer):
            digest.update(view[:read_count])

        after = os.fstat(handle.fileno())
        if _stat_fingerprint(after) != _stat_fingerprint(before):
            raise StaleFileError("file fingerprint changed while hashing")
        return digest.digest()


def _store_digest(connection: sqlite3.Connection, row: sqlite3.Row, digest: bytes) -> None:
    connection.execute(
        """
        INSERT INTO file_hashes(
            file_id, algorithm, digest, size_bytes, mtime_ns, ctime_ns, device, inode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(file_id, algorithm) DO UPDATE SET
            digest = excluded.digest,
            size_bytes = excluded.size_bytes,
            mtime_ns = excluded.mtime_ns,
            ctime_ns = excluded.ctime_ns,
            device = excluded.device,
            inode = excluded.inode,
            computed_at = CURRENT_TIMESTAMP
        """,
        (
            row["file_id"],
            HASH_ALGORITHM,
            digest,
            *_catalog_fingerprint(row),
        ),
    )


def _valid_hash_predicate() -> str:
    return """
        h.algorithm = 'sha256'
        AND f.status = 'active'
        AND h.size_bytes = f.size_bytes
        AND h.mtime_ns = f.mtime_ns
        AND h.ctime_ns = f.ctime_ns
        AND h.device = f.device
        AND h.inode = f.inode
    """


def _load_groups(
    connection: sqlite3.Connection,
    *,
    group_limit: int,
) -> tuple[int, list[ExactDuplicateGroup]]:
    predicate = _valid_hash_predicate()
    total_group_count = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM (
                SELECT h.digest, f.size_bytes
                FROM file_hashes AS h
                JOIN files AS f ON f.id = h.file_id
                WHERE {predicate}
                GROUP BY h.digest, f.size_bytes
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
    )
    summaries = connection.execute(
        f"""
        SELECT
            h.digest,
            f.size_bytes,
            COUNT(*) AS file_count,
            COUNT(DISTINCT printf('%lld:%lld', f.device, f.inode))
                AS distinct_storage_instances
        FROM file_hashes AS h
        JOIN files AS f ON f.id = h.file_id
        WHERE {predicate}
        GROUP BY h.digest, f.size_bytes
        HAVING COUNT(*) > 1
        ORDER BY f.size_bytes DESC, h.digest
        LIMIT ?
        """,
        (group_limit,),
    ).fetchall()

    groups: list[ExactDuplicateGroup] = []
    for summary in summaries:
        digest = bytes(summary["digest"])
        members = connection.execute(
            f"""
            SELECT
                f.id AS file_id,
                f.relative_path,
                f.device,
                f.inode,
                s.root_path_bytes,
                f.relative_path_bytes
            FROM file_hashes AS h
            JOIN files AS f ON f.id = h.file_id
            JOIN sources AS s ON s.id = f.source_id
            WHERE {predicate} AND h.digest = ? AND f.size_bytes = ?
            ORDER BY s.root_path_bytes, f.relative_path_bytes
            LIMIT ?
            """,
            (digest, summary["size_bytes"], MAX_FILES_PER_GROUP),
        ).fetchall()
        files = tuple(
            ExactDuplicateFile(
                file_id=int(member["file_id"]),
                path=os.path.join(
                    bytes(member["root_path_bytes"]),
                    bytes(member["relative_path_bytes"]),
                ).decode("utf-8", errors="backslashreplace"),
                relative_path=str(member["relative_path"]),
                device=int(member["device"]),
                inode=int(member["inode"]),
            )
            for member in members
        )
        file_count = int(summary["file_count"])
        distinct_storage_instances = int(summary["distinct_storage_instances"])
        size_bytes = int(summary["size_bytes"])
        groups.append(
            ExactDuplicateGroup(
                sha256=digest.hex(),
                size_bytes=size_bytes,
                file_count=file_count,
                distinct_storage_instances=distinct_storage_instances,
                reclaimable_bytes=max(0, distinct_storage_instances - 1) * size_bytes,
                files=files,
                files_truncated=file_count - len(files),
            )
        )
    return total_group_count, groups


def find_exact_duplicates(
    catalog: Catalog,
    *,
    group_limit: int = 100,
    commit_every: int = 64,
) -> ExactDuplicateReport:
    """Hash size-collision candidates and return review-only exact groups."""

    if not 1 <= group_limit <= MAX_GROUP_LIMIT:
        raise ValueError(f"group_limit must be between 1 and {MAX_GROUP_LIMIT}")
    if commit_every < 1:
        raise ValueError("commit_every must be at least 1")

    report = ExactDuplicateReport()
    writes_since_commit = 0
    for row in _iter_candidates(catalog.connection):
        report.candidate_files += 1
        if _cached_digest(catalog.connection, row) is not None:
            report.reused_hashes += 1
            continue
        try:
            digest = _hash_file(row)
        except StaleFileError:
            report.stale_files += 1
            report.status = "warning"
            continue
        except OSError:
            report.error_count += 1
            report.status = "warning"
            continue

        _store_digest(catalog.connection, row, digest)
        report.hashed_files += 1
        writes_since_commit += 1
        if writes_since_commit >= commit_every:
            catalog.flush()
            writes_since_commit = 0

    catalog.flush()
    report.total_group_count, report.groups = _load_groups(
        catalog.connection,
        group_limit=group_limit,
    )
    report.returned_group_count = len(report.groups)
    return report
