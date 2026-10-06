"""A parent's own voice, recorded for the lines Zio and Sio say (D-174).

The roadmap's way to a parent's voice is cloning (Phase 2a): ninety seconds of
audio, OpenVoice, a GPU and a month. Most of what a child would hear in it is
seven sentences - welcome, ten, five and one minute left, time is up, yes and
not now to what they asked for. So a parent can simply say those, into their
phone, and the child hears their mother or father instead of espeak-ng.

The recordings are WAV files in a directory of their own, outside the 0700
`/var/lib/biokids`, because the voice service runs as the child and must read
them: `<recorded_voice_dir>/<locale>/<event>.wav`, with the sentence that was
said in `<event>.txt` for the card on screen. Nothing else of the family's is
in there, and nothing in there leaves the machine.

Recorded audio is checked before it is kept: PCM WAV, mono, 16-bit, a sane
sample rate, between half a second and fifteen seconds.
"""

from __future__ import annotations

import io
import json
import re
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Final

EVENTS: Final = (
    "welcome",
    "warn_10",
    "warn_5",
    "warn_1",
    "time_up",
    "request_approved",
    "request_denied",
)
"""What can be recorded, in the order a child meets them."""

MIN_SECONDS: Final = 0.5
MAX_SECONDS: Final = 15.0
MAX_BYTES: Final = 2 * 1024 * 1024
RATES: Final = range(8_000, 48_001)
PERSONAS: Final = ("zio", "sio")
_META: Final = "voice.json"
_LOCALE_RE: Final = re.compile(r"^[a-z]{2}$")


class RecordingError(ValueError):
    """A recording that will not be kept. The message is a catalog key."""


@dataclass(frozen=True, slots=True)
class Recording:
    path: Path
    text: str
    seconds: float


def _safe(locale: str, event: str) -> tuple[str, str]:
    """Both are interpolated into a path: only known values get that far."""
    if event not in EVENTS or not _LOCALE_RE.match(locale):
        raise RecordingError("error.invalid_input")
    return locale, event


def wav_seconds(data: bytes) -> float:
    """The length of a recording, or RecordingError when it is not one we keep."""
    if len(data) > MAX_BYTES:
        raise RecordingError("voices.error.too_long")
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            channels = wav.getnchannels()
            width = wav.getsampwidth()
            rate = wav.getframerate()
            frames = wav.getnframes()
    except (wave.Error, EOFError, RuntimeError, ValueError) as exc:  # truncated input
        raise RecordingError("voices.error.not_audio") from exc
    if channels != 1 or width != 2 or rate not in RATES:
        raise RecordingError("voices.error.not_audio")
    seconds = frames / rate
    if seconds < MIN_SECONDS:
        raise RecordingError("voices.error.too_short")
    if seconds > MAX_SECONDS:
        raise RecordingError("voices.error.too_long")
    return seconds


def save(directory: Path, locale: str, event: str, data: bytes, text: str) -> Recording:
    """Keep a recording, replacing any earlier one. World-readable on purpose:
    the voice service is the child's process."""
    locale, event = _safe(locale, event)
    seconds = wav_seconds(data)
    folder = directory / locale
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    target = folder / f"{event}.wav"
    partial = folder / f".{event}.wav.part"
    partial.write_bytes(data)
    partial.chmod(0o644)
    partial.replace(target)
    sentence = folder / f"{event}.txt"
    sentence.write_text(" ".join(text.split())[:300], encoding="utf-8")
    sentence.chmod(0o644)
    return Recording(path=target, text=text, seconds=seconds)


def delete(directory: Path, locale: str, event: str) -> bool:
    locale, event = _safe(locale, event)
    removed = False
    for suffix in (".wav", ".txt"):
        path = directory / locale / f"{event}{suffix}"
        if path.exists():
            path.unlink()
            removed = True
    return removed


def lookup(directory: Path, locale: str, event: str) -> Recording | None:
    """The recording for this line, if a parent made one and it is still good."""
    try:
        locale, event = _safe(locale, event)
    except RecordingError:
        return None
    path = directory / locale / f"{event}.wav"
    try:
        data = path.read_bytes()
        seconds = wav_seconds(data)
    except (OSError, RecordingError):
        return None
    try:
        text = (directory / locale / f"{event}.txt").read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return Recording(path=path, text=text, seconds=seconds)


def recorded_events(directory: Path, locale: str) -> set[str]:
    return {event for event in EVENTS if lookup(directory, locale, event) is not None}


def persona(directory: Path) -> str:
    """Which character the recorded voice belongs to: Zio (a mother's voice) or
    Sio (a father's), so the card shows the right face."""
    try:
        chosen = json.loads((directory / _META).read_text(encoding="utf-8")).get("persona")
    except (OSError, ValueError, AttributeError):
        return "zio"
    return chosen if chosen in PERSONAS else "zio"


def set_persona(directory: Path, chosen: str) -> None:
    if chosen not in PERSONAS:
        raise RecordingError("error.invalid_input")
    directory.mkdir(parents=True, exist_ok=True)
    meta = directory / _META
    meta.write_text(json.dumps({"persona": chosen}), encoding="utf-8")
    meta.chmod(0o644)
