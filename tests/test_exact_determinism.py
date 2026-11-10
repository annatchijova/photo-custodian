from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from photo_custodian.catalog import Catalog
from photo_custodian.exact_duplicates import find_exact_duplicates
from photo_custodian.scanner import scan_sources


class ExactDuplicateDeterminismTests(unittest.TestCase):
    def test_creation_and_traversal_order_do_not_change_exact_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first_source = base / "first"
            second_source = base / "second"
            first_source.mkdir()
            second_source.mkdir()

            for name, content in (("a.jpg", b"same"), ("b.jpg", b"same"), ("c.jpg", b"nope")):
                (first_source / name).write_bytes(content)
            for name, content in (("c.jpg", b"nope"), ("b.jpg", b"same"), ("a.jpg", b"same")):
                (second_source / name).write_bytes(content)

            first_catalog_path = base / "first.sqlite3"
            second_catalog_path = base / "second.sqlite3"
            with Catalog(first_catalog_path) as catalog:
                scan_sources(catalog, [first_source])
                first_report = find_exact_duplicates(catalog)
            with Catalog(second_catalog_path) as catalog:
                scan_sources(catalog, [second_source])
                second_report = find_exact_duplicates(catalog)

        def normalized(report: object) -> list[tuple[str, int, tuple[str, ...]]]:
            return [
                (
                    group.sha256,
                    group.size_bytes,
                    tuple(sorted(item.relative_path for item in group.files)),
                )
                for group in report.groups
            ]

        self.assertEqual(normalized(first_report), normalized(second_report))


if __name__ == "__main__":
    unittest.main()
