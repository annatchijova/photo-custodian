from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from photo_custodian.catalog import Catalog
from photo_custodian.scanner import scan_registered_source, scan_sources


class RegisteredSourceScanTests(unittest.TestCase):
    def test_attachment_change_after_walk_rolls_back_changes_and_missing_marks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "working-copy"
            source.mkdir()
            previous = source / "previous.txt"
            previous.write_bytes(b"synthetic original")
            with Catalog(base / "catalog.sqlite3") as catalog:
                scan_sources(catalog, [source])
                source_id = catalog.connection.execute("SELECT id FROM sources").fetchone()[0]
                old_file = tuple(catalog.connection.execute(
                    "SELECT id, relative_path, status, last_seen_run_id FROM files"
                ).fetchone())
                previous.rename(source / "renamed.txt")
                metadata = source.stat()

                # The attachment is stable before the walk and changes at the
                # final publication check, after the missing update was staged.
                unchanged = Mock(side_effect=[True, True, False])
                report = scan_registered_source(
                    catalog, source_id, source,
                    identity=(metadata.st_dev, metadata.st_ino), unchanged=unchanged,
                )

                self.assertEqual(report.status, "warning")
                self.assertEqual(report.missing_files, 0)
                rows = catalog.connection.execute(
                    "SELECT id, relative_path, status, last_seen_run_id FROM files"
                ).fetchall()
                self.assertEqual([tuple(row) for row in rows], [old_file])
                self.assertEqual(catalog.connection.execute(
                    "SELECT COUNT(*) FROM scan_runs"
                ).fetchone()[0], 1)
                self.assertEqual(catalog.connection.execute(
                    "SELECT status FROM sources WHERE id = ?", (source_id,)
                ).fetchone()[0], "unavailable")
