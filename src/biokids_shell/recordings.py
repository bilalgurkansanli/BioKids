"""Sesim: the stories, songs and messages a child records, on their own launcher (D-240).

A child who cannot write yet can still make something: they hold the big
button, tell a story or sing a song, and hear it back. The recordings are the
child's, like their drawings in Resimlerim (D-212): kept in their own home,
`~/.local/share/biokids/sesim/`, by the launcher that runs as them, and sent
nowhere - unless the child presses "Büyüğüme gönder", which hands that one
recording to the daemon for the parent's panel (`shared`).

What this module guarantees, because the launcher writes and serves files:

- Only short mono 16-bit PCM WAV, as the page records it: the header is read,
  not trusted from the name, and a recording is 0.5 to `MAX_SECONDS` seconds.
- Only plain names this module made (`sesim-YYYYMMDD-HHMMSS.wav`): no
  separators, nothing hidden, nothing a request could aim elsewhere.
- Only files the child owns, never a symbolic link, checked on the open file.
- At most `MAX_KEPT`: a full folder asks the child to delete one, rather than
  quietly dropping the oldest story they are proud of.
"""

from __future__ import annotations

import array
import contextlib
import datetime as dt
import os
import re
import stat
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FOLDER: Final = Path(".local") / "share" / "biokids" / "sesim"
NAME: Final = re.compile(r"sesim-\d{8}-\d{6}\.wav")

MAX_SECONDS: Final = 40
"""Long enough for a story a four-year-old tells; short enough to share (`shared`)."""
MIN_SECONDS: Final = 0.5
MAX_KEPT: Final = 40
RATES: Final = (8000, 16000, 22050, 24000, 32000, 44100, 48000)
SHARED_RATE: Final = 8000
"""What the parent gets: telephone quality, small enough for one message to the
daemon (`ipc.MAX_LINE_BYTES`) at the longest recording."""


class RecordingError(ValueError):
    """Not a recording this module keeps; `key` is the sentence for the child."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


@dataclass(frozen=True, slots=True)
class Recording:
    name: str
    seconds: float
    made: dt.datetime


def folder(home: Path) -> Path:
    return home / FOLDER


def _parse(data: bytes) -> tuple[int, int, int]:
    """(sample rate, where the samples start, their length in bytes) of a mono
    16-bit PCM WAV, or RecordingError. The chunks are walked, never searched."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise RecordingError("sesim.error.not_audio")
    offset = 12
    rate = channels = bits = 0
    fmt_seen = False
    while offset + 8 <= len(data):
        chunk, size = struct.unpack_from("<4sI", data, offset)
        body = offset + 8
        if chunk == b"fmt ":
            if size < 16 or body + 16 > len(data):
                raise RecordingError("sesim.error.not_audio")
            fmt, channels, rate, _byte_rate, _align, bits = struct.unpack_from(
                "<HHIIHH", data, body
            )
            if fmt != 1:
                raise RecordingError("sesim.error.not_audio")
            fmt_seen = True
        elif chunk == b"data":
            if not fmt_seen or channels != 1 or bits != 16 or rate not in RATES:
                raise RecordingError("sesim.error.not_audio")
            return rate, body, min(size, len(data) - body)
        offset = body + size + (size & 1)
    raise RecordingError("sesim.error.not_audio")


def wav_seconds(data: bytes) -> tuple[int, float]:
    """(sample rate, seconds) of a mono 16-bit PCM WAV, or RecordingError."""
    rate, _start, length = _parse(data)
    return rate, length / (rate * 2)


HEADER_BYTES: Final = 4096
"""Enough of a WAV for its chunks up to the samples: what a listing reads."""


def recordings(home: Path, uid: int | None) -> list[Recording]:
    """The child's recordings, newest first. Each file's header only, and its
    length from the file system: a page of forty stories reads 160 KB, not 50 MB."""
    found: list[Recording] = []
    place = folder(home)
    try:
        entries = list(place.iterdir())
    except OSError:
        return found
    for path in entries:
        if not NAME.fullmatch(path.name):
            continue
        found_head = _read_own(path, uid, head=HEADER_BYTES)
        if found_head is None:
            continue
        head, size = found_head
        try:
            rate, start, _length = _parse(head)
        except RecordingError:
            continue
        seconds = max(0, size - start) / (rate * 2)
        found.append(Recording(path.name, round(seconds, 1), _made(path.name)))
    found.sort(key=lambda r: r.name, reverse=True)
    return found


def save(home: Path, uid: int | None, data: bytes, now: dt.datetime | None = None) -> Recording:
    """Keep one recording the page made. Raises RecordingError with a key."""
    _rate, seconds = wav_seconds(data)
    if seconds < MIN_SECONDS:
        raise RecordingError("sesim.error.too_short")
    if seconds > MAX_SECONDS + 1:
        raise RecordingError("sesim.error.too_long")
    place = folder(home)
    place.mkdir(parents=True, exist_ok=True, mode=0o700)
    if len(recordings(home, uid)) >= MAX_KEPT:
        raise RecordingError("sesim.error.full")
    now = now or dt.datetime.now()
    name = f"sesim-{now:%Y%m%d-%H%M%S}.wav"
    target = place / name
    if target.exists():
        raise RecordingError("sesim.error.too_fast")
    temp = place / f".{name}.tmp"
    temp.write_bytes(data)
    temp.chmod(0o600)
    temp.replace(target)
    return Recording(name, round(seconds, 1), _made(name))


def read(home: Path, uid: int | None, name: str) -> bytes | None:
    """The recording's bytes, for the page to play; None for anything else."""
    if not NAME.fullmatch(name):
        return None
    found = _read_own(folder(home) / name, uid)
    if found is None:
        return None
    data, _size = found
    try:
        wav_seconds(data)
    except RecordingError:
        return None
    return data


def delete(home: Path, uid: int | None, name: str) -> bool:
    if read(home, uid, name) is None:
        return False
    with contextlib.suppress(OSError):
        (folder(home) / name).unlink()
        return True
    return False


def shared(data: bytes) -> bytes:
    """The recording as the parent gets it: mono 16-bit at SHARED_RATE.

    Down-sampled by averaging, which is enough for a voice and needs nothing the
    launcher does not have (`audioop` left Python in 3.13).
    """
    rate, start, length = _parse(data)
    samples = array.array("h")
    samples.frombytes(data[start : start + length // 2 * 2])
    if sys.byteorder != "little":
        samples.byteswap()
    step = max(1, round(rate / SHARED_RATE))
    out = array.array(
        "h",
        (
            int(sum(samples[i : i + step]) / len(samples[i : i + step]))
            for i in range(0, len(samples) - step + 1, step)
        ),
    )
    if sys.byteorder != "little":
        out.byteswap()
    pcm = out.tobytes()
    new_rate = rate // step
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, new_rate, new_rate * 2, 2, 16)
    return header + b"data" + struct.pack("<I", len(pcm)) + pcm


def _made(name: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(name, "sesim-%Y%m%d-%H%M%S.wav")
    except ValueError:
        return dt.datetime.min


def _read_own(path: Path, uid: int | None, *, head: int = 0) -> tuple[bytes, int] | None:
    """The file (or its first `head` bytes) and its size, if it is a regular file
    the child owns - checked on the open file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            return None
        if uid is not None and info.st_uid != uid:
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return (handle.read(head) if head else handle.read()), info.st_size
    except OSError:
        return None
    finally:
        os.close(fd)
