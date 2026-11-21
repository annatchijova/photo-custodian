import io
import os
import random
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from photo_custodian.cli import main

try:
    from PIL import Image
except ImportError:
    Image = None


@unittest.skipUnless(Image, "install the review extra for image analysis")
class ReviewReportTests(unittest.TestCase):
    def test_report_separates_copies_and_versions_and_preserves_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "library"
            source.mkdir()
            original = source / "<script>-original.png"
            randomizer = random.Random(7)
            photo = Image.new("RGB", (16, 16))
            photo.putdata([tuple(randomizer.randrange(256) for _ in range(3))
                           for _ in range(256)])
            photo = photo.resize((320, 320), Image.Resampling.NEAREST)
            photo.save(original)
            (source / "copy.png").write_bytes(original.read_bytes())
            photo.resize((160, 160)).save(source / "small.jpg", quality=92)
            photo.transpose(Image.Transpose.ROTATE_90).save(source / "different.png")
            before = {p.name: p.read_bytes() for p in source.iterdir()}
            catalog = root / "catalog.sqlite3"
            output = root / "review.html"
            options = ["--catalog", str(catalog)]
            report = ["report", *options, "--output", str(output)]
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["scan", str(source), *options]), 0)
                with patch("photo_custodian.review_report.suggest_category") as categorize:
                    self.assertEqual(main(report), 0)
                    categorize.assert_not_called()
                self.assertEqual(main(report), 2)
            html = output.read_text()
            self.assertIn("2 identical paths", html)
            self.assertIn("2 visually similar photos", html)
            self.assertIn("320 × 320", html)
            self.assertIn("160 × 160", html)
            self.assertIn("&lt;script&gt;-original.png", html)
            self.assertNotIn("<script>-original.png", html)
            self.assertEqual(before, {p.name: p.read_bytes() for p in source.iterdir()})

            with sqlite3.connect(catalog) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM photo_signatures"
                ).fetchone()[0], 3)
                file_id = connection.execute(
                    "SELECT id FROM files WHERE relative_path = 'small.jpg'"
                ).fetchone()[0]
            categorized = root / "categories.html"
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                patch("photo_custodian.review_report.suggest_category",
                      return_value="Nature and animals") as categorize,
            ):
                self.assertEqual(main([
                    "report", *options, "--output", str(categorized),
                    "--categorize", str(file_id),
                ]), 0)
                categorize.assert_called_once()
                self.assertTrue(categorize.call_args.args[0].startswith("data:image/jpeg;base64,"))
            self.assertIn("Nature and animals", categorized.read_text())

            (source / "small.jpg").write_bytes(b"changed")
            stale = root / "stale.html"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(main(["report", *options, "--output", str(stale)]), 1)
            self.assertIn("Skipped 1 unreadable", stale.read_text())
            self.assertNotIn("2 visually similar photos", stale.read_text())
