"""Explicit hosted or loopback inference with no provider fallback."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .advisor import AdviceUnavailable, _NoRedirect, _request_text

_OLLAMA = "http://127.0.0.1:11434/api/chat"
_RESPONSE_LIMIT = 256 * 1024


@dataclass(frozen=True)
class Inference:
    provider: str
    model: str
    api_key: str = ""

    def structured(self, content: list[dict[str, str]], instructions: str) -> dict:
        if self.provider == "openai":
            response = _request_text({
                "model": self.model, "store": False, "max_output_tokens": 1100,
                "text": {"format": {"type": "json_object"}},
                "instructions": instructions,
                "input": [{"role": "user", "content": content}],
            }, api_key=self.api_key)
        else:
            response = self._ollama(content, instructions)
        try:
            record = json.loads(response)
        except (ValueError, RecursionError):
            raise AdviceUnavailable("The model returned invalid JSON.") from None
        if not isinstance(record, dict):
            raise AdviceUnavailable("The model returned an invalid record.")
        return record

    def _ollama(self, content: list[dict[str, str]], instructions: str) -> str:
        images = [block["image_url"].split(",", 1)[1] for block in content
                  if block["type"] == "input_image"]
        user = {"role": "user", "content": "\n".join(
            block["text"] for block in content if block["type"] == "input_text"
        )}
        if images:
            user["images"] = images
        payload = {
            "model": self.model, "stream": False, "format": "json",
            "options": {"num_predict": 1100},
            "messages": [{"role": "system", "content": instructions}, user],
        }
        request = Request(_OLLAMA, data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=60) as response:
                raw = response.read(_RESPONSE_LIMIT + 1)
        except HTTPError as error:
            error.close()
            raise AdviceUnavailable(f"Ollama returned HTTP {error.code}.") from None
        except (URLError, OSError, ValueError):
            raise AdviceUnavailable("Could not reach Ollama on this machine.") from None
        if len(raw) > _RESPONSE_LIMIT:
            raise AdviceUnavailable("Ollama response exceeded the size limit.")
        try:
            result = json.loads(raw)
            content = result["message"]["content"]
            if result["done"] is True and isinstance(content, str) and content.strip():
                return content
        except (TypeError, KeyError, ValueError, RecursionError):
            pass
        raise AdviceUnavailable("Ollama returned no usable advice.")


def select_inference(args) -> Inference | None:
    if args.openai:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise AdviceUnavailable("Set OPENAI_API_KEY to enable OpenAI.")
        return Inference("openai", args.model or os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini", key)
    if args.ollama:
        model = args.model or os.environ.get("OLLAMA_MODEL", "")
        if not model or model.endswith(("-cloud", ":cloud")) or "/" in model:
            raise AdviceUnavailable("Select an installed local Ollama model with --model or OLLAMA_MODEL.")
        return Inference("ollama", model)
    return None
