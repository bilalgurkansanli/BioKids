"""Turning a sentence into a WAV file.

Piper is invoked as a **subprocess**, never imported. That is partly taste — a
CPU inference engine has no business inside a service whose real job is a queue
— and mostly licensing: D-30 allows GPL software invoked as a separate process
and forbids linking it, and calling `piper` over a pipe keeps us on the right
side of that line permanently, whatever Piper's packaging does next.

Nothing here touches the network. Piper is offline by design (VOICE.md §2) and
`SECURITY.md` §4 lists no TTS destination at all: cloud voices were cut (D-181).

Three backends:

* `PiperSynth` is the best one, and it fails loudly when its model is missing.
* `EspeakSynth` is the one a stock Debian machine actually has. It speaks
  through the MBROLA Turkish voices when a family has installed them, and with
  espeak-ng's own voice otherwise.
* `StubSynth` writes silence of roughly the right length. It exists so that the
  service starts, the queue runs, and the whole test suite passes on a machine
  with no TTS installed and no sound card. It still honours a missing model
  file, so the Piper → espeak-ng fallback can be tested without Piper.

The cache is the reason this is cheap. A family hears the same handful of
sentences every day; after the first afternoon almost nothing is synthesised
live, which is what lets a warning arrive in the same tick it was decided
rather than a second and a half later.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import subprocess
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Final, Protocol, cast

from biokids_core.config import Config, PathsConfig, get_config
from biokids_core.errors import BioKidsError

LOG = logging.getLogger(__name__)

SAMPLE_RATE: Final = 22_050
"""What Piper's Turkish voices emit, and therefore what silence should match."""

CHARS_PER_SECOND: Final = 14.0
MIN_DURATION_S: Final = 0.8
MAX_DURATION_S: Final = 15.0
PIPER_TIMEOUT_S: Final = 20.0

STOCK_MODEL_SUBDIR: Final = "piper"
ESPEAK_CACHE_SUBDIR: Final = "espeak"
"""espeak-ng's audio is cached apart from Piper's, for the same reason the stub's
is: the cache key names the sentence and the voice and never the engine."""
ESPEAK_TUNING: Final = 2
"""Bumped whenever espeak-ng is asked to sound different. Part of what its cache
remembers, so a family hears the change instead of yesterday's audio."""
MBROLA_DIR: Final = Path("/usr/share/mbrola")
"""Where Debian's `mbrola-*` packages put their databases."""
MADE_BY_FILE: Final = ".made-by"
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
    """Always `stock` since voice cloning was cut (D-181); a parent's own voice
    is a recording the service plays directly (D-174)."""
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
    """Which voices to try for a persona: the Piper voice a family installed,
    if any. espeak-ng, further down the chain, needs no model."""

    def __init__(self, config: Config | None = None) -> None:
        self._config = config or get_config()

    def candidates(self, persona: str, locale: str) -> tuple[VoiceSpec, ...]:
        stock_model = self.stock_model(persona, locale)
        return (
            VoiceSpec(
                persona=persona,
                locale=locale,
                kind="stock",
                profile_id=f"stock:{persona}:{locale}:{_fingerprint(stock_model)}",
                model_path=stock_model,
            ),
        )

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
        return candidate if _present(candidate) else None


_UNREADABLE: set[Path] = set()


def _present(path: Path, *, directory: bool = False) -> bool:
    """Is it there, as far as this process is allowed to know?

    The voice service runs as the child, and a model directory configured
    somewhere the child cannot read - the family's 0700 data directory, say - is
    not an error.
    Python 3.13's `is_dir()` raises `PermissionError` there instead of answering,
    so on the first real machine every line crashed the worker before espeak-ng
    was ever tried, and the child heard nothing at all (D-160). Not being
    allowed to look is answered as "not here", and said once in the log.
    """
    try:
        return path.is_dir() if directory else path.is_file()
    except PermissionError:
        if path not in _UNREADABLE:
            _UNREADABLE.add(path)
            LOG.warning(
                "%s cannot be read by this user; any voice there is skipped and "
                "the next one in the chain speaks instead",
                path,
            )
        return False
    except OSError:
        return False


def _fingerprint(path: Path | None) -> str:
    """Enough of a model file's identity to notice that it changed.

    `profile_id` is part of the cache key (VOICE.md §6), and a family who
    installs a different Piper voice would otherwise keep hearing the old one:
    every sentence is already in the cache. Size and modification time move;
    hashing sixty megabytes of ONNX before every line would not be worth it.
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
        if not _present(voice.model_path):
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


@dataclass(frozen=True, slots=True)
class MbrolaVoice:
    """One MBROLA database, as Debian's `mbrola-<name>` package installs it."""

    database: str
    """`tr2`: the directory under /usr/share/mbrola and the file inside it."""
    volume: float
    """mbrola's `-v`. The databases were recorded at very different levels, and
    the Turkish woman is a third as loud as espeak-ng without it."""


class EspeakSynth:
    """espeak-ng, which every Debian machine can have and none has to download.

    The fallback that makes the exit criterion reachable. Piper sounds better
    and is tried first, but `piper-tts` is not in Debian 13 and the last Turkish
    Piper voice is licensed CC-BY-NC-SA, so on an ordinary install Piper is
    simply not there. espeak-ng is in main, speaks Turkish, and carries its
    voices inside the package - no model file, no network, nothing to license.

    On its own it sounds like a 1990s robot. That is the right trade against
    silence: a child who hears "bes dakika kaldi" has been warned, and a child
    watching a card they may not read yet has not. Two things make it gentler
    (D-161). It speaks more slowly, with espeak-ng's female variant for Zio and
    its male one for Sio. And where a family has installed the MBROLA Turkish
    voices - `mbrola-tr2`, a woman, and `mbrola-tr1`, a man, from `contrib` and
    `non-free` - espeak-ng only works out the phonemes and MBROLA speaks them
    from recordings of real voices, which is a different thing to listen to.
    """

    name = "espeak-ng"

    #: espeak-ng's own variants: one voice, shaped female or male. Zio is the
    #: mother and Sio the father (D-85). Before D-161 the pitches were the
    #: other way round, so the mother spoke a full octave below the father.
    VARIANT: Final[dict[str, str]] = {"zio": "f3", "sio": "m3"}
    PITCH: Final[dict[str, int]] = {"zio": 55, "sio": 40}
    DEFAULT_PITCH: Final = 50
    #: espeak-ng's default is 175. A four-year-old needs the sentence slower
    #: than an adult reading along does; 150 was still a rush.
    WORDS_PER_MINUTE: Final = 140

    #: The MBROLA voices worth reaching for, by language and persona.
    MBROLA: Final[dict[tuple[str, str], MbrolaVoice]] = {
        ("tr", "zio"): MbrolaVoice("tr2", volume=2.6),
        ("tr", "sio"): MbrolaVoice("tr1", volume=1.5),
    }
    #: Phonemes espeak-ng 1.52's Turkish produces that its own `tr1_phtrans`
    #: table never learned, so MBROLA meets them as unknown diphones and
    #: speaks silence instead: the "l" of every "kaldı" and the "e" of every
    #: "gel". Upstream's table has the same gap. Mapped here to the nearest
    #: sound the tr1/tr2 diphone sets do have, which is why BioKids runs the
    #: two programs itself instead of letting espeak-ng call MBROLA.
    MBROLA_PHONEMES: Final[dict[str, str]] = {"L/": "l", "l/": "l", "&": "e"}

    def __init__(
        self,
        binary: str = "espeak-ng",
        *,
        mbrola_binary: str = "mbrola",
        mbrola_dir: Path | None = None,
        timeout: float = PIPER_TIMEOUT_S,
    ) -> None:
        self._binary = binary
        self._mbrola_binary = mbrola_binary
        self._timeout = timeout
        # Decided once, so that `flavour` - which the cache keys itself on -
        # and `render` cannot disagree about which voice is speaking. A family
        # who installs MBROLA hears it from the next login.
        self._mbrola = find_mbrola_voices(mbrola_binary, mbrola_dir or MBROLA_DIR, self.MBROLA)

    def available(self) -> bool:
        return which(self._binary) is not None

    @property
    def flavour(self) -> str:
        """What this engine's audio sounds like, in one line, for the cache."""
        found = ", ".join(
            f"{persona}={path.name}" for (_, persona), path in sorted(self._mbrola.items())
        )
        return f"espeak-ng tuning {ESPEAK_TUNING}; mbrola: {found or 'none'}"

    def render(self, text: str, voice: VoiceSpec, destination: Path) -> None:
        if not self.available():
            raise SynthError(f"{self._binary} is not installed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # `tr` rather than `tr_TR`: espeak-ng names languages, not locales,
        # and an unknown name is a hard failure rather than a fallback.
        language = voice.locale.split("_")[0].split("-")[0] or "tr"

        database = self._mbrola.get((language, voice.persona))
        if database is not None:
            try:
                self._render_mbrola(
                    text, database, self.MBROLA[(language, voice.persona)], destination
                )
                return
            except SynthError as exc:
                # Not degraded: the same engine, its plainer voice. Logged,
                # because a family who installed MBROLA should be able to
                # find out why they are hearing the robot.
                LOG.warning(
                    "mbrola could not speak %s (%s); using espeak-ng's own voice",
                    voice.persona,
                    exc,
                )
                destination.unlink(missing_ok=True)

        variant = self.VARIANT.get(voice.persona)
        finished = self._run(
            [
                self._binary,
                "-v",
                f"{language}+{variant}" if variant else language,
                "-p",
                str(self.PITCH.get(voice.persona, self.DEFAULT_PITCH)),
                "-s",
                str(self.WORDS_PER_MINUTE),
                # Text on stdin, so a sentence starting with a dash is a
                # sentence and not an option.
                "--stdout",
            ],
            text.encode("utf-8"),
        )
        if not finished.stdout:
            raise SynthError(f"{self._binary} produced no audio")
        destination.write_bytes(finished.stdout)

    def _render_mbrola(
        self, text: str, database: Path, voice: MbrolaVoice, destination: Path
    ) -> None:
        """espeak-ng for the phonemes, the fix-up, then MBROLA for the sound."""
        phonemes = self._run(
            [
                self._binary,
                "-v",
                f"mb-{voice.database}",
                "-s",
                str(self.WORDS_PER_MINUTE),
                "-q",
                "--pho",
            ],
            text.encode("utf-8"),
        ).stdout
        if not phonemes.strip():
            raise SynthError(f"{self._binary} produced no phonemes")

        fixed = translate_phonemes(phonemes.decode("utf-8", "replace"), self.MBROLA_PHONEMES)
        # MBROLA picks the format from the output's extension; the cache's
        # partial files keep `.wav`. It writes the file itself, header and
        # all, rather than a stream whose header never learns its length.
        self._run(
            [
                self._mbrola_binary,
                "-e",  # an unknown diphone is a short gap, not a failed line
                "-v",
                str(voice.volume),
                str(database),
                "-",
                str(destination),
            ],
            fixed.encode("utf-8"),
        )
        if not _present(destination) or destination.stat().st_size <= WAV_HEADER_BYTES:
            raise SynthError(f"{self._mbrola_binary} produced no audio")

    def _run(self, argv: list[str], stdin: bytes) -> subprocess.CompletedProcess[bytes]:
        try:
            finished = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise SynthError(f"{argv[0]} could not be run: {exc}") from exc
        if finished.returncode != 0:
            detail = finished.stderr.decode("utf-8", "replace").strip()[:200]
            raise SynthError(f"{argv[0]} exited {finished.returncode}: {detail}")
        return finished


WAV_HEADER_BYTES: Final = 44


def find_mbrola_voices(
    binary: str, directory: Path, wanted: dict[tuple[str, str], MbrolaVoice]
) -> dict[tuple[str, str], Path]:
    """The databases among `wanted` this machine can actually speak with.

    Both halves are checked: espeak-ng ships a voice file naming every MBROLA
    database there is, installed or not, so `espeak-ng --voices` is no guide.
    Debian puts each database at `<dir>/<name>/<name>`; a hand-installed one is
    often just `<dir>/<name>`.
    """
    if which(binary) is None:
        return {}
    found: dict[tuple[str, str], Path] = {}
    for key, voice in wanted.items():
        for candidate in (
            directory / voice.database / voice.database,
            directory / voice.database,
        ):
            if _present(candidate):
                found[key] = candidate
                break
    return found


def translate_phonemes(pho: str, fixes: dict[str, str]) -> str:
    """Rename the phonemes MBROLA would not know, leaving timing and pitch alone.

    A `.pho` line is the phoneme, its length in milliseconds, then pitch
    points; comment lines start with `;`. Only the first field is touched.
    """
    lines: list[str] = []
    for line in pho.splitlines():
        phoneme, separator, rest = line.partition("\t")
        if not separator:
            phoneme, separator, rest = line.partition(" ")
        lines.append(fixes.get(phoneme, phoneme) + separator + rest)
    return "\n".join(lines) + "\n"


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
        # Piper → espeak-ng fallback can be tested without installing Piper.
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


def cache_root(config: Config | None = None) -> Path:
    """Where rendered audio goes, for a process running as the child.

    Not `/var/lib/biokids/voice-cache`, although that is what the config says.
    The family's data directory is 0700 root — a child's session may not read
    the history of every child in the house — so the voice service, which runs
    as the child with no privilege at all, cannot even traverse it. Writing
    there raises `PermissionError` on the very first warning, and because that
    is an `OSError` rather than a `SynthError` it escapes the fallback and every
    spoken line silently becomes an on-screen card, for the life of the machine,
    with nothing louder than a stack trace in the child's own journal.

    So: `$CACHE_DIRECTORY` when systemd made one (the user unit asks for it with
    `CacheDirectory=`), then the XDG cache, then `~/.cache`. The configured path
    is honoured only when an administrator set it deliberately, which is what
    keeps `BIOKIDS_PREFIX` and the test suite working.
    """
    config = config or get_config()

    from_systemd = os.environ.get("CACHE_DIRECTORY", "").split(":")[0]
    if from_systemd:
        return Path(from_systemd)

    if config.paths.voice_cache_dir != _DEFAULT_CACHE_DIR:
        # Set on purpose, in config.toml or by BIOKIDS_PREFIX.
        return config.paths.voice_cache_dir

    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "biokids-voice"


_DEFAULT_CACHE_DIR = PathsConfig().voice_cache_dir
"""The built-in default, so a configured path can be told from an inherited one."""


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
        directory = cache_root(config)
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

    def made_by(self, maker: str) -> None:
        """Empty the cache if what is in it was made some other way.

        The key is the sentence and the voice, never how it was synthesised.
        Without this a family who installs the MBROLA voices - or who upgrades
        to a gentler espeak-ng tuning - goes on hearing the old audio for every
        line already heard once, which is every line there is. A cache with no
        record at all predates the record and is emptied too.
        """
        stamp = self.directory / MADE_BY_FILE
        try:
            previous = stamp.read_text(encoding="utf-8").strip()
        except OSError:
            previous = None
        if previous == maker:
            return
        self.clear()
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp.write_text(maker + "\n", encoding="utf-8")
        except OSError as exc:
            # Harmless beyond a cache emptied again at the next start.
            LOG.warning("voice cache: could not record what made it: %s", exc)

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
    """The Piper → espeak-ng → nothing chain of VOICE.md §6, cache in front.

    Holds *engines*, plural. Choosing one at construction is what made the chain
    not a chain: `which("piper")` says a binary exists, not that it can speak,
    and BioKids ships no Piper model - so a family who installed piper got
    "no voice model" for every line for ever while espeak-ng sat there unused.
    """

    def __init__(
        self,
        engines: Synth | Sequence[tuple[Synth, VoiceCache]],
        cache: VoiceCache | None = None,
    ) -> None:
        if cache is not None:
            # The one-engine form, which the tests and `from_config`'s simpler
            # paths still use.
            engines = [(cast("Synth", engines), cache)]
        self.engines: list[tuple[Synth, VoiceCache]] = list(
            cast("Sequence[tuple[Synth, VoiceCache]]", engines)
        )
        if not self.engines:
            raise ValueError("a synthesizer needs at least one engine")
        self._spoke_with: Synth | None = None

    @property
    def backend(self) -> Synth:
        """Whichever engine last actually produced audio, else the preferred one.

        `status` reports this. Before the fix it reported the engine that had
        been *selected*, which is how a machine that had been silent for a week
        still said `backend: "piper"`.
        """
        return self._spoke_with or self.engines[0][0]

    @property
    def cache(self) -> VoiceCache:
        """The preferred engine's cache. Kept for `status` and the tests."""
        return self.engines[0][1]

    @classmethod
    def from_config(cls, config: Config | None = None) -> Synthesizer:
        """Piper, then espeak-ng, then silence.

        espeak-ng is in the middle because on a stock Debian 13 machine it is
        the only one of the three that is actually there: `piper-tts` is not in
        the archive, and the last Turkish Piper voice is licensed CC-BY-NC-SA
        (see `assets/voices/LICENSES.md`). Without this rung the chain went
        straight from "no Piper" to silence, and the Phase 0 exit criterion -
        warnings *spoken* in Turkish - was unreachable by any install.

        Falling back to silence rather than refusing to start is what keeps the
        service, the card and the queue working on a machine with neither — but
        it is loud about it, because a family who expected a voice and got none
        deserves a reason in the log.
        """
        config = config or get_config()
        engines: list[tuple[Synth, VoiceCache]] = []

        piper = PiperSynth(config.voice.piper_binary)
        if piper.available():
            engines.append((piper, VoiceCache.from_config(config)))

        espeak = EspeakSynth(
            config.voice.espeak_binary, mbrola_binary=config.voice.mbrola_binary
        )
        if espeak.available():
            # Always appended when present, never instead of Piper. A Piper that
            # is installed but has no model raises on every line, and this is
            # what catches that - which is the entire reason the chain is a list.
            #
            # Its own cache directory, for the reason the stub has one: the key
            # describes the sentence and the voice, never the engine, so a
            # family who installs Piper later would otherwise keep hearing the
            # robot for every line they have already heard.
            espeak_cache = VoiceCache.from_config(config, subdir=ESPEAK_CACHE_SUBDIR)
            espeak_cache.made_by(espeak.flavour)
            LOG.info("speaking with %s", espeak.flavour)
            engines.append((espeak, espeak_cache))

        if engines:
            if piper.available() and not espeak.available():
                LOG.warning(
                    "%s is installed but %s is not; if no voice model is present "
                    "nothing will be spoken at all",
                    config.voice.piper_binary,
                    config.voice.espeak_binary,
                )
            return cls(engines)

        LOG.warning(
            "neither %s nor %s is installed: cards will appear but nothing will be spoken",
            config.voice.piper_binary,
            espeak.name,
        )
        # Silence gets a cache of its own. The key is the sentence, the persona
        # and the profile (VOICE.md §6) and says nothing about which engine
        # produced the audio, so a shared directory would leave a family who
        # installed Piper afterwards listening to yesterday's empty WAVs for
        # every line they hear every day, with `status` cheerfully reporting
        # `piper`.
        return cls(StubSynth(), VoiceCache.from_config(config, subdir=STUB_CACHE_SUBDIR))

    def render(self, text: str, voices: Sequence[VoiceSpec]) -> SynthResult:
        """Every voice, through every engine, until one of them speaks.

        Voice is the outer loop and engine the inner one, which is the order
        VOICE.md §6 describes: the family's Piper voice before espeak-ng,
        and Piper before espeak-ng within each. Never raises; the worst case is
        no audio and a card that carries the line as text.
        """
        degraded = False
        for voice in voices:
            key = cache_key(text, voice)
            for backend, cache in self.engines:
                hit = cache.get(key)
                if hit is not None:
                    self._spoke_with = backend
                    return SynthResult(hit, voice, cached=True, degraded=degraded)

            for backend, cache in self.engines:
                destination = cache.path_for(key)
                try:
                    self._render_whole(backend, text, voice, destination)
                except SynthError as exc:
                    LOG.warning(
                        "%s could not speak %s/%s (%s); trying the next engine",
                        backend.name,
                        voice.persona,
                        voice.kind,
                        exc,
                    )
                    destination.unlink(missing_ok=True)
                    degraded = True
                    continue
                cache.evict()
                self._spoke_with = backend
                return SynthResult(destination, voice, cached=False, degraded=degraded)

        LOG.warning("no voice could speak %r: the card will appear silently", text[:60])
        return SynthResult(degraded=True)

    def _render_whole(
        self, backend: Synth, text: str, voice: VoiceSpec, destination: Path
    ) -> None:
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
            backend.render(text, voice, partial)
            partial.replace(destination)
        except OSError as exc:
            raise SynthError(f"could not store {destination.name}: {exc}") from exc
        finally:
            # Kept inside the cache's own naming so that eviction reaps whatever
            # a hard kill leaves behind; this only runs when the move did not.
            #
            # Suppressed, because an OSError raised here would *replace* the
            # SynthError being raised — and `render` promises it never raises.
            # A full disk or a read-only home would then turn a well-handled
            # fallback into an unhandled traceback in the child's journal.
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
