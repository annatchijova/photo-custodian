from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from photo_custodian.cli import main


class ExactDuplicateCliTests(unittest.TestCase):
    def test_duplicates_exact_emits_bounded_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "a.jpg").write_bytes(b"same")
            (source / "b.jpg").write_bytes(b"same")
            catalog_path = base / "catalog.sqlite3"

            with contextlib.redirect_stdout(io.StringIO()):
                scan_result = main(
                    ["scan", str(source), "--catalog", str(catalog_path), "--json"]
                )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                duplicate_result = main(
                    [
                        "duplicates",
                        "exact",
                        "--catalog",
                        str(catalog_path),
                        "--limit",
                        "10",
                        "--json",
                    ]
                )

        report = json.loads(stdout.getvalue())
        self.assertEqual(scan_result, 0)
        self.assertEqual(duplicate_result, 0)
        self.assertEqual(report["total_group_count"], 1)
        self.assertEqual(report["groups"][0]["file_count"], 2)


if __name__ == "__main__":
    unittest.main()
