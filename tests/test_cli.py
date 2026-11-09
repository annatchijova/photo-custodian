from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from photo_custodian.cli import main


class CliTests(unittest.TestCase):
    def test_invalid_scan_root_returns_two_with_boundary_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            catalog_path = base / "catalog.sqlite3"
            invalid_root = base / "file.txt"
            invalid_root.write_text("not a directory", encoding="utf-8")
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                result = main(
                    ["scan", str(invalid_root), "--catalog", str(catalog_path)]
                )

        self.assertEqual(result, 2)
        self.assertIn("is not a directory", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
