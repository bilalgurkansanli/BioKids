"""Speech to text with whisper-server, for "press and speak" (D-165).

The audio arrives as a WAV in memory, is posted to whisper-server inside the
assistant's own network namespace, and is dropped as soon as the text comes
back. It is never written to disk and never leaves the machine: that is the
condition the owner set for opening the microphone at all (D-166).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any, Final

from biokids_ai.localhttp import Endpoint, HttpError, post_multipart

INFERENCE_PATH: Final = "/inference"
TIMEOUT_S: Final = 60.0
MAX_AUDIO_BYTES: Final = 16_000 * 2 * 16 + 1024
"""Sixteen seconds of 16 kHz, 16-bit mono, plus a header: the button allows 15."""

#: What Whisper writes for silence, music and noise, in either language.
_NOISE: Final = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")

Multipart = Callable[..., Awaitable[dict[str, Any]]]


class TranscriptionError(Exception):
    pass


class Transcriber:
    def __init__(self, endpoint: Endpoint, *, post: Multipart = post_multipart) -> None:
        self.endpoint = endpoint
        self._post = post

    async def transcribe(self, wav: bytes, locale: str) -> str:
        if not wav.startswith(b"RIFF") or len(wav) > MAX_AUDIO_BYTES:
            raise TranscriptionError("not a short WAV recording")
        language = "tr" if locale.startswith("tr") else "en"
        try:
            data = await self._post(
                self.endpoint,
                INFERENCE_PATH,
                {"temperature": "0.0", "response_format": "json", "language": language},
                "file",
                "speech.wav",
                wav,
                wait_s=TIMEOUT_S,
            )
        except HttpError as exc:
            raise TranscriptionError(str(exc)) from exc
        return clean(str(data.get("text", "")))


def clean(text: str) -> str:
    """Whisper's text with its noise markers and stray whitespace removed."""
    return " ".join(_NOISE.sub(" ", text).split())
