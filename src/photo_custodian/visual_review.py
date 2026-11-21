"""Local photo fingerprints and review candidates."""

from __future__ import annotations

import base64
import io
import json
import os
import sqlite3
import stat
from collections import defaultdict
from dataclasses import dataclass

from .exact_duplicates import _catalog_fingerprint, _full_path_bytes, _stat_fingerprint

SIGNATURE_VERSION = "dhash-tone-v1"
MAX_PIXELS = 60_000_000
MAX_DISTANCE = 4
DEFAULT_COMPARISONS = 25_000_000
BAND_WIDTHS = (13, 13, 13, 13, 12)


@dataclass(frozen=True)
class Photo:
    file_id: int
    path: str
    size: int
    width: int
    height: int
    digest: int
    tone: tuple[int, ...]


def image_rows(connection: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {
        row["file_id"]: row
        for row in connection.execute("""
            SELECT f.*, f.id AS file_id, s.root_path_bytes
            FROM files f JOIN sources s ON s.id = f.source_id
            WHERE f.status = 'active' AND f.media_kind = 'image'
            ORDER BY f.id
        """)
    }


def read_photo(
    row: sqlite3.Row, *, preview: bool = False, preview_edge: int = 320
) -> Photo | str:
    from PIL import Image, ImageOps

    path = _full_path_bytes(row)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or _stat_fingerprint(before) != _catalog_fingerprint(row):
            raise ValueError("Photo changed since scan.")
        with Image.open(source) as original:
            if original.width * original.height > MAX_PIXELS or getattr(original, "is_animated", False):
                raise ValueError("Image exceeds review limits or is animated.")
            image = ImageOps.exif_transpose(original).convert("RGB")
            width, height = image.size
            edge = preview_edge if preview else 320
            image.thumbnail((edge, edge))
        if _stat_fingerprint(os.fstat(source.fileno())) != _stat_fingerprint(before):
            raise ValueError("Photo changed while reading.")
    if preview:
        encoded = io.BytesIO()
        # Build a fresh image so embedded source metadata cannot survive.
        clean = Image.new("RGB", image.size)
        clean.paste(image)
        clean.save(encoded, format="JPEG", quality=82)
        return "data:image/jpeg;base64," + base64.b64encode(encoded.getvalue()).decode("ascii")

    pixels = list(image.convert("L").resize((9, 8)).getdata())
    digest = 0
    for y in range(8):
        for x in range(8):
            index = 9 * y + x
            digest = (digest << 1) | (pixels[index] > pixels[index + 1])
    tone = tuple(channel for pixel in image.resize((2, 2)).getdata() for channel in pixel)
    return Photo(
        row["file_id"], path.decode("utf-8", errors="backslashreplace"),
        row["size_bytes"], width, height, digest, tone,
    )


def _load_signature(connection: sqlite3.Connection, row: sqlite3.Row, algorithm: str) -> Photo:
    path = _full_path_bytes(row)
    expected = _catalog_fingerprint(row)
    fingerprint = json.dumps(expected)
    metadata = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(metadata.st_mode) or _stat_fingerprint(metadata) != expected:
        raise ValueError("Photo changed since scan.")
    cached = connection.execute(
        "SELECT signature FROM photo_signatures WHERE file_id = ? AND algorithm = ? AND fingerprint = ?",
        (row["file_id"], algorithm, fingerprint),
    ).fetchone()
    if cached:
        width, height, digest, tone = json.loads(cached[0])
        return Photo(
            row["file_id"], path.decode("utf-8", errors="backslashreplace"),
            row["size_bytes"], width, height, digest, tuple(tone),
        )
    photo = read_photo(row)
    connection.execute(
        """
        INSERT INTO photo_signatures(file_id, algorithm, fingerprint, signature)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(file_id) DO UPDATE SET algorithm = excluded.algorithm,
            fingerprint = excluded.fingerprint, signature = excluded.signature
        """,
        (photo.file_id, algorithm, fingerprint,
         json.dumps((photo.width, photo.height, photo.digest, photo.tone))),
    )
    return photo


def find_versions(
    connection: sqlite3.Connection,
    rows: dict[int, sqlite3.Row],
    aliases: dict[int, int],
    *,
    comparison_budget: int = DEFAULT_COMPARISONS,
) -> tuple[list[list[Photo]], int, int, bool]:
    from PIL import Image, __version__

    algorithm = f"{SIGNATURE_VERSION}:Pillow:{__version__}"

    groups: dict[int, list[Photo]] = {}
    bands: dict[tuple[int, int], list[int]] = defaultdict(list)
    skipped = 0
    analyzed = 0
    comparisons = 0
    for file_id, row in rows.items():
        if file_id in aliases:
            continue
        try:
            photo = _load_signature(connection, row, algorithm)
        except (OSError, ValueError, Image.DecompressionBombError, SyntaxError):
            skipped += 1
            continue
        if (analyzed + skipped + 1) % 128 == 0:
            connection.commit()
        analyzed += 1
        # Uniform or monotonic fingerprints provide little discriminating evidence.
        if not 4 <= photo.digest.bit_count() <= 60:
            skipped += 1
            analyzed -= 1
            continue
        keys = []
        value = photo.digest
        for index, width in enumerate(BAND_WIDTHS):
            keys.append((index, value & ((1 << width) - 1)))
            value >>= width
        candidates = {anchor_id for key in keys for anchor_id in bands.get(key, ())}
        matches = []
        for anchor_id in sorted(candidates):
            if comparisons >= comparison_budget:
                return _multiple(groups), analyzed - 1, skipped, True
            comparisons += 1
            anchor = groups[anchor_id][0]
            distance = (photo.digest ^ anchor.digest).bit_count()
            left = photo.width * anchor.height
            right = anchor.width * photo.height
            if (
                distance <= MAX_DISTANCE
                and abs(left - right) * 100 <= max(left, right) * 5
                and sum(abs(a - b) for a, b in zip(photo.tone, anchor.tone)) <= 240
            ):
                matches.append((distance, anchor_id))
        if matches:
            groups[min(matches)[1]].append(photo)
        else:
            groups[file_id] = [photo]
            for key in keys:
                bands[key].append(file_id)
    return _multiple(groups), analyzed, skipped, False


def _multiple(groups: dict[int, list[Photo]]) -> list[list[Photo]]:
    return sorted(
        (group for group in groups.values() if len(group) > 1),
        key=lambda group: (-len(group), group[0].file_id),
    )
