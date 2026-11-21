import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from photo_custodian.advisor import AdviceUnavailable, _NoRedirect, request_advice
from photo_custodian.cli import main


class AdvisorTests(unittest.TestCase):
    def test_request_sends_only_inventory_totals(self):
        response = {
            "status": "completed",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Revisá una carpeta."}],
            }],
        }
        with patch("photo_custodian.advisor.build_opener") as build_opener:
            opener = build_opener.return_value
            opener.open.return_value = io.BytesIO(json.dumps(response).encode())
            advice = request_advice(
                [{"media_kind": "image", "file_count": 2, "total_bytes": 12,
                  "path": "/private/photo.jpg"}],
                api_key="test-key",
                model="test-model",
            )
        self.assertEqual(advice, "Revisá una carpeta.")
        request = opener.open.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(json.loads(body["input"]), [
            {"media_kind": "image", "file_count": 2, "total_bytes": 12},
        ])
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)

    def test_redirect_cannot_forward_authorization(self):
        request = Request("https://api.openai.com/v1/responses")
        redirect = _NoRedirect().redirect_request(
            request, None, 302, "", {}, "https://example.com"
        )
        self.assertIsNone(redirect)

    def test_incomplete_response_is_unavailable(self):
        with patch("photo_custodian.advisor.build_opener") as build_opener:
            opener = build_opener.return_value
            opener.open.return_value = io.BytesIO(b'{"status":"incomplete","output":[]}')
            with self.assertRaises(AdviceUnavailable):
                request_advice([], api_key="test-key", model="test-model")

    def test_api_failure_keeps_local_next_step(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            (source / "sample.jpg").write_bytes(b"synthetic")
            catalog = str(Path(directory) / "catalog.sqlite3")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(["scan", str(source), "--catalog", catalog]), 0)
            with (
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}),
                patch("photo_custodian.cli.request_advice", side_effect=AdviceUnavailable("Unavailable.")),
                redirect_stdout(io.StringIO()) as output,
                redirect_stderr(io.StringIO()) as errors,
            ):
                status = main(["agent", "--openai", "--catalog", catalog])
            self.assertEqual(status, 1)
            self.assertIn("duplicates exact --limit 5", output.getvalue())
            self.assertIn("WARN: Unavailable.", errors.getvalue())
