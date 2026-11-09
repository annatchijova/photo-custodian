from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from photo_custodian.catalog import Catalog
from photo_custodian.scanner import scan_sources


class ScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.catalog_path = self.base / "catalog.sqlite3"

    def test_scan_indexes_regular_files_and_skips_symlinks(self) -> None:
        photo = self.source / "photo.jpg"
        photo.write_bytes(b"photo-data")
        document = self.source / "notes.txt"
        document.write_bytes(b"notes")
        (self.source / "photo-link.jpg").symlink_to(photo)

        with Catalog(self.catalog_path) as catalog:
            summary = scan_sources(catalog, [self.source])
            rows = catalog.list_files()

        self.assertEqual(summary.discovered_files, 2)
        self.assertEqual(summary.new_files, 2)
        self.assertEqual(summary.skipped_entries, 1)
        self.assertEqual(summary.error_count, 0)
        self.assertEqual({row["relative_path"] for row in rows}, {"photo.jpg", "notes.txt"})
        self.assertEqual(
            {row["relative_path"]: row["media_kind"] for row in rows},
            {"photo.jpg": "image", "notes.txt": "other"},
        )

    def test_repeat_scan_distinguishes_unchanged_and_changed_files(self) -> None:
        photo = self.source / "photo.jpg"
        photo.write_bytes(b"first")

        with Catalog(self.catalog_path) as catalog:
            first = scan_sources(catalog, [self.source])
            second = scan_sources(catalog, [self.source])
            photo.write_bytes(b"second-version")
            os.utime(photo, None)
            third = scan_sources(catalog, [self.source])

        self.assertEqual((first.new_files, first.changed_files), (1, 0))
        self.assertEqual((second.unchanged_files, second.changed_files), (1, 0))
        self.assertEqual((third.unchanged_files, third.changed_files), (0, 1))

    def test_completed_scan_marks_absent_files_missing(self) -> None:
        photo = self.source / "photo.jpg"
        photo.write_bytes(b"data")

        with Catalog(self.catalog_path) as catalog:
            scan_sources(catalog, [self.source])
            photo.unlink()
            summary = scan_sources(catalog, [self.source])
            rows = catalog.list_files(include_missing=True)

        self.assertEqual(summary.missing_files, 1)
        self.assertEqual(rows[0]["status"], "missing")

    def test_inventory_summary_aggregates_by_media_kind(self) -> None:
        (self.source / "a.jpg").write_bytes(b"1234")
        (self.source / "b.mp4").write_bytes(b"123456")
        (self.source / "c.bin").write_bytes(b"12")

        with Catalog(self.catalog_path) as catalog:
            scan_sources(catalog, [self.source])
            inventory = catalog.inventory_summary()

        self.assertEqual(
            inventory,
            [
                {"media_kind": "image", "file_count": 1, "total_bytes": 4},
                {"media_kind": "other", "file_count": 1, "total_bytes": 2},
                {"media_kind": "video", "file_count": 1, "total_bytes": 6},
            ],
        )

    def test_invalid_roots_are_rejected_before_catalog_mutation(self) -> None:
        regular_file = self.base / "not-a-directory"
        regular_file.write_text("content", encoding="utf-8")

        with Catalog(self.catalog_path) as catalog:
            with self.assertRaisesRegex(ValueError, "is not a directory"):
                scan_sources(catalog, [regular_file])
            runs = catalog.connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]

        self.assertEqual(runs, 0)

    def test_summary_is_json_serializable(self) -> None:
        (self.source / "photo.jpg").write_bytes(b"data")

        with Catalog(self.catalog_path) as catalog:
            summary = scan_sources(catalog, [self.source])

        encoded = json.dumps(summary.as_dict(), sort_keys=True)
        self.assertIn('"discovered_files": 1', encoded)


if __name__ == "__main__":
    unittest.main()
