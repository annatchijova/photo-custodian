"""Stage resumable, private transcript windows from a catalogued media source."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from pathlib import Path
from shutil import rmtree

from .archive_evidence import VIDEO_FORMATS, _capture
from .exact_duplicates import _catalog_fingerprint, _full_path_bytes
from .intake import _write, _write_json
from .manifest_verify import file_fingerprint
from .transcription import AUDIO_FORMATS, transcribe_window

_WINDOW = 300
_MAX_TEXT = 24_000
_FORMATS = AUDIO_FORMATS | VIDEO_FORMATS
_OPEN = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _validate_file(handle, expected=None) -> tuple:
    metadata = os.fstat(handle.fileno())
    fingerprint = file_fingerprint(metadata)
    if not stat.S_ISREG(metadata.st_mode) or (expected is not None and fingerprint != expected):
        raise ValueError("File no longer matches the recorded fingerprint.")
    return fingerprint


def _duration(fd: int, demuxer: str) -> int:
    value = _capture("ffprobe", [
        "-v", "error", "-protocol_whitelist", "file,pipe", "-f", demuxer,
        "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
        f"/proc/self/fd/{fd}",
    ], limit=128, fd=fd)
    try:
        seconds = Decimal(value.decode("ascii").strip())
        if not seconds.is_finite() or not 0 < seconds <= 7 * 24 * 3600:
            raise ValueError
        return int(seconds.to_integral_value(rounding=ROUND_CEILING))
    except (ValueError, InvalidOperation):
        raise ValueError("Media has no supported declared duration.") from None


def _state(directory: Path, source, row: sqlite3.Row, weights_sha: str) -> dict:
    path = directory / "source.json"
    expected = {
        "schema_version": 1, "file_id": row["id"], "source_id": row["source_id"],
        "source_fingerprint": list(_catalog_fingerprint(row)),
        "model_sha256": weights_sha, "window_seconds": _WINDOW, "audio_stream": 0,
        "demuxer": _FORMATS[row["extension"]],
    }
    if path.is_file() and not path.is_symlink():
        state = json.loads(path.read_text())
        if any(state.get(key) != value for key, value in expected.items()):
            raise ValueError("Source or model changed. Use a new transcript directory.")
        if type(state.get("declared_duration_rounded_up")) is not int or not (
            0 < state["declared_duration_rounded_up"] <= 7 * 24 * 3600
        ):
            raise ValueError("Transcript source record is invalid.")
        return state
    digest = hashlib.file_digest(source, "sha256").hexdigest()
    source.seek(0)
    state = {
        **expected, "source_sha256": digest,
        "declared_duration_rounded_up": _duration(source.fileno(), expected["demuxer"]),
    }
    _validate_file(source, tuple(expected["source_fingerprint"]))
    _write_json(path, state)
    return state


def _window(directory, source, state, weights, weights_fingerprint, start, duration) -> str:
    final = directory / f"window-{start:07}"
    receipt = final / "window.json"
    if final.is_symlink() or receipt.is_symlink():
        raise ValueError("Transcript window path is invalid.")
    if receipt.is_file():
        saved = json.loads(receipt.read_text())
        if (
            saved.get("source_sha256") != state["source_sha256"]
            or saved.get("model_sha256") != state["model_sha256"]
            or saved.get("requested_start_seconds") != start
            or saved.get("requested_duration_seconds") != duration
        ):
            raise ValueError("Transcript window receipt does not match its source.")
        expected = saved.get("text_sha256")
        if expected is not None:
            with os.fdopen(os.open(final / "transcript.txt", _OPEN), "rb") as text:
                actual = hashlib.sha256(text.read(_MAX_TEXT + 1)).hexdigest()
            if actual != expected:
                raise ValueError("Saved transcript no longer matches its receipt.")
        return "reused"
    if final.exists():
        raise ValueError("Transcript window has no receipt.")

    if file_fingerprint(weights.stat()) != weights_fingerprint:
        raise ValueError("Whisper weights changed.")
    result = transcribe_window(source.fileno(), state["demuxer"], weights,
                               start=start, duration=duration, max_bytes=_MAX_TEXT)
    _validate_file(source, tuple(state["source_fingerprint"]))
    if file_fingerprint(weights.stat()) != weights_fingerprint:
        raise ValueError("Whisper weights changed.")
    coverage = (
        "no_pcm" if not result.decoded_frames else
        "short_pcm" if result.decoded_frames < duration * result.sample_rate else "requested_pcm"
    )
    text = result.text.encode("utf-8") if result.text else b""
    if len(text) > _MAX_TEXT:
        raise ValueError("Decoded transcript exceeds the text byte limit.")
    work = Path(tempfile.mkdtemp(prefix=".transcript-window-", dir=directory.parent.parent))
    try:
        if text:
            _write(work / "transcript.txt", text)
        _write_json(work / "window.json", {
            "schema_version": 1, "source_sha256": state["source_sha256"],
            "model_sha256": state["model_sha256"],
            "requested_start_seconds": start, "requested_duration_seconds": duration,
            "decoded_frames": result.decoded_frames, "sample_rate": result.sample_rate,
            "decode_coverage": coverage, "contains_timed_transcript": bool(text),
            "text_sha256": hashlib.sha256(text).hexdigest() if text else None,
            "text_timestamp_basis": "relative to decoded PCM window",
        })
        work.rename(final)
    finally:
        if work.exists():
            rmtree(work)
    return "processed"


def stage_transcripts(args) -> int:
    if not 1 <= args.limit <= 500 or not 1 <= args.source_id <= 2**63 - 1:
        raise ValueError("Use a positive source ID and a window budget of 1–500.")
    weights = args.whisper_model.expanduser().resolve(strict=True)
    if weights.suffix != ".pt":
        raise ValueError("Select installed Whisper .pt weights.")
    with os.fdopen(os.open(weights, _OPEN), "rb") as handle:
        weights_fingerprint = _validate_file(handle)
        weights_sha = hashlib.file_digest(handle, "sha256").hexdigest()
        _validate_file(handle, weights_fingerprint)
    catalog = args.catalog.expanduser().resolve(strict=True)
    output = args.output.expanduser().resolve()
    connection = sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        root = connection.execute("SELECT root_path_bytes FROM sources WHERE id = ?",
                                  (args.source_id,)).fetchone()
        if root is None:
            raise ValueError("The selected source ID is absent.")
        source_path = Path(os.fsdecode(root[0])).resolve()
        if output.is_relative_to(source_path) or source_path.is_relative_to(output):
            raise ValueError("Use separate source and transcript directories.")
        markers = ",".join("?" for _ in _FORMATS)
        rows = connection.execute(f"""
            SELECT f.*, s.root_path_bytes FROM files f JOIN sources s ON s.id = f.source_id
            WHERE f.source_id = ? AND f.status = 'active' AND f.extension IN ({markers})
            ORDER BY f.id
        """, (args.source_id, *_FORMATS)).fetchall()
    finally:
        connection.close()

    output.mkdir(mode=0o700, exist_ok=True)
    permissions = output.stat()
    if not stat.S_ISDIR(permissions.st_mode) or permissions.st_uid != os.getuid() or permissions.st_mode & 0o077:
        raise ValueError("Use a private transcript directory owned by the current user.")
    lock = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    counts = Counter()
    blocked: list[int] = []
    pending = False
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        identity = {"schema_version": 1, "catalog_path_hex": os.fsencode(catalog).hex(),
                    "source_id": args.source_id, "model_sha256": weights_sha}
        binding = output / ".transcript-source.json"
        if os.path.lexists(binding):
            if binding.is_symlink() or json.loads(binding.read_text()) != identity:
                raise ValueError("Transcript output belongs to a different source or model.")
        else:
            _write_json(binding, identity)
        for row in rows:
            directory = output / f"media-{row['id']}"
            if directory.is_symlink():
                raise ValueError("Transcript output contains a symbolic link.")
            try:
                with os.fdopen(os.open(_full_path_bytes(row), _OPEN), "rb") as source:
                    _validate_file(source, _catalog_fingerprint(row))
                    directory.mkdir(mode=0o700, exist_ok=True)
                    state = _state(directory, source, row, weights_sha)
                    duration = state["declared_duration_rounded_up"]
                    for start in range(0, duration, _WINDOW):
                        committed = (directory / f"window-{start:07}").is_dir()
                        if not committed:
                            if counts["attempted"] >= args.limit:
                                pending = True
                                break
                            counts["attempted"] += 1
                        count = _window(directory, source, state, weights, weights_fingerprint,
                                        start, min(_WINDOW, duration - start))
                        counts[count] += 1
            except (OSError, ValueError, RuntimeError, TypeError, KeyError):
                blocked.append(row["id"])
            if pending:
                break
    finally:
        os.close(lock)
    print(json.dumps({
        "attempted_windows": counts["attempted"],
        "new_windows": counts["processed"], "reused_windows": counts["reused"],
        "blocked_file_ids": blocked, "more_windows": pending,
        "coverage": "requested windows over declared container duration",
    }))
    return int(bool(blocked))
