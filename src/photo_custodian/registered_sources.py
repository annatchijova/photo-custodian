"""Explicit Linux attachment of catalog sources to a filesystem UUID."""

from __future__ import annotations

import json
import os
import sqlite3
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .catalog import Catalog
from .scanner import scan_registered_source

_UUID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_MOUNT_ESCAPE = re.compile(rb"\\([0-7]{3})")


@dataclass(frozen=True)
class Attachment:
    root: Path
    mount_id: bytes
    device: int
    inode: int


def _uuid(value: str) -> str:
    if not _UUID.fullmatch(value):
        raise ValueError("Use a filesystem UUID from /dev/disk/by-uuid.")
    return value


def _folder(raw: bytes) -> tuple[bytes, ...]:
    if raw == b".":
        return ()
    parts = tuple(raw.split(b"/"))
    if any(part in (b"", b".", b"..") or b"\0" in part for part in parts):
        raise ValueError("Use a path relative to the mountpoint, or . for its root.")
    return parts


def _attach(mount: Path, uuid: str, relative: bytes) -> Attachment:
    supplied = mount.expanduser()
    if supplied.is_symlink():
        raise ValueError("Mountpoint must be a real directory.")
    mounted = supplied.resolve(strict=True)
    encoded = os.fsencode(mounted)
    link_stat = (Path("/dev/disk/by-uuid") / _uuid(uuid)).stat()
    if not stat.S_ISBLK(link_stat.st_mode):
        raise ValueError("The registered UUID does not identify a block device.")
    number = f"{os.major(link_stat.st_rdev)}:{os.minor(link_stat.st_rdev)}".encode()
    entries = []
    mount_paths = []
    for line in Path("/proc/self/mountinfo").read_bytes().splitlines():
        fields = line.split(b" ")
        if len(fields) < 6:
            continue
        path = _MOUNT_ESCAPE.sub(lambda match: bytes([int(match[1], 8)]), fields[4])
        mount_paths.append(path)
        if path == encoded:
            entries.append((fields[0], fields[2], fields[3]))
    if len(entries) != 1 or entries[0][1:] != (number, b"/"):
        raise ValueError("The supplied path is not an unambiguous mount root for the registered UUID.")
    root = mounted
    for part in _folder(relative):
        root /= os.fsdecode(part)
        if root.is_symlink():
            raise ValueError("Registered source folders cannot traverse symbolic links.")
    if not root.is_dir() or root.resolve(strict=True) != root:
        raise ValueError("Registered source folder is unavailable.")
    root_bytes = os.fsencode(root)
    prefix = root_bytes.rstrip(b"/") + b"/"
    if any(path != encoded and (
        path == root_bytes or path.startswith(prefix)
        or (path.startswith(encoded.rstrip(b"/") + b"/")
            and root_bytes.startswith(path.rstrip(b"/") + b"/"))
    ) for path in mount_paths):
        raise ValueError("Registered source contains a nested mount.")
    metadata = root.stat()
    mount_metadata = mounted.stat()
    if metadata.st_dev != mount_metadata.st_dev or metadata.st_dev != link_stat.st_rdev:
        raise ValueError("Source folder is on a different device.")
    return Attachment(root, entries[0][0], metadata.st_dev, metadata.st_ino)


def list_sources(args) -> int:
    path = args.catalog.expanduser().resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("""
            SELECT s.id AS source_id, s.status AS last_attachment_status, s.root_path,
                   m.source_id IS NOT NULL AS registered,
                   coalesce(r.status, 'not_scanned') AS latest_scan_status
            FROM sources s LEFT JOIN registered_mounts m ON m.source_id = s.id
            LEFT JOIN scan_runs r ON r.id = (
                SELECT max(id) FROM scan_runs WHERE source_id = s.id
            ) ORDER BY s.id
        """)
        result = []
        for row in rows:
            item = dict(row)
            item["registered"] = bool(item["registered"])
            if not args.show_paths:
                item.pop("root_path")
            result.append(item)
    finally:
        connection.close()
    print(json.dumps(result, indent=2))
    return 0


def require_attached_source(row: sqlite3.Row) -> None:
    if row["filesystem_uuid"] is None:
        return
    if row["status"] != "active":
        raise ValueError("Registered source is unavailable. Run source-scan after reconnecting.")
    root = Path(os.fsdecode(row["root_path_bytes"]))
    relative = bytes(row["folder_bytes"])
    mount = root
    for _ in _folder(relative):
        mount = mount.parent
    try:
        attached = _attach(mount, row["filesystem_uuid"], relative)
    except (OSError, ValueError):
        raise ValueError("Registered source is unavailable. Run source-scan after reconnecting.") from None
    if attached.root != root or attached.device != row["device"]:
        raise ValueError("Registered source changed. Run source-scan to reattach it.")


def register(catalog: Catalog, args) -> int:
    uuid = _uuid(args.uuid)
    relative = os.fsencode(args.folder)
    attached = _attach(args.mount, uuid, relative)
    connection = catalog.connection
    with connection:
        existing = connection.execute("""
            SELECT source_id FROM registered_mounts WHERE filesystem_uuid = ? AND folder_bytes = ?
        """, (uuid, relative)).fetchone()
        if existing is not None:
            raise ValueError(f"Source already registered as ID {existing[0]}.")
        root_bytes = os.fsencode(attached.root)
        source = connection.execute("SELECT id, device FROM sources WHERE root_path_bytes = ?",
                                    (root_bytes,)).fetchone()
        if source is not None and source["device"] != attached.device:
            raise ValueError("Existing catalog path belongs to a different observed device.")
        source_id = int(source["id"]) if source else catalog.ensure_source(attached.root)
        connection.execute("""
            INSERT INTO registered_mounts(source_id, filesystem_uuid, folder_bytes)
            VALUES (?, ?, ?)
        """, (source_id, uuid, relative))
    print(f"Registered source ID: {source_id}")
    return 0


def scan(catalog: Catalog, args) -> int:
    from .cli import _print_scan

    if not 1 <= args.source_id <= 2**63 - 1:
        raise ValueError("Use a positive registered source ID.")
    row = catalog.connection.execute("""
        SELECT filesystem_uuid, folder_bytes FROM registered_mounts WHERE source_id = ?
    """, (args.source_id,)).fetchone()
    if row is None:
        raise ValueError("Source ID is not registered.")

    def observe() -> Attachment:
        return _attach(args.mount, row["filesystem_uuid"], bytes(row["folder_bytes"]))

    try:
        attached = observe()
    except (OSError, ValueError):
        catalog.connection.execute("UPDATE sources SET status = 'unavailable' WHERE id = ?",
                                   (args.source_id,))
        catalog.flush()
        raise ValueError("Registered source is unavailable at this mountpoint.") from None
    def unchanged() -> bool:
        try:
            return observe() == attached
        except (OSError, ValueError):
            return False

    result = scan_registered_source(
        catalog, args.source_id, attached.root,
        identity=(attached.device, attached.inode), unchanged=unchanged,
    )
    _print_scan(result.as_dict(), args.as_json)
    return int(result.status != "complete")
