"""Read-only collection into private, reviewable working directories."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from .advisor import _NoRedirect

_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_RESPONSE = 96 * 1024 * 1024
_MAX_ZIP_MEMBER = 4 * 1024**3
_MAX_ZIP_TOTAL = 128 * 1024**3
_MAX_ZIP_ENTRIES = 30_000
_TEXT_CHUNK_BYTES = 24_000


def _write(output: Path, data: bytes, *, replace: bool = False) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".intake-", dir=output.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, output)
        else:
            os.link(temporary, output)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _write_json(path: Path, data: dict, *, replace: bool = False) -> None:
    _write(path, json.dumps(data, indent=2, ensure_ascii=True).encode(), replace=replace)


def _gmail_get(opener, token: str, path: str) -> dict:
    request = Request(_GMAIL + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with opener.open(request, timeout=60) as response:
            raw = response.read(_MAX_RESPONSE + 1)
    except HTTPError as error:
        error.close()
        raise RuntimeError(f"Gmail download paused with HTTP {error.code}.") from None
    except (URLError, OSError, ValueError):
        raise RuntimeError("Gmail download could not reach the API.") from None
    if len(raw) > _MAX_RESPONSE:
        raise RuntimeError("Gmail response exceeds the download limit.")
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError):
        raise RuntimeError("Gmail returned an invalid response.") from None
    if not isinstance(result, dict):
        raise RuntimeError("Gmail returned an invalid record.")
    return result


def gmail(args) -> int:
    token = os.environ.get("GMAIL_ACCESS_TOKEN", "").strip()
    if not token:
        raise ValueError("Set GMAIL_ACCESS_TOKEN with a Gmail read-only OAuth access token.")
    if not args.query.strip() or len(args.query) > 1000 or not 1 <= args.pages <= 100:
        raise ValueError("Use a non-empty Gmail query and 1–100 pages per run.")
    output = args.output.expanduser()
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    profile = _gmail_get(opener, token, "profile")
    address = profile.get("emailAddress")
    if not isinstance(address, str) or not address:
        raise RuntimeError("Gmail did not identify the account.")
    identity = hashlib.sha256(json.dumps([address, args.query]).encode()).hexdigest()
    state_path = output / "gmail-download.json"
    if output.exists():
        if output.is_symlink() or not state_path.is_file() or state_path.is_symlink():
            raise ValueError("Use a new directory or the original Gmail download directory.")
        state = json.loads(state_path.read_text())
        if state.get("schema_version") != 1 or state.get("selection_id") != identity:
            raise ValueError("The Gmail account or query differs from the saved selection.")
    else:
        output.mkdir(mode=0o700)
        state = {"schema_version": 1, "selection_id": identity, "next_page": "", "complete": False}
        _write_json(state_path, state)
    downloaded = 0
    for _ in range(args.pages):
        if state["complete"]:
            break
        query = {"q": args.query, "maxResults": 25}
        if state["next_page"]:
            query["pageToken"] = state["next_page"]
        page = _gmail_get(opener, token, "messages?" + urlencode(query))
        messages = page.get("messages", [])
        next_page = page.get("nextPageToken", "")
        if not isinstance(messages, list) or len(messages) > 25 or not isinstance(next_page, str):
            raise RuntimeError("Gmail returned an invalid message page.")
        for message in messages:
            message_id = message.get("id") if isinstance(message, dict) else None
            if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
                raise RuntimeError("Gmail returned an invalid message ID.")
            file = output / (message_id + ".eml")
            metadata = output / (message_id + ".gmail.json")
            if os.path.lexists(file) and (file.is_symlink() or not file.is_file()):
                raise ValueError("A Gmail message output path is unavailable.")
            if file.is_file() and metadata.is_file() and not metadata.is_symlink():
                continue
            data = _gmail_get(opener, token, f"messages/{message_id}?format=raw")
            try:
                if data.get("id") != message_id:
                    raise ValueError
                encoded = data["raw"].encode("ascii")
                mime = base64.b64decode(encoded + b"=" * (-len(encoded) % 4),
                                        altchars=b"-_", validate=True)
                if not mime:
                    raise ValueError
            except (KeyError, TypeError, AttributeError, ValueError):
                raise RuntimeError("Gmail returned invalid raw message data.") from None
            digest = hashlib.sha256(mime).hexdigest()
            if file.is_file():
                with file.open("rb") as existing:
                    existing_digest = hashlib.file_digest(existing, "sha256").hexdigest()
                if existing_digest != digest:
                    raise ValueError("An existing Gmail message differs from the downloaded bytes.")
            else:
                _write(file, mime)
            _write_json(metadata, {
                "schema_version": 1, "message_id": message_id,
                "thread_id": data.get("threadId"), "gmail_internal_date": data.get("internalDate"),
                "raw_sha256": digest, "size_bytes": len(mime),
                "downloaded_utc": datetime.now(timezone.utc).isoformat(),
            })
            downloaded += 1
        state["next_page"] = next_page
        state["complete"] = not next_page
        state["updated_utc"] = datetime.now(timezone.utc).isoformat()
        _write_json(state_path, state, replace=True)
    print(json.dumps({"downloaded": downloaded, "complete": state["complete"]}))
    return 0


def _split_chat(file: Path, output: Path, key: str) -> list[dict]:
    pieces = []
    with file.open("rb") as handle:
        start = 0
        while data := handle.read(_TEXT_CHUNK_BYTES):
            if len(data) == _TEXT_CHUNK_BYTES:
                boundary = data.rfind(b"\n")
                if boundary > _TEXT_CHUNK_BYTES // 2:
                    handle.seek(boundary + 1 - len(data), os.SEEK_CUR)
                    data = data[:boundary + 1]
            name = f"{key}-text-{len(pieces):05}.txt"
            _write(output / name, data)
            pieces.append({"name": name, "byte_range": [start, start + len(data)]})
            start += len(data)
    return pieces


def whatsapp(args) -> int:
    source = args.export.expanduser()
    destination = args.output.expanduser()
    if not source.is_file() or source.is_symlink():
        raise ValueError("Select a regular WhatsApp ZIP or TXT export.")
    if source.suffix.lower() not in (".zip", ".txt"):
        raise ValueError("Select a WhatsApp ZIP or TXT export.")
    with source.open("rb") as input_file:
        observed = os.fstat(input_file.fileno())
        source_digest = hashlib.file_digest(input_file, "sha256").hexdigest()
    destination.mkdir(mode=0o700)
    records = []
    if source.suffix.lower() == ".txt":
        if source.stat().st_size > 256 * 1024**2:
            raise ValueError("The text export exceeds the size limit.")
        records.append({"source_entry": source.name, "pieces": _split_chat(source, destination, "chat")})
    else:
        with zipfile.ZipFile(source) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if (len(members) > _MAX_ZIP_ENTRIES
                    or sum(member.file_size for member in members) > _MAX_ZIP_TOTAL
                    or any(member.file_size > _MAX_ZIP_MEMBER for member in members)):
                raise ValueError("WhatsApp ZIP exceeds import limits.")
            raw = destination / "members"
            raw.mkdir(mode=0o700)
            for index, member in enumerate(members, 1):
                suffix = PurePosixPath(member.filename).suffix.lower()
                suffix = suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ".bin"
                key = f"member-{index:06}"
                target = raw / (key + suffix)
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                member_hash = hashlib.sha256()
                count = 0
                with os.fdopen(descriptor, "wb") as out, archive.open(member) as item:
                    while block := item.read(1024 * 1024):
                        count += len(block)
                        if count > member.file_size:
                            raise ValueError("ZIP member exceeds its declared size.")
                        member_hash.update(block)
                        out.write(block)
                if count != member.file_size:
                    raise ValueError("ZIP member is incomplete.")
                record = {
                    "source_entry": member.filename, "entry_index": index, "crc32": member.CRC,
                    "name": str(target.relative_to(destination)), "sha256": member_hash.hexdigest(),
                }
                if suffix == ".txt" and member.file_size <= 256 * 1024**2:
                    record["pieces"] = _split_chat(target, destination, key)
                records.append(record)
    current = source.stat()
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
        observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns, observed.st_ctime_ns
    ):
        raise ValueError("WhatsApp source changed during intake.")
    _write_json(destination / "whatsapp-intake.json", {
        "schema_version": 1, "scope": "working derivatives of a supplied export",
        "source_name": source.name, "source_sha256": source_digest, "entries": records,
    })
    print(json.dumps({"entries": len(records)}))
    return 0
