"""Run an installed Whisper CLI with local weights on a private audio sample."""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

AUDIO_FORMATS = {
    ".mp3": "mp3", ".m4a": "mov", ".aac": "aac", ".wav": "wav",
    ".ogg": "ogg", ".opus": "ogg", ".flac": "flac", ".amr": "amr",
}
SAMPLE_SECONDS = 600


@dataclass(frozen=True)
class TranscriptWindow:
    text: str
    decoded_frames: int
    sample_rate: int


def transcribe(fd: int, demuxer: str, weights: Path, *, max_bytes: int) -> str:
    result = transcribe_window(fd, demuxer, weights, start=0,
                               duration=SAMPLE_SECONDS, max_bytes=max_bytes)
    if not result.text:
        raise ValueError("Whisper returned no timed speech segments.")
    return result.text


def transcribe_window(
    fd: int, demuxer: str, weights: Path, *, start: int, duration: int, max_bytes: int
) -> TranscriptWindow:
    model = weights.expanduser().resolve(strict=True)
    if not model.is_file() or model.suffix != ".pt":
        raise ValueError("Select an installed local Whisper .pt model.")
    ffmpeg, whisper = shutil.which("ffmpeg"), shutil.which("whisper")
    if not ffmpeg or not whisper:
        raise ValueError("Install FFmpeg and the local Whisper CLI for transcription.")
    env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8"}
    with TemporaryDirectory(prefix="photo-custodian-whisper-") as directory:
        sample = Path(directory) / "sample.wav"
        try:
            subprocess.run([
                ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                "-threads", "1", "-f", demuxer, "-ss", str(start),
                "-i", f"/proc/self/fd/{fd}",
                "-map", "0:a:0", "-t", str(duration), "-vn", "-sn", "-dn",
                "-map_metadata", "-1", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                str(sample),
            ], pass_fds=(fd,), check=True, timeout=90, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env)
            with wave.open(str(sample), "rb") as audio:
                frames = audio.getnframes()
                rate = audio.getframerate()
            if not frames:
                return TranscriptWindow("", frames, rate)
            subprocess.run([
                whisper, str(sample), "--model", str(model), "--output_dir", directory,
                "--output_format", "vtt", "--verbose", "False",
            ], check=True, timeout=1800, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**env, "HOME": directory, "XDG_CACHE_HOME": directory})
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError, wave.Error, EOFError):
            raise ValueError("Local transcription failed or timed out.") from None
        with (Path(directory) / "sample.vtt").open("rb") as output:
            data = output.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("Whisper output exceeds the transcript window limit.")
        text = data.decode("utf-8", "replace").strip()
        return TranscriptWindow(text if "-->" in text else "", frames, rate)
