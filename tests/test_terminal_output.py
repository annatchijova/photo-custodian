from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from photo_custodian.cli import main


class TerminalOutputTests(unittest.TestCase):
    def test_exact_duplicate_paths_escape_control_characters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "source"
            source.mkdir()
            (source / "line\nbreak.jpg").write_bytes(b"same")
            (source / "ordinary.jpg").write_bytes(b"same")
            catalog_path = base / "catalog.sqlite3"

            with contextlib.redirect_stdout(io.StringIO()):
                main(["scan", str(source), "--catalog", str(catalog_path)])
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = main(
                    ["duplicates", "exact", "--catalog", str(catalog_path)]
                )

        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("line\\nbreak.jpg", output)
        self.assertNotIn("line\nbreak.jpg", output)


if __name__ == "__main__":
    unittest.main()
