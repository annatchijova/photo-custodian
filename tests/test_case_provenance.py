from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
import zipfile
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

    def test_whatsapp_text_fragment_is_bound_to_a_private_receipt_and_mutation_unlinks_it(self):
        originals = self.root / "originals"
        originals.mkdir()
        export = originals / "private-chat.txt"
        export.write_bytes(b"first synthetic line\nsecond synthetic line\n")
        staged = self.root / "whatsapp-working"
        self.command("intake-whatsapp", str(export), "--output", str(staged), catalog=False)
        fragment, = staged.glob("chat-text-*.txt")
        self.command("scan", str(originals))
        self.command("scan", str(staged))
        self.command("analyze", "--source", str(staged))
        item, raw = self.handoff_item(self.file_id(fragment.name), "whatsapp.json")
        lineage = item["lineage"]
        self.assertEqual(lineage["kind"], "whatsapp_text_fragment")
        self.assertEqual(lineage["range_basis"], "export_bytes")
        self.assertEqual(lineage["entry_index"], 0)
        self.assertEqual(lineage["parent_sha256"], hashlib.sha256(export.read_bytes()).hexdigest())
        self.assertNotIn("private-chat", raw)
        self.assertNotIn("first synthetic line", raw)

        fragment.write_bytes(b"FIRST synthetic line\nsecond synthetic line\n")
        self.command("scan", str(staged))
        self.command("analyze", "--source", str(staged), expected=1)
        item, _ = self.handoff_item(self.file_id(fragment.name), "whatsapp-mutated.json")
        self.assertEqual(item["lineage"], {"status": "unverified"})

    def test_whatsapp_zip_fragments_distinguish_duplicate_member_names_without_disclosing_them(self):
        originals = self.root / "originals"
        originals.mkdir()
        export = originals / "private-export.zip"
        with zipfile.ZipFile(export, "w") as archive:
            archive.writestr("same-name.txt", b"first duplicate member\n")
            archive.writestr("same-name.txt", b"second duplicate member\n")
        staged = self.root / "whatsapp-working"
        self.command("intake-whatsapp", str(export), "--output", str(staged), catalog=False)
        fragments = sorted(staged.glob("member-*-text-*.txt"))
        self.assertEqual(len(fragments), 2)
        self.command("scan", str(originals))
        self.command("scan", str(staged))
        self.command("analyze", "--source", str(staged), "--limit", "10")
        rows = []
        report = self.root / "zip-whatsapp.json"
        self.command("case-handoff", "--output", str(report))
        for fragment in fragments:
            file_id = self.file_id(fragment.name)
            rows.append(next(item for item in json.loads(report.read_text())["items"]
                             if item["file_id"] == file_id)["lineage"])
        self.assertEqual([row["entry_index"] for row in rows], [1, 2])
        self.assertTrue(all(row["range_basis"] == "decompressed_zip_member_bytes" for row in rows))
        raw = report.read_text()
        self.assertNotIn("same-name", raw)
        self.assertNotIn("duplicate member", raw)

    def test_whatsapp_zip_fragment_rejects_a_same_size_wrong_member_range(self):
        export = self.root / "private-export.zip"
        content = b"A" * 24_000 + b"B" * 4_000
        with zipfile.ZipFile(export, "w") as archive:
            archive.writestr("chat.txt", content)
        staged = self.root / "whatsapp-working"
        self.command("intake-whatsapp", str(export), "--output", str(staged), catalog=False)
        fragment, = staged.glob("member-000001-text-00000.txt")
        receipt_path = staged / ".whatsapp-lineage" / (fragment.name + ".json")
        receipt = json.loads(receipt_path.read_text())
        receipt["byte_range"] = [1, len(fragment.read_bytes()) + 1]
        receipt_path.write_text(json.dumps(receipt))
        self.command("scan", str(staged))
        self.command("analyze", "--source", str(staged), expected=1)
        item, _ = self.handoff_item(self.file_id(fragment.name), "wrong-range.json")
        self.assertEqual(item["lineage"], {"status": "unverified"})

    def test_timeline_preserves_source_clock_domains_and_omits_private_names(self):
        first, second = self.root / "first-source", self.root / "second-source"
        first.mkdir()
        second.mkdir()
        private_a, private_a2 = first / "private-alpha.txt", first / "private-alpha-2.txt"
        private_b, private_b2 = second / "private-beta.txt", second / "private-beta-2.txt"
        private_a.write_text("synthetic private alpha")
        private_a2.write_text("synthetic private alpha two")
        private_b.write_text("synthetic private beta")
        private_b2.write_text("synthetic private beta two")
        os.utime(private_a, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_001))
        os.utime(private_a2, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_002))
        os.utime(private_b, ns=(1_699_000_000_000_000_000, 1_699_000_000_000_000_002))
        os.utime(private_b2, ns=(1_699_000_000_000_000_000, 1_699_000_000_000_000_001))
        self.command("scan", str(first), str(second))
        output = self.root / "timeline.json"
        self.command("timeline", "--output", str(output))
        payload = json.loads(output.read_text())
        self.assertEqual(payload["header"]["timestamp_scope"], "recorded filesystem metadata; not event time")
        self.assertEqual(payload["header"]["cross_source_ordering"], "not_established_without_anchor")
        self.assertEqual(len(payload["tracks"]), 2)
        for rows in payload["tracks"].values():
            self.assertTrue(all(row["sequence_scope"] == "within_source_only" for row in rows))
            self.assertTrue(all(row["epistemic_level"] == "recorded_metadata" for row in rows))
            self.assertEqual([row["recorded_filesystem_mtime_ns"] for row in rows],
                             sorted(row["recorded_filesystem_mtime_ns"] for row in rows))
        raw = output.read_text()
        self.assertNotIn("private-alpha", raw)
        self.assertNotIn("synthetic private beta", raw)
        html = self.root / "timeline.html"
        self.command("timeline", "--output", str(html))
        rendered = html.read_text()
        self.assertIn("2023-11-14T22:13:20.000000001+00:00", rendered)
        self.assertIn("2023-11-14T22:13:20.000000002+00:00", rendered)
        self.assertEqual(stat.S_IMODE(html.stat().st_mode), 0o600)
