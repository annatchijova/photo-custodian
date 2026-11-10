from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from photo_custodian.catalog import Catalog
from photo_custodian.exact_duplicates import find_exact_duplicates
from photo_custodian.scanner import scan_sources


class ExactDuplicateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.catalog_path = self.base / "catalog.sqlite3"

    def test_exact_groups_use_complete_sha256_and_distinguish_hardlinks(self) -> None:
        original = self.source / "original.jpg"
        duplicate = self.source / "duplicate.jpg"
        hardlink = self.source / "hardlink.jpg"
        same_size_different_content = self.source / "different.jpg"
        original.write_bytes(b"same-content")
        duplicate.write_bytes(b"same-content")
        hardlink.hardlink_to(original)
        same_size_different_content.write_bytes(b"other-bytes!")

        with Catalog(self.catalog_path) as catalog:
            scan_sources(catalog, [self.source])
            report = find_exact_duplicates(catalog)

        self.assertEqual(report.status, "complete")
        self.assertEqual(report.candidate_files, 4)
        self.assertEqual(report.hashed_files, 4)
        self.assertEqual(report.total_group_count, 1)
        self.assertEqual(len(report.groups), 1)
        group = report.groups[0]
        self.assertEqual(group.size_bytes, len(b"same-content"))
        self.assertEqual(group.file_count, 3)
        self.assertEqual(group.distinct_storage_instances, 2)
        self.assertEqual(group.reclaimable_bytes, len(b"same-content"))
        self.assertEqual(
            {item.relative_path for item in group.files},
            {"original.jpg", "duplicate.jpg", "hardlink.jpg"},
        )

    def test_second_run_reuses_hashes_for_unchanged_fingerprints(self) -> None:
        (self.source / "a.jpg").write_bytes(b"same")
        (self.source / "b.jpg").write_bytes(b"same")

        with Catalog(self.catalog_path) as catalog:
            scan_sources(catalog, [self.source])
            first = find_exact_duplicates(catalog)
            second = find_exact_duplicates(catalog)

        self.assertEqual(first.hashed_files, 2)
        self.assertEqual(first.reused_hashes, 0)
        self.assertEqual(second.hashed_files, 0)
        self.assertEqual(second.reused_hashes, 2)
        self.assertEqual(second.total_group_count, 1)

    def test_file_changed_after_scan_is_reported_stale_and_not_grouped(self) -> None:
        first = self.source / "a.jpg"
        second = self.source / "b.jpg"
        first.write_bytes(b"same")
        second.write_bytes(b"same")

        with Catalog(self.catalog_path) as catalog:
            scan_sources(catalog, [self.source])
            second.write_bytes(b"changed")
            report = find_exact_duplicates(catalog)

        self.assertEqual(report.status, "warning")
        self.assertEqual(report.stale_files, 1)
        self.assertEqual(report.total_group_count, 0)

    def test_group_limit_is_bounded_at_the_api_boundary(self) -> None:
        with Catalog(self.catalog_path) as catalog:
            with self.assertRaisesRegex(ValueError, "group_limit must be between 1 and 10000"):
                find_exact_duplicates(catalog, group_limit=0)
            with self.assertRaisesRegex(ValueError, "group_limit must be between 1 and 10000"):
                find_exact_duplicates(catalog, group_limit=10_001)


if __name__ == "__main__":
    unittest.main()
