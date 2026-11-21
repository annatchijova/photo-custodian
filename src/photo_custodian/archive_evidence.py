"""Bounded, read-only evidence extraction from catalogued files."""

from __future__ import annotations

import base64
import io
import os
import selectors
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from tempfile import TemporaryFile

from .exact_duplicates import _catalog_fingerprint, _full_path_bytes, _stat_fingerprint
from .visual_review import read_photo

MAX_TEXT_BYTES = 32_768
MAX_VIDEO_FRAME = 512_000
MAX_PDF_BYTES = 128 * 1024 * 1024
PDF_PAGES = 6
VIDEO_FORMATS = {".mov": "mov", ".mp4": "mov", ".m4v": "mov", ".mkv": "matroska", ".webm": "matroska"}


class EvidenceUnavailable(RuntimeError):
    """A tool or input could not provide evidence."""


@dataclass(frozen=True)
class Evidence:
    scope: str
    text: str = ""
    image: str = ""


def _capture(tool: str, arguments: list[str], *, limit: int, fd: int) -> bytes:
    executable = shutil.which(tool)
    if executable is None:
        raise EvidenceUnavailable(f"Install {tool} to analyze this file type.")
    data = bytearray()
    deadline = time.monotonic() + 25
    with subprocess.Popen(
        [executable, *arguments], pass_fds=(fd,), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as process, selectors.DefaultSelector() as watcher:
        watcher.register(process.stdout, selectors.EVENT_READ)
        try:
            while len(data) < limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not watcher.select(remaining):
                    raise EvidenceUnavailable(f"{tool} timed out.")
                chunk = os.read(process.stdout.fileno(), min(8192, limit - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) == limit:
                return bytes(data)
            if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                raise EvidenceUnavailable(f"{tool} could not extract content.")
            return bytes(data)
        except subprocess.TimeoutExpired:
            raise EvidenceUnavailable(f"{tool} timed out.") from None
        finally:
            if process.poll() is None:
                process.kill()


def _pdf_or_video(row) -> Evidence:
    path = _full_path_bytes(row)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    with os.fdopen(os.open(path, flags), "rb") as source:
        fd = source.fileno()
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _stat_fingerprint(before) != _catalog_fingerprint(row):
            raise EvidenceUnavailable("File changed since inventory.")
        locator = f"/proc/self/fd/{fd}"
        extension = row["extension"]
        if extension == ".pdf":
            if before.st_size > MAX_PDF_BYTES:
                raise EvidenceUnavailable("PDF exceeds the extraction size limit.")
            data = _capture("pdftotext", ["-f", "1", "-l", str(PDF_PAGES),
                            "-layout", locator, "-"], limit=MAX_TEXT_BYTES, fd=fd)
            result = Evidence(f"Text layer, first {PDF_PAGES} PDF pages, bounded excerpt",
                              data.decode("utf-8", "replace").strip())
            if not result.text:
                raise EvidenceUnavailable("No PDF text layer in the sampled pages.")
        else:
            demuxer = VIDEO_FORMATS.get(extension)
            if demuxer is None:
                raise EvidenceUnavailable("Video container is unsupported for frame extraction.")
            jpeg = _capture("ffmpeg", [
                "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                "-threads", "1", "-max_pixels", "60000000", "-f", demuxer, "-i", locator,
                "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", "1",
                "-vf", "scale=640:640:force_original_aspect_ratio=decrease",
                "-map_metadata", "-1", "-threads", "1", "-c:v", "mjpeg",
                "-f", "image2pipe", "pipe:1",
            ], limit=MAX_VIDEO_FRAME, fd=fd)
            if not jpeg.startswith(b"\xff\xd8") or not jpeg.endswith(b"\xff\xd9"):
                raise EvidenceUnavailable("Video frame is unavailable.")
            result = Evidence("First decoded video frame, without audio",
                              image="data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"))
        if _stat_fingerprint(os.fstat(fd)) != _stat_fingerprint(before):
            raise EvidenceUnavailable("File changed during extraction.")
    return result


def extract(row, *, ocr_language: str = "eng+spa", use_ocr: bool = True) -> Evidence:
    if row["media_kind"] != "image":
        return _pdf_or_video(row)

    try:
        from PIL import Image
    except ImportError:
        raise EvidenceUnavailable("Install the review extra for photo decoding.") from None

    try:
        preview = read_photo(row, preview=True, preview_edge=1600)
    except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
        raise EvidenceUnavailable("Photo is unavailable, changed, or unsupported.") from None
    text = ""
    scope = "Resized photo preview"
    if use_ocr:
        jpeg = base64.b64decode(preview.split(",", 1)[1])
        with TemporaryFile() as image:
            image.write(jpeg)
            image.seek(0)
            fd = image.fileno()
            try:
                data = _capture("tesseract", [f"/proc/self/fd/{fd}", "stdout",
                                "-l", ocr_language, "--psm", "11"],
                                limit=MAX_TEXT_BYTES, fd=fd)
                text = data.decode("utf-8", "replace").strip()
                scope += " and local OCR excerpt"
            except EvidenceUnavailable as error:
                scope += f"; OCR unavailable ({error})"
    return Evidence(scope, text, preview)
