"""Turning a sentence into a WAV file.

Piper is invoked as a **subprocess**, never imported. That is partly taste — a
CPU inference engine has no business inside a service whose real job is a queue
— and mostly licensing: D-30 allows GPL software invoked as a separate process
and forbids linking it, and calling `piper` over a pipe keeps us on the right
side of that line permanently, whatever Piper's packaging does next.

Nothing here touches the network. Piper is offline by design (VOICE.md §2) and
`SECURITY.md` §4 lists no TTS destination except the opt-in ElevenLabs path,
which Phase 0 does not implement.

Two backends:

* `PiperSynth` is the real one, and it fails loudly when its model is missing.
* `StubSynth` writes silence of roughly the right length. It exists so that the
  service starts, the queue runs, and the whole test suite passes on a machine
  with no TTS installed and no sound card. It still honours a missing model
  file, because otherwise the cloned → stock fallback would be untestable.

The cache is the reason this is cheap. A family hears the same handful of
sentences every day; after the first afternoon almost nothing is synthesised
live, which is what lets a warning arrive in the same tick it was decided
rather than a second and a half later.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Final, Protocol

from biokids_core.config import Config, get_config
from biokids_core.errors import BioKidsError

LOG = logging.getLogger(__name__)

SAMPLE_RATE: Final = 22_050
"""What Piper's Turkish voices emit, and therefore what silence should match."""

CHARS_PER_SECOND: Final = 14.0
MIN_DURATION_S: Final = 0.8
MAX_DURATION_S: Final = 15.0
PIPER_TIMEOUT_S: Final = 20.0

STOCK_MODEL_SUBDIR: Final = "piper"
MODEL_SUFFIX: Final = ".onnx"

STUB_CACHE_SUBDIR: Final = "silent"
"""Where `StubSynth` keeps its silence, apart from real audio. See `from_config`."""


class SynthError(BioKidsError):
    """A backend could not produce audio for one line, for one voice."""


def estimate_duration(text: str) -> float:
    """How long this line will take to say, near enough.

    Used for two things: the length of the silence `StubSynth` writes, and how
    long a card stays up when there is no audio at all. Both want "about
    right", neither wants a phoneme model, and both look wrong if a two-word
    line and a two-sentence line are treated the same.
    """
    seconds = len(text.strip()) / CHARS_PER_SECOND
    return min(MAX_DURATION_S, max(MIN_DURATION_S, seconds))


# ------------------------------------------------------------------- voices


@dataclass(frozen=True, slots=True)
class VoiceSpec:
    """One voice a line could be spoken in."""

    persona: str
    """`zio` | `sio`."""
    locale: str
    kind: str = "stock"
    """`stock` | `cloned`. The order of the fallback chain, in one word."""
    profile_id: str = "stock"
    """Part of the cache key, so re-recording a voice invalidates its audio."""
    model_path: Path | None = None
    """None means "whatever the backend does by default", which only the stub has."""


def cache_key(text: str, voice: VoiceSpec) -> str:
    """`sha256(text + persona + voice_profile_id)`, exactly as VOICE.md §6 says.

    The separator matters: without it, ("ab", "c") and ("a", "bc") would share
    a key, and a family would occasionally hear the wrong line in the wrong
    voice with no way to explain why.
    """
    material = "\x1f".join((text, voice.persona, voice.profile_id))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class VoiceBank:
    """Which voices to try for a persona, best first.

    A family's own recording always comes first; stock Piper is the permanent
    floor underneath it. The cloned entry is offered whenever the persona has a
    profile directory at all, *even when its model file has gone missing* —
    that is what makes a deleted model produce a logged warning and a stock
    voice instead of silently pretending the family never recorded anything.
    """

    def __init__(self, config: Config | None = None) -> None:
        self._config = config or get_config()

    def candidates(self, persona: str, locale: str) -> tuple[VoiceSpec, ...]:
        found: list[VoiceSpec] = []
        cloned_dir = self._config.paths.voice_profile_dir / persona
        if cloned_dir.is_dir():
            cloned_model = cloned_dir / f"{locale}{MODEL_SUFFIX}"
            found.append(
                VoiceSpec(
                    persona=persona,
                    locale=locale,
                    kind="cloned",
                    profile_id=f"cloned:{persona}:{locale}:{_fingerprint(cloned_model)}",
                    model_path=cloned_model,
                )
            )
        stock_model = self.stock_model(persona, locale)
        found.append(
            VoiceSpec(
                persona=persona,
                locale=locale,
                kind="stock",
                profile_id=f"stock:{persona}:{locale}:{_fingerprint(stock_model)}",
                model_path=stock_model,
            )
        )
        return tuple(found)

    def stock_model(self, persona: str, locale: str) -> Path | None:
        """`<model_dir>/piper/<locale>-<persona>.onnx`, or None when unbundled.

        Phase 0 ships no voice model (see this package's README), so this is
        normally None and the chain ends at the card. One naming convention
        rather than a search: a voice that is not where the packaging put it is
        a packaging bug, and guessing would hide it.
        """
        candidate = (
            self._config.paths.model_dir
            / STOCK_MODEL_SUBDIR
            / f"{locale}-{persona}{MODEL_SUFFIX}"
        )
        return candidate if candidate.is_file() else None


def _fingerprint(path: Path | None) -> str:
    """Enough of a model file's identity to notice that it changed.

    `profile_id` is part of the cache key (VOICE.md §6), and a family who
    re-records Zio would otherwise keep hearing the voice they replaced: every
    sentence they hear every day is already in the cache under the old audio,
    and nothing about `cloned:zio:tr` moves when the file underneath it does.
    Size and modification time move; hashing sixty megabytes of ONNX before
    every "on dakikan kaldı" would not be a trade worth making.
    """
    try:
        stat = path.stat() if path is not None else None
    except OSError:  # pragma: no cover - raced with a deletion
        stat = None
    return f"{stat.st_size}:{stat.st_mtime_ns}" if stat is not None else "absent"


# ----------------------------------------------------------------- backends


class Synth(Protocol):
    """What the service needs from a text-to-speech engine."""

    name: str

    def available(self) -> bool: ...

    def render(self, text: str, voice: VoiceSpec, destination: Path) -> None: ...


class PiperSynth:
    """Piper, over a pipe: text on stdin, a WAV at `--output_file`."""

    name = "piper"

    def __init__(self, binary: str = "piper", *, timeout: float = PIPER_TIMEOUT_S) -> None:
        self._binary = binary
        self._timeout = timeout

    def available(self) -> bool:
        return which(self._binary) is not None

    def render(self, text: str, voice: VoiceSpec, destination: Path) -> None:
        if not self.available():
            raise SynthError(f"{self._binary} is not installed")
        if voice.model_path is None:
            raise SynthError(f"no voice model for {voice.persona}/{voice.locale}")
        if not voice.model_path.is_file():
            raise SynthError(f"voice model {voice.model_path} is missing")

        destination.parent.mkdir(parents=True, exist_ok=True)
        argv = [
            self._binary,
            "--model",
            str(voice.model_path),
            "--output_file",
            str(destination),
        ]
        try:
            finished = subprocess.run(
                argv,
                input=text.encode("utf-8"),
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SynthError(f"{self._binary} could not be run: {exc}") from exc

        if finished.returncode != 0:
            detail = finished.stderr.decode("utf-8", "replace").strip()[:200]
            raise SynthError(f"{self._binary} exited {finished.returncode}: {detail}")
        if not destination.is_file() or destination.stat().st_size == 0:
            raise SynthError(f"{self._binary} wrote nothing to {destination}")


class StubSynth:
    """Silence of about the right length, for a machine with no TTS.

    Deliberately not a no-op: a real file of a plausible duration means the
    queue, the cache, the player and the card all behave exactly as they will
    on a machine that can actually speak.
    """

    name = "stub"

    def __init__(self, *, sample_rate: int = SAMPLE_RATE) -> None:
        self._sample_rate = sample_rate
        self.calls = 0

    def available(self) -> bool:
        return True

    def render(self, text: str, voice: VoiceSpec, destination: Path) -> None:
        # A model path that was set and then removed still fails here, so the
        # cloned → stock fallback can be tested without installing Piper.
        if voice.model_path is not None and not voice.model_path.is_file():
            raise SynthError(f"voice model {voice.model_path} is missing")
        self.calls += 1
        write_silence(destination, estimate_duration(text), self._sample_rate)


def write_silence(destination: Path, seconds: float, sample_rate: int = SAMPLE_RATE) -> None:
    """A valid mono 16-bit WAV of `seconds` of nothing."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, int(seconds * sample_rate))
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames)


# -------------------------------------------------------------------- cache


class VoiceCache:
    """WAV files on disk, capped and evicted least-recently-used first.

    Recency is the file's mtime, bumped on every hit. That is coarser than a
    real LRU list and it is also free, survives a restart, and can be inspected
    with `ls -lt` by a parent wondering what is in there.
    """

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self.hits = 0
        self.misses = 0

    @classmethod
    def from_config(cls, config: Config | None = None, *, subdir: str = "") -> VoiceCache:
        config = config or get_config()
        directory = config.paths.voice_cache_dir
        return cls(
            directory / subdir if subdir else directory,
            max_bytes=config.voice.cache_max_mb * 1024 * 1024,
        )

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key}.wav"

    def get(self, key: str) -> Path | None:
        path = self.path_for(key)
        try:
            if path.stat().st_size == 0:
                path.unlink(missing_ok=True)
                self.misses += 1
                return None
            path.touch()
        except OSError:
            self.misses += 1
            return None
        self.hits += 1
        return path

    def size_bytes(self) -> int:
        return sum(entry.st_size for entry in self._entries())

    def evict(self) -> int:
        """Delete the oldest files until the cache fits. Returns how many went."""
        entries = sorted(self._entries(), key=lambda e: e.st_mtime)
        total = sum(entry.st_size for entry in entries)
        removed = 0
        for entry in entries:
            if total <= self.max_bytes:
                break
            try:
                entry.path.unlink()
            except OSError:  # pragma: no cover - another process got there first
                continue
            total -= entry.st_size
            removed += 1
        if removed:
            LOG.info("voice cache: evicted %d file(s) to stay under the cap", removed)
        return removed

    def clear(self) -> None:
        """Used when a parent deletes a recorded voice (VOICE.md §4)."""
        for entry in self._entries():
            entry.path.unlink(missing_ok=True)

    def _entries(self) -> list[_CacheEntry]:
        try:
            found = list(self.directory.glob("*.wav"))
        except OSError:  # pragma: no cover - the directory may not exist yet
            return []
        entries: list[_CacheEntry] = []
        for path in found:
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover - raced with an eviction
                continue
            entries.append(_CacheEntry(path, stat.st_size, stat.st_mtime))
        return entries


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    path: Path
    st_size: int
    st_mtime: float


# -------------------------------------------------------------- the chain


@dataclass(frozen=True, slots=True)
class SynthResult:
    """What came back. `path is None` means the card speaks alone."""

    path: Path | None = None
    voice: VoiceSpec | None = None
    cached: bool = False
    degraded: bool = False
    """True when a preferred voice failed and something further down answered."""

    @property
    def spoken(self) -> bool:
        return self.path is not None


class Synthesizer:
    """The cloned → stock → nothing chain of VOICE.md §6, with the cache in front."""

    def __init__(self, backend: Synth, cache: VoiceCache) -> None:
        self.backend = backend
        self.cache = cache

    @classmethod
    def from_config(cls, config: Config | None = None) -> Synthesizer:
        """Piper when it is installed, silence when it is not.

        Falling back to silence rather than refusing to start is what keeps the
        service, the card and the queue working on a machine without Piper —
        but it is loud about it, because a family who expected a voice and got
        none deserves a reason in the log.
        """
        config = config or get_config()
        piper = PiperSynth(config.voice.piper_binary)
        if piper.available():
            return cls(piper, VoiceCache.from_config(config))

        LOG.warning(
            "%s is not installed: cards will appear but nothing will be spoken",
            config.voice.piper_binary,
        )
        # Silence gets a cache of its own. The key is the sentence, the persona
        # and the profile (VOICE.md §6) and says nothing about which engine
        # produced the audio, so a shared directory would leave a family who
        # installed Piper afterwards listening to yesterday's empty WAVs for
        # every line they hear every day, with `status` cheerfully reporting
        # `piper`.
        return cls(StubSynth(), VoiceCache.from_config(config, subdir=STUB_CACHE_SUBDIR))

    def render(self, text: str, voices: Sequence[VoiceSpec]) -> SynthResult:
        """Try each voice in turn. Never raises; the worst case is no audio."""
        degraded = False
        for voice in voices:
            key = cache_key(text, voice)
            hit = self.cache.get(key)
            if hit is not None:
                return SynthResult(hit, voice, cached=True, degraded=degraded)

            destination = self.cache.path_for(key)
            try:
                self._render_whole(text, voice, destination)
            except SynthError as exc:
                LOG.warning(
                    "voice %s/%s could not speak (%s); trying the next one",
                    voice.persona,
                    voice.kind,
                    exc,
                )
                destination.unlink(missing_ok=True)
                degraded = True
                continue
            self.cache.evict()
            return SynthResult(destination, voice, cached=False, degraded=degraded)

        LOG.warning("no voice could speak %r: the card will appear silently", text[:60])
        return SynthResult(degraded=True)

    def _render_whole(self, text: str, voice: VoiceSpec, destination: Path) -> None:
        """Build the file beside its cache entry and move it in when it is done.

        A `piper` killed halfway — the child logged out, the session ended, the
        machine was switched off at the wall — leaves a truncated WAV behind,
        and the cache would serve that as a hit for as long as the family keeps
        the machine. The line would never be heard again and nothing would say
        why. A rename within one directory is atomic, so the next read finds
        either no file or a finished one.
        """
        partial = destination.with_name(f"{destination.stem}.part{destination.suffix}")
        try:
            self.backend.render(text, voice, partial)
            partial.replace(destination)
        except OSError as exc:
            raise SynthError(f"could not store {destination.name}: {exc}") from exc
        finally:
            # Kept inside the cache's own naming so that eviction reaps whatever
            # a hard kill leaves behind; this only runs when the move did not.
            partial.unlink(missing_ok=True)
