"""D-174 — a parent's own voice for the spoken lines."""

from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest

from biokids_core import recorded_voice


def a_wav(seconds: float = 1.0, rate: int = 24_000, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(seconds * rate) * channels)
    return buffer.getvalue()


# ------------------------------------------------------------------ the files


def test_a_recording_is_kept_and_found(tmp_path: Path) -> None:
    recorded_voice.save(tmp_path, "tr", "warn_10", a_wav(2.0), "On dakikan kaldı canım.")
    found = recorded_voice.lookup(tmp_path, "tr", "warn_10")
    assert found is not None
    assert found.text == "On dakikan kaldı canım."
    assert found.seconds == pytest.approx(2.0)
    assert recorded_voice.recorded_events(tmp_path, "tr") == {"warn_10"}


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (a_wav(0.2), "voices.error.too_short"),
        (a_wav(20.0, rate=8_000), "voices.error.too_long"),
        (a_wav(1.0, channels=2), "voices.error.not_audio"),
        (b"RIFF....WAVEnot really", "voices.error.not_audio"),
    ],
    ids=["too-short", "too-long", "stereo", "not-a-wav"],
)
def test_what_is_not_a_short_mono_recording_is_refused(
    tmp_path: Path, data: bytes, error: str
) -> None:
    with pytest.raises(recorded_voice.RecordingError, match=error):
        recorded_voice.save(tmp_path, "tr", "warn_5", data, "")
    assert recorded_voice.lookup(tmp_path, "tr", "warn_5") is None


@pytest.mark.parametrize(("locale", "event"), [("tr", "../../etc/passwd"), ("../x", "warn_1")])
def test_names_never_become_paths(tmp_path: Path, locale: str, event: str) -> None:
    with pytest.raises(recorded_voice.RecordingError):
        recorded_voice.save(tmp_path, locale, event, a_wav(), "")


def test_deleting_brings_the_computer_voice_back(tmp_path: Path) -> None:
    recorded_voice.save(tmp_path, "tr", "time_up", a_wav(), "Süren bitti.")
    assert recorded_voice.delete(tmp_path, "tr", "time_up")
    assert recorded_voice.lookup(tmp_path, "tr", "time_up") is None


def test_the_face_is_the_parents_choice(tmp_path: Path) -> None:
    assert recorded_voice.persona(tmp_path) == "zio"
    recorded_voice.set_persona(tmp_path, "sio")
    assert recorded_voice.persona(tmp_path) == "sio"
