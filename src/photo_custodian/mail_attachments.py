"""Stage decoded MIME attachments as traceable, private working derivatives."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter
from email import policy
from email.parser import BytesParser
from pathlib import Path
from shutil import rmtree

from .intake import _write, _write_json, verify_staged_file
from .manifest_verify import file_fingerprint

_MAX_MESSAGE = 64 * 1024 * 1024
_MAX_PARTS = 1000
_MAX_DECODED = 64 * 1024 * 1024
_OPEN_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _read_message(source: Path) -> tuple[bytes, tuple[int, ...]]:
    with os.fdopen(os.open(source, _OPEN_FILE), "rb") as handle:
        metadata = os.fstat(handle.fileno())
        original = file_fingerprint(metadata)
        if not stat.S_ISREG(metadata.st_mode) or original[0] > _MAX_MESSAGE:
            raise ValueError("message_unavailable_or_too_large")
        raw = handle.read(_MAX_MESSAGE + 1)
        if (len(raw) != original[0] or file_fingerprint(os.fstat(handle.fileno())) != original
                or file_fingerprint(source.lstat()) != original):
            raise ValueError("message_changed_while_reading")
    return raw, original


def _suffix(filename: str | None) -> str:
    extension = Path(filename or "").suffix.lower()
    return extension if re.fullmatch(r"\.[a-z0-9]{1,10}", extension) else ".bin"


def _extract(raw: bytes, destination: Path) -> tuple[list[dict], list[dict]]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    entries = []
    unavailable = []
    decoded_bytes = 0
    for part_index, part in enumerate(message.walk(), 1):
        if part_index > _MAX_PARTS:
            unavailable.append({"reason": "mime_part_limit", "first_unprocessed_index": part_index})
            break
        name = part.get_filename()
        disposition = part.get_content_disposition()
        content_type = part.get_content_type()
        inline_image = content_type.startswith("image/") and part.get("Content-ID") is not None
        if disposition != "attachment" and not name and not inline_image:
            continue
        if part.is_multipart():
            unavailable.append({"part_index": part_index, "reason": "nested_mime_attachment"})
            continue
        data = part.get_payload(decode=True)
        if not isinstance(data, bytes) or part.defects:
            unavailable.append({"part_index": part_index, "reason": "invalid_transfer_encoding"})
            continue
        decoded_bytes += len(data)
        if decoded_bytes > _MAX_DECODED:
            unavailable.append({"reason": "decoded_size_limit", "first_unprocessed_index": part_index})
            break
        output_name = f"part-{part_index:04}{_suffix(name)}"
        _write(destination / output_name, data)
        entries.append({
            "part_index": part_index, "original_filename": name, "working_filename": output_name,
            "declared_content_type": content_type, "content_disposition": disposition,
            "transfer_encoding": str(part.get("Content-Transfer-Encoding", "")),
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    if message.defects:
        unavailable.append({"reason": "source_mime_parse_defects"})
    return entries, unavailable


def _stage(source: Path, output: Path) -> str:
    source_name = os.fsencode(source.name)
    key = hashlib.sha256(source_name).hexdigest()
    final = output / key
    if os.path.lexists(final):
        receipt = final / "mime-intake.json"
        if final.is_symlink() or receipt.is_symlink():
            raise ValueError("Attachment staging path must be local.")
        record = json.loads(receipt.read_text(encoding="utf-8"))
        if record["schema_version"] != 1 or record["source_eml_name_hex"] != source_name.hex():
            raise ValueError("Attachment receipt is invalid.")
        if record["source_fingerprint"] != list(file_fingerprint(source.lstat())):
            return "source_changed"
        for derivative in record["working_derivatives"]:
            name = derivative["working_filename"]
            if not re.fullmatch(r"part-[0-9]{4}\.[a-z0-9]{1,10}", name):
                raise ValueError("Attachment receipt contains an invalid staged name.")
            verify_staged_file(final / name, derivative["bytes"], derivative["sha256"])
        return "already_partial" if record["unavailable"] else "already_staged"

    raw, fingerprint = _read_message(source)
    # Build next to the destination so interrupted staging stays outside its scan.
    staging = Path(tempfile.mkdtemp(prefix=".photo-custodian-mime-", dir=output.parent))
    try:
        try:
            entries, unavailable = _extract(raw, staging)
        except (LookupError, RecursionError, UnicodeError):
            entries = []
            unavailable = [{"reason": "mime_parser_limit_or_invalid_encoding"}]
            for path in staging.iterdir():
                path.unlink()
        _write_json(staging / "mime-intake.json", {
            "schema_version": 1,
            "source_eml_name_hex": source_name.hex(),
            "source_eml_sha256": hashlib.sha256(raw).hexdigest(),
            "source_fingerprint": list(fingerprint),
            "working_derivatives": entries,
            "unavailable": unavailable,
        })
        if file_fingerprint(source.lstat()) != fingerprint:
            return "source_changed"
        if os.path.lexists(final):
            raise ValueError("Attachment directory already exists.")
        staging.rename(final)
    finally:
        if staging.exists():
            rmtree(staging)
    return "partial" if unavailable else "staged"


def stage_attachments(args) -> int:
    if not 1 <= args.limit <= 1000:
        raise ValueError("Use an attachment batch limit of 1–1000 messages.")
    source = args.source.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    if not source.is_dir() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use separate source and staging directories.")
    output.mkdir(mode=0o700, exist_ok=True)
    mode = output.stat()
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("Use a private staging directory owned by the current user.")
    lock = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    summary = Counter()
    remaining = False
    processed = 0
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another attachment staging process is using this directory.") from None
        source_receipt = output / ".mime-source.json"
        identity = {"schema_version": 1, "source_path_hex": os.fsencode(source).hex()}
        if os.path.lexists(source_receipt):
            if source_receipt.is_symlink() or json.loads(source_receipt.read_text()) != identity:
                raise ValueError("The staging directory belongs to a different EML source.")
        else:
            _write_json(source_receipt, identity)
        for entry in sorted(source.iterdir()):
            if entry.suffix.lower() != ".eml" or not stat.S_ISREG(entry.lstat().st_mode):
                continue
            existing = output / hashlib.sha256(os.fsencode(entry.name)).hexdigest()
            if processed >= args.limit and not os.path.lexists(existing):
                remaining = True
                break
            try:
                status = _stage(entry, output)
            except (OSError, ValueError, TypeError, KeyError):
                status = "unavailable"
            summary[status] += 1
            if status in ("staged", "partial"):
                processed += 1
    finally:
        os.close(lock)
    print(json.dumps({
        "counts": dict(sorted(summary.items())), "more_messages": remaining,
    }))
    return int(any(summary[key] for key in ("partial", "already_partial", "unavailable", "source_changed")))
