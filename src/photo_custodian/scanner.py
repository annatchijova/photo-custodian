from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .catalog import Catalog


IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".dng",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".raw",
    ".tif",
    ".tiff",
    ".webp",
}
VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}


@dataclass
class ScanSummary:
    source_count: int = 0
    discovered_files: int = 0
    total_bytes: int = 0
    new_files: int = 0
    changed_files: int = 0
    unchanged_files: int = 0
    missing_files: int = 0
    skipped_entries: int = 0
    error_count: int = 0
    status: str = "complete"

    def as_dict(self) -> dict[str, int | str]:
        return asdict(self)


def _validate_roots(roots: Iterable[str | os.PathLike[str]]) -> list[Path]:
    validated: list[Path] = []
    seen: set[bytes] = set()
    for supplied_root in roots:
        candidate = Path(supplied_root).expanduser()
        if candidate.is_symlink():
            raise ValueError(f"source root is a symlink: {candidate}")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"source root is not a directory: {candidate}")
        encoded = os.fsencode(resolved)
        if encoded not in seen:
            validated.append(resolved)
            seen.add(encoded)
    if not validated:
        raise ValueError("at least one source root is required")
    return validated


def _classify_extension(name: str) -> tuple[str, str]:
    extension = Path(name).suffix.lower()
    if extension in IMAGE_EXTENSIONS:
        return "image", extension
    if extension in VIDEO_EXTENSIONS:
        return "video", extension
    return "other", extension


def _scan_one_source(
    catalog: Catalog,
    root: Path,
    summary: ScanSummary,
    *,
    flush_every: int,
) -> None:
    source_id = catalog.ensure_source(root)
    scan_run_id = catalog.begin_scan(source_id)
    stack: list[str] = [os.fspath(root)]
    recorded_since_flush = 0
    source_errors = 0

    while stack:
        directory = stack.pop()
        try:
            iterator = os.scandir(directory)
        except OSError as error:
            catalog.record_error(scan_run_id, directory, error)
            summary.error_count += 1
            source_errors += 1
            continue

        with iterator:
            for entry in iterator:
                try:
                    if entry.is_symlink():
                        summary.skipped_entries += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        summary.skipped_entries += 1
                        continue
                    stat_result = entry.stat(follow_symlinks=False)
                except OSError as error:
                    catalog.record_error(scan_run_id, entry.path, error)
                    summary.error_count += 1
                    source_errors += 1
                    continue

                relative_path_bytes = os.path.relpath(
                    os.fsencode(entry.path), os.fsencode(root)
                )
                media_kind, extension = _classify_extension(entry.name)
                outcome = catalog.record_file(
                    source_id=source_id,
                    scan_run_id=scan_run_id,
                    relative_path_bytes=relative_path_bytes,
                    stat_result=stat_result,
                    media_kind=media_kind,
                    extension=extension,
                )
                summary.discovered_files += 1
                summary.total_bytes += stat_result.st_size
                if outcome == "new":
                    summary.new_files += 1
                elif outcome == "changed":
                    summary.changed_files += 1
                else:
                    summary.unchanged_files += 1

                recorded_since_flush += 1
                if recorded_since_flush >= flush_every:
                    catalog.flush()
                    recorded_since_flush = 0

    if source_errors == 0:
        summary.missing_files += catalog.mark_missing(source_id, scan_run_id)
        terminal_status = "complete"
    else:
        terminal_status = "warning"
        summary.status = "warning"
    catalog.complete_scan(scan_run_id, terminal_status, summary.as_dict())


def scan_sources(
    catalog: Catalog,
    roots: Iterable[str | os.PathLike[str]],
    *,
    flush_every: int = 1_000,
) -> ScanSummary:
    """Inventory regular files without modifying any source root."""

    if flush_every < 1:
        raise ValueError("flush_every must be at least 1")
    validated_roots = _validate_roots(roots)
    summary = ScanSummary(source_count=len(validated_roots))
    for root in validated_roots:
        _scan_one_source(catalog, root, summary, flush_every=flush_every)
    return summary
