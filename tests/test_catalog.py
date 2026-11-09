from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from photo_custodian.catalog import CATALOG_SCHEMA_VERSION, Catalog


class CatalogTests(unittest.TestCase):
    def test_new_catalog_stamps_current_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.sqlite3"

            with Catalog(catalog_path):
                pass

            with sqlite3.connect(catalog_path) as connection:
                stored_version = connection.execute(
                    "SELECT schema_version FROM schema_metadata WHERE singleton = 1"
                ).fetchone()[0]
                pragma_version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertEqual(stored_version, CATALOG_SCHEMA_VERSION)
            self.assertEqual(pragma_version, CATALOG_SCHEMA_VERSION)

    def test_future_schema_is_rejected_with_a_named_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.sqlite3"
            with sqlite3.connect(catalog_path) as connection:
                connection.execute("PRAGMA user_version = 999")

            with self.assertRaisesRegex(
                RuntimeError, "catalog schema version 999 is newer"
            ):
                Catalog(catalog_path)


if __name__ == "__main__":
    unittest.main()
