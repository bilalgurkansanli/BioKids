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

AUTO: Final = "auto"
"""Whisper's own guess of the language (D-244)."""
HEARD_LANGUAGES: Final = frozenset({"tr", "turkish", "en", "english"})
"""What whisper-server names Turkish and English, by code or by name."""

Multipart = Callable[..., Awaitable[dict[str, Any]]]


class TranscriptionError(Exception):
    pass


class Transcriber:
    def __init__(self, endpoint: Endpoint, *, post: Multipart = post_multipart) -> None:
        self.endpoint = endpoint
        self._post = post

    async def transcribe(self, wav: bytes, locale: str, fallback: str = "tr") -> str:
        """The words of `wav`, heard in `locale`'s language.

        "auto" lets Whisper find out which (D-244). It knows a hundred
        languages, and a child's short sentence heard as Azerbaijani comes out
        as nonsense: anything but Turkish or English is heard again in
        `fallback`'s.
        """
        if not wav.startswith(b"RIFF") or len(wav) > MAX_AUDIO_BYTES:
            raise TranscriptionError("not a short WAV recording")
        if locale == AUTO:
            data = await self._inference(wav, AUTO, "verbose_json")
            if heard_language(data) in HEARD_LANGUAGES:
                return clean(str(data.get("text", "")))
            locale = fallback
        language = "tr" if locale.startswith("tr") else "en"
        return clean(str((await self._inference(wav, language, "json")).get("text", "")))

    async def _inference(
        self, wav: bytes, language: str, response_format: str
    ) -> dict[str, Any]:
        try:
            return await self._post(
                self.endpoint,
                INFERENCE_PATH,
                {
                    "temperature": "0.0",
                    "response_format": response_format,
                    "language": language,
                },
                "file",
                "speech.wav",
                wav,
                wait_s=TIMEOUT_S,
            )
        except HttpError as exc:
            raise TranscriptionError(str(exc)) from exc


def heard_language(data: dict[str, Any]) -> str:
    """The language whisper-server says it heard, in its own words, lower case."""
    return str(data.get("language") or data.get("detected_language") or "").strip().lower()


def clean(text: str) -> str:
    """Whisper's text with its noise markers and stray whitespace removed."""
    return " ".join(_NOISE.sub(" ", text).split())
