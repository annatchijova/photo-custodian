from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from photo_custodian.catalog import CATALOG_SCHEMA_VERSION, SCHEMA_V1, Catalog


class MigrationTests(unittest.TestCase):
    def test_v1_catalog_migrates_to_v2_without_losing_sources_or_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.sqlite3"
            with sqlite3.connect(catalog_path) as connection:
                connection.executescript(SCHEMA_V1)
                connection.execute(
                    "INSERT INTO schema_metadata(singleton, schema_version) VALUES (1, 1)"
                )
                connection.execute("PRAGMA user_version = 1")
                connection.execute(
                    """
                    INSERT INTO sources(root_path, root_path_bytes, device)
                    VALUES ('/photos', ?, 7)
                    """,
                    (b"/photos",),
                )
                connection.execute("INSERT INTO scan_runs(source_id) VALUES (1)")
                connection.execute(
                    """
                    INSERT INTO files(
                        source_id, relative_path, relative_path_bytes, size_bytes,
                        mtime_ns, ctime_ns, device, inode, mode, media_kind,
                        extension, last_seen_run_id
                    ) VALUES (1, 'a.jpg', ?, 4, 10, 11, 7, 12, 33188,
                              'image', '.jpg', 1)
                    """,
                    (b"a.jpg",),
                )

            with Catalog(catalog_path) as catalog:
                file_count = catalog.connection.execute(
                    "SELECT COUNT(*) FROM files"
                ).fetchone()[0]
                hash_table = catalog.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'file_hashes'"
                ).fetchone()
                stored_version = catalog.connection.execute(
                    "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
                ).fetchone()[0]
                pragma_version = catalog.connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]

            self.assertEqual(file_count, 1)
            self.assertIsNotNone(hash_table)
            self.assertEqual(stored_version, CATALOG_SCHEMA_VERSION)
            self.assertEqual(pragma_version, CATALOG_SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
