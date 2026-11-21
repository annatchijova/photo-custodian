"""Optional OpenAI advice over aggregate inventory data."""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

_ENDPOINT = "https://api.openai.com/v1/responses"
_MAX_RESPONSE_BYTES = 65536


class AdviceUnavailable(RuntimeError):
    """The API did not provide usable advisory text."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def next_step(rows: list[dict[str, int | str]]) -> str:
    if not rows:
        return "Scan one source folder to start: photo-custodian scan /path/to/folder"
    return "Review a small batch of exact duplicates: photo-custodian duplicates exact --limit 5"


def request_advice(
    rows: list[dict[str, int | str]], *, api_key: str, model: str
) -> str:
    aggregates = [
        {field: row[field] for field in ("media_kind", "file_count", "total_bytes")}
        for row in rows
        if row["media_kind"] in ("image", "video", "other")
    ]
    return _request_text({
            "model": model,
            "store": False,
            "max_output_tokens": 300,
            "instructions": (
                "Give one small, concrete task for organizing a local media library "
                "in Rioplatense Spanish, using voseo and at most three sentences. "
                "The input is fixed inventory data. Do not repeat or alter its figures, "
                "infer file contents or duplicate counts, issue commands, or suggest "
                "moving, renaming, or deleting files. Help the user decide what to "
                "review visually in a short session."
            ),
            "input": json.dumps(aggregates),
        }, api_key=api_key)


def _request_text(payload: dict[str, object], *, api_key: str) -> str:
    request = Request(
        _ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        error.close()
        raise AdviceUnavailable(f"OpenAI returned HTTP {error.code}.") from None
    except (URLError, OSError, ValueError):
        raise AdviceUnavailable("Could not reach OpenAI.") from None

    if len(raw) > _MAX_RESPONSE_BYTES:
        raise AdviceUnavailable("OpenAI response exceeded the size limit.")
    try:
        payload = json.loads(raw)
        if payload["status"] == "completed":
            parts = [
                part["text"]
                for item in payload["output"]
                if item["type"] == "message" and item["role"] == "assistant"
                for part in item["content"]
                if part["type"] == "output_text"
            ]
            advice = " ".join(" ".join(parts).split())
            if advice:
                return advice
    except (KeyError, TypeError, ValueError):
        pass
    raise AdviceUnavailable("OpenAI did not return usable advice.")
