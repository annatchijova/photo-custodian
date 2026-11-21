from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from pathlib import Path

from photo_custodian.cli import main

_MAIL = (
    b"From: private-fixture@example.invalid\r\n"
    b"MIME-Version: 1.0\r\n"
    b"Content-Type: multipart/mixed; boundary=fixture\r\n\r\n"
    b"--fixture\r\nContent-Type: text/plain\r\n\r\nfixture message\r\n"
    b"--fixture\r\nContent-Type: text/plain\r\n"
    b"Content-Disposition: attachment; filename=secret-fixture.txt\r\n\r\n"
    b"synthetic attachment text\r\n--fixture--\r\n"
)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.catalog = self.root / "catalog.sqlite3"

    def command(self, *args, expected=0, catalog=True):
        output, errors = io.StringIO(), io.StringIO()
        argv = list(args)
        if catalog:
            argv += ["--catalog", str(self.catalog)]
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(argv)
        self.assertEqual(code, expected, errors.getvalue())
        return output.getvalue(), errors.getvalue()

    def file_id(self, filename):
        with closing(sqlite3.connect(self.catalog)) as connection:
            return connection.execute(
                "SELECT id FROM files WHERE relative_path = ?", (filename,)
            ).fetchone()[0]

    def case_fixture(self):
        mail_dir = self.root / "download"
        mail_dir.mkdir()
        parent = mail_dir / "case-fixture.eml"
        parent.write_bytes(_MAIL)
        parts = self.root / "parts"
        self.command("stage-mail-attachments", "--source", str(mail_dir),
                     "--output", str(parts), catalog=False)
        child, = parts.glob("*/part-*.txt")
        self.assertEqual(child.read_bytes(), b"synthetic attachment text")
        self.command("scan", str(mail_dir))
        self.command("scan", str(parts))
        self.command("analyze", "--source", str(parts))
        child_id = self.file_id(str(child.relative_to(parts)))
        parent_id = self.file_id(parent.name)
        manifest = self.root / "selection.json"
        out, _ = self.command("manifest", str(parent_id), "--output", str(manifest))
        return parent, child, child_id, parent_id, manifest, json.loads(out)["payload_sha256"]

    def handoff_item(self, file_id, name):
        report = self.root / name
        self.command("case-handoff", "--output", str(report))
        raw = report.read_text()
        item, = (row for row in json.loads(raw)["items"] if row["file_id"] == file_id)
        return item, raw

    def test_handoff_binds_mime_part_and_only_reports_latest_parent_observation(self):
        parent, child, child_id, parent_id, manifest, digest = self.case_fixture()
        args = ["verify-lineage-parents", str(manifest), "--root", str(parent.parent),
                "--expected-sha256", digest]
        before, _ = self.handoff_item(child_id, "before.json")
        self.assertEqual(before["lineage"]["parent_byte_check"], {"status": "not_checked"})

        self.command(*args)
        item, raw = self.handoff_item(child_id, "checked.json")
        lineage = item["lineage"]
        self.assertEqual(lineage["part_index"], 3)
        self.assertEqual(lineage["parent_sha256"], hashlib.sha256(_MAIL).hexdigest())
        self.assertEqual(lineage["derivative_sha256"], hashlib.sha256(child.read_bytes()).hexdigest())
        self.assertEqual(lineage["parent_byte_check"]["status"], "match")
        self.assertEqual(lineage["parent_byte_check"]["manifest_file_id"], parent_id)
        self.assertEqual(lineage["parent_byte_check"]["manifest_sha256"], digest)
        for private in (parent.name, str(child), "secret-fixture", "synthetic attachment text"):
            self.assertNotIn(private, raw)

        parent.write_bytes(b"X" + _MAIL[1:])
        self.command(*args, expected=1)
        item, _ = self.handoff_item(child_id, "changed.json")
        self.assertEqual(item["lineage"]["parent_byte_check"]["status"], "mismatch")
        self.assertEqual(item["lineage"]["parent_sha256_basis"], "declared_by_receipt")
        with closing(sqlite3.connect(self.catalog)) as connection:
            self.assertEqual(
                connection.execute("SELECT status FROM parent_byte_checks ORDER BY id").fetchall(),
                [("match",), ("mismatch",)],
            )

    def test_wrong_manifest_anchor_creates_no_parent_observation(self):
        parent, _, _, _, manifest, _ = self.case_fixture()
        _, errors = self.command(
            "verify-lineage-parents", str(manifest), "--root", str(parent.parent),
            "--expected-sha256", "0" * 64, expected=2,
        )
        self.assertIn("independently supplied digest", errors)
        with closing(sqlite3.connect(self.catalog)) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM parent_byte_checks"
            ).fetchone()[0], 0)

    def test_manifest_verifier_rejects_symlink_even_when_target_bytes_match(self):
        parent, _, _, parent_id, manifest, digest = self.case_fixture()
        copied = self.root / "verification-root"
        copied.mkdir()
        (copied / parent.name).symlink_to(parent)
        out, _ = self.command("verify-manifest", str(manifest), "--root", str(copied),
                              "--expected-sha256", digest, expected=1, catalog=False)
        self.assertEqual(json.loads(out)["files"], [{"file_id": parent_id, "status": "unavailable"}])

    def test_changed_derivative_cannot_reuse_an_older_lineage_record(self):
        _, child, child_id, _, _, _ = self.case_fixture()
        parts = child.parent.parent
        child.write_bytes(b"Synthetic attachment text")
        self.command("scan", str(parts))
        self.command("analyze", "--source", str(parts), expected=1)
        item, _ = self.handoff_item(child_id, "changed-child.json")
        self.assertEqual(item["lineage"], {"status": "unverified"})

    def test_gmail_audit_compares_bytes_and_detects_an_orphan_receipt(self):
        download = self.root / "gmail"
        download.mkdir()
        message = download / "example.eml"
        message.write_bytes(_MAIL)
        receipt = {
            "schema_version": 1, "message_id": "example", "size_bytes": len(_MAIL),
            "raw_sha256": hashlib.sha256(_MAIL).hexdigest(),
        }
        (download / "example.gmail.json").write_text(json.dumps(receipt))
        argv = ["verify-mail-intake", "--source", str(download)]
        out, _ = self.command(*argv, catalog=False)
        self.assertEqual(json.loads(out), {"checked": 1, "match": 1, "unmatched": 0})

        message.write_bytes(b"X" + _MAIL[1:])
        (download / "orphan.gmail.json").write_text(json.dumps({**receipt, "message_id": "orphan"}))
        out, _ = self.command(*argv, expected=1, catalog=False)
        self.assertEqual(json.loads(out), {"checked": 2, "match": 0, "unmatched": 2})

    def test_metadata_handoff_and_catalog_are_owner_only(self):
        media = self.root / "source"
        media.mkdir()
        (media / "sample.txt").write_text("synthetic")
        self.command("scan", str(media))
        self.catalog.chmod(0o644)
        self.command("inventory")
        report = self.root / "handoff.html"
        self.command("case-handoff", "--output", str(report))
        self.assertEqual(stat.S_IMODE(self.catalog.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
        self.assertNotIn("sample.txt", report.read_text())
