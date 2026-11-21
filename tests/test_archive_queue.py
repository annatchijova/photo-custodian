from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from photo_custodian.advisor import AdviceUnavailable
from photo_custodian.archive_evidence import _capture
from photo_custodian.cli import main


class ArchiveQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.catalog = self.root / "catalog.sqlite3"
        self.documents = [b"synthetic first item", b"synthetic later item"]

    def command(self, *args, expected=0):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            status = main([*args, "--catalog", str(self.catalog)])
        self.assertEqual(status, expected, errors.getvalue())

    def prepare_sources(self):
        for index, data in enumerate(self.documents, 1):
            (self.source / f"{index}.txt").write_bytes(data)
        self.command("scan", str(self.source))
        return ["analyze", "--source", str(self.source), "--openai"]

    def test_item_rejected_advances_queue_and_is_retried_only_on_request(self):
        args = self.prepare_sources()
        failed = []

        def fake_model(content, instructions):
            supplied = json.loads(content[0]["text"])
            failed.append(supplied["extracted_text"])
            if len(failed) == 1:
                return {"category": "Invalid category"}
            return {
                "category": "Other", "title": "", "summary": "", "saved_reason_hypothesis": "",
                "important_information": "", "suggested_rule": "", "vision_transcription": "",
                "defense_lead": "PRIVATE_MODEL_FIXTURE", "complaint_lead": "",
            }

        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}),
            patch("photo_custodian.inference.Inference.structured", side_effect=fake_model),
        ):
            self.command(*args, expected=1)
            self.assertCountEqual(failed, [item.decode() for item in self.documents])
            rejected = failed[0]
            with closing(sqlite3.connect(self.catalog)) as connection:
                rows = connection.execute("""
                    SELECT status, extracted_text, json_extract(advice, '$._triage_state')
                    FROM archive_analysis
                """).fetchall()
            self.assertIn(("extracted", rejected, "rejected"), rows)
            self.assertEqual(sum(row[0] == "advised" for row in rows), 1)
            self.command(*args, expected=3)
            self.assertEqual(len(failed), 2)
            self.command(*args, "--retry-triage")
            self.assertEqual(failed[-1], rejected)
        for suffix in ("json", "html"):
            output = self.root / f"private.{suffix}"
            self.command("case-handoff", "--output", str(output))
            text = output.read_text()
            for private in ("PRIVATE_MODEL_FIXTURE", "synthetic first item", "synthetic later item"):
                self.assertNotIn(private, text)
            if suffix == "json":
                items = json.loads(text)["items"]
                self.assertEqual(len(items), 2)
                self.assertTrue(all(item["proposed_lanes"] == ["defense"] for item in items))
                self.assertTrue(all(item["text_coverage"] == "bounded" for item in items))
            else:
                self.assertIn("Defensa", text)

    def test_provider_failure_pauses_after_saving_only_the_current_extraction(self):
        args = self.prepare_sources()
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-key"}),
            patch("photo_custodian.archive_advisor.analyze",
                  side_effect=AdviceUnavailable("Synthetic provider unavailable")),
        ):
            self.command(*args, expected=2)
        with closing(sqlite3.connect(self.catalog)) as connection:
            rows = connection.execute("SELECT status, extracted_text, advice FROM archive_analysis").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "extracted")
        self.assertIn(rows[0][1].encode(), self.documents)
        self.assertNotIn("_triage_state", json.loads(rows[0][2]))

    def test_extractor_subprocess_receives_no_provider_credentials(self):
        script = (
            "import json, os; print(json.dumps({key: key in os.environ for key in "
            "('OPENAI_API_KEY', 'GMAIL_ACCESS_TOKEN')}))"
        )
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-openai",
                                    "GMAIL_ACCESS_TOKEN": "synthetic-gmail"}),
            tempfile.TemporaryFile() as source,
        ):
            result = _capture(sys.executable, ["-c", script], limit=512, fd=source.fileno())
        self.assertEqual(json.loads(result), {"OPENAI_API_KEY": False, "GMAIL_ACCESS_TOKEN": False})
