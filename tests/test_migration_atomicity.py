from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from photo_custodian.catalog import SCHEMA_V1, Catalog


class MigrationAtomicityTests(unittest.TestCase):
    def test_failed_v1_to_v2_migration_rolls_back_version_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.sqlite3"
            with sqlite3.connect(catalog_path) as connection:
                connection.executescript(SCHEMA_V1)
                connection.execute(
                    "INSERT INTO schema_metadata(singleton, schema_version) VALUES (1, 1)"
                )
                connection.execute("PRAGMA user_version = 1")
                connection.execute("CREATE TABLE file_hashes(marker TEXT)")

            with self.assertRaisesRegex(sqlite3.OperationalError, "already exists"):
                Catalog(catalog_path)

            with sqlite3.connect(catalog_path) as connection:
                metadata_version = connection.execute(
                    "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
                ).fetchone()[0]
                pragma_version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertEqual(metadata_version, 1)
            self.assertEqual(pragma_version, 1)


if __name__ == "__main__":
    unittest.main()
