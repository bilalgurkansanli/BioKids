"""Tests for `biokids-voice`.

The list in VOICE.md §9 is the specification for this file, so each of its
bullets appears below under a name you can match to it by eye.

Nothing here needs Piper, a sound card, PipeWire or a unix socket: the suite
runs on the Windows host this project is developed from, which is only possible
because every edge of the service — synthesis, playback and the mixer — is a
protocol with a headless implementation. That is not a testing convenience; it
is the same seam that lets the service start on a machine with no audio at all.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import random
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import Database
from biokids_core.db.models import VoiceLine
from biokids_core.ipc import Event, Request
from biokids_voice import synth as synth_module
from biokids_voice.lines import DEFAULT_EVENTS, LineBook
from biokids_voice.player import AudioDucker, NullPlayer, PipeWirePlayer, SilentPlayer
from biokids_voice.service import (
    EVENT_FINISHED,
    EVENT_STARTED,
    STATUS_BANNER,
    STATUS_DROPPED,
    STATUS_QUEUED,
    STATUS_SILENT,
    STATUS_SPOKEN,
    VoiceService,
)
from biokids_voice.synth import (
    ESPEAK_CACHE_SUBDIR,
    STUB_CACHE_SUBDIR,
    EspeakSynth,
    PiperSynth,
    StubSynth,
    SynthError,
    Synthesizer,
    VoiceBank,
    VoiceCache,
    VoiceSpec,
    cache_key,
    find_mbrola_voices,
    translate_phonemes,
    write_silence,
)

LOCALES = ("tr", "en")
TIMEOUT = 2.0


def no_hold(_text: str) -> float:
    """Cards dismiss instantly here; how long they linger is not what is under test."""
    return 0.0


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def no_mbrola(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the machine running the suite has installed, espeak-ng speaks alone."""
    monkeypatch.setattr(synth_module, "MBROLA_DIR", tmp_path_factory.mktemp("no-mbrola"))


@pytest.fixture
def voice_config(config: Config) -> Config:
    """The shared config, with a queue small enough to overflow deliberately."""
    return replace(config, voice=replace(config.voice, queue_depth=3, duck_percent=30))


@pytest.fixture
def cache(voice_config: Config) -> VoiceCache:
    made = VoiceCache.from_config(voice_config)
    made.directory.mkdir(parents=True, exist_ok=True)
    return made


@pytest.fixture
def stub(cache: VoiceCache) -> Synthesizer:
    return Synthesizer(StubSynth(), cache)


@pytest.fixture
def lines(voice_config: Config) -> LineBook:
    """Seeded, so a test that asserts on a chosen variant is not flaky."""
    return LineBook(voice_config, rng=random.Random(20260919))


def build_service(
    config: Config,
    *,
    synthesizer: Synthesizer | None = None,
    player: NullPlayer | None = None,
    clock: FakeClock | None = None,
) -> VoiceService:
    return VoiceService(
        config,
        lines=LineBook(config, rng=random.Random(7)),
        voices=VoiceBank(config),
        synthesizer=synthesizer,
        player=player,
        clock=clock or FakeClock(wall=dt.datetime(2026, 9, 21, 16, 0, 0)),
        hold=no_hold,
        rng=random.Random(7),
    )


def speak(event: str, **args: object) -> Request:
    payload: dict[str, object] = {"event": event, "locale": "tr"}
    payload.update(args)
    return Request(op="speak", id=event, args=payload)


def install_line(config: Config, locale: str, event: str, text: str) -> None:
    """Write a line where the `.deb` puts it, under `/usr/share/biokids/voice`."""
    path = config.paths.voice_lines_dir / locale / f"{event}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{text}\n", encoding="utf-8")


class Recorder:
    """A subscriber that keeps every frame, the way the shell's SSE bridge will."""

    def __init__(self, service: VoiceService) -> None:
        self._frames: AsyncIterator[Event] = service.events()
        self._task: asyncio.Task[None] | None = None
        self._arrived = asyncio.Event()
        self.frames: list[Event] = []

    async def __aenter__(self) -> Recorder:
        self._task = asyncio.create_task(self._consume())
        await asyncio.sleep(0.01)  # let the subscription register
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await self._task

    async def _consume(self) -> None:
        async for frame in self._frames:
            self.frames.append(frame)
            self._arrived.set()

    def started(self) -> list[str]:
        return [f.payload["id"] for f in self.frames if f.kind == EVENT_STARTED]

    def outcome(self, utterance_id: str) -> str | None:
        for frame in self.frames:
            if frame.kind == EVENT_FINISHED and frame.payload["id"] == utterance_id:
                return str(frame.payload["status"])
        return None

    async def wait_for(self, utterance_id: str, limit: float = TIMEOUT) -> str:
        await self._until(lambda: self.outcome(utterance_id) is not None, limit)
        found = self.outcome(utterance_id)
        assert found is not None
        return found

    async def wait_for_count(self, count: int, limit: float = TIMEOUT) -> None:
        await self._until(
            lambda: len([f for f in self.frames if f.kind == EVENT_FINISHED]) >= count,
            limit,
        )

    async def _until(self, ready: Callable[[], bool], limit: float) -> None:
        async with asyncio.timeout(limit):
            while not ready():
                # Nothing awaits between the check and the clear, so a frame
                # cannot arrive in the gap and have its wake-up discarded.
                self._arrived.clear()
                await self._arrived.wait()


# ------------------------------------------------- every default line renders


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("event", DEFAULT_EVENTS)
def test_every_default_line_renders(lines: LineBook, event: str, locale: str) -> None:
    """VOICE.md §9: every stock line renders in both locales, without error."""
    variants = lines.variants(event, locale)
    assert len(variants) >= 2, f"{locale}/{event} needs at least two phrasings"

    expected = {
        variant.replace("{name}", "Elif").replace("{minutes}", "10") for variant in variants
    }
    seen: set[str] = set()
    for _ in range(len(variants) * 20):
        line = lines.render(event, locale, {"name": "Elif", "minutes": 10})
        assert line is not None
        assert line.text
        assert "{" not in line.text and "}" not in line.text
        assert line.text in expected
        seen.add(line.text)

    assert len(seen) > 1, "the same wording every time is what §5 asks us to avoid"


@pytest.mark.parametrize("event", DEFAULT_EVENTS)
def test_both_locales_ship_the_same_events(lines: LineBook, event: str) -> None:
    for locale in LOCALES:
        assert event in lines.events(locale)


def test_a_line_without_a_name_avoids_the_variants_that_need_one(lines: LineBook) -> None:
    """A greeting with a hole where the name belongs is worse than a shorter one."""
    for _ in range(30):
        line = lines.render("welcome", "tr", {"minutes": 45})
        assert line is not None
        assert "Elif" not in line.text
        assert "  " not in line.text
        assert " ," not in line.text


def test_an_unknown_placeholder_is_never_spoken(lines: LineBook) -> None:
    rendered = lines.render("time_up", "tr", {"app": "Boya"})
    assert rendered is not None
    assert "{" not in rendered.text


def test_an_unknown_event_says_nothing(lines: LineBook) -> None:
    assert lines.render("bedtime_for_bonzo", "tr", {}) is None


def test_an_event_name_cannot_escape_the_line_directory(lines: LineBook) -> None:
    """The event key arrives over a socket and becomes a filename."""
    assert lines.variants("../../../etc/passwd", "tr") == ()
    assert lines.render("..", "tr", {}) is None


def test_a_parent_override_wins_over_the_shipped_line(
    db: Database, voice_config: Config
) -> None:
    """VOICE.md §5: a parent can rewrite every line, and an upgrade must not lose it."""
    book = LineBook(voice_config, rng=random.Random(1))
    with db.session() as session:
        session.add(VoiceLine(event="warn_10", locale="tr", text="Canım, on dakikan var."))
        session.flush()
        rendered = book.render("warn_10", "tr", {"minutes": 10}, session=session)

    assert rendered is not None
    assert rendered.text == "Canım, on dakikan var."
    assert rendered.source == "parent"


# ----------------------------------------------------------------- the queue


class GatedPlayer(NullPlayer):
    """Holds the first line until the test lets go.

    Without it the worker picks the first request up before the second arrives,
    and "three simultaneous requests" never actually happens.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def _elapse(self, path: Path) -> None:
        if len(self.played) == 1:
            await self.gate.wait()


async def test_three_simultaneous_requests_play_in_priority_order(
    voice_config: Config, stub: Synthesizer
) -> None:
    """VOICE.md §9: three at once, in priority order, none lost, none overlapping.

    `NullPlayer` raises if a line starts while another is still playing, so
    "none overlaps" is asserted by the player itself, on every one of these.
    """
    player = GatedPlayer()
    service = build_service(voice_config, synthesizer=stub, player=player)
    async with Recorder(service) as recorder:
        await service.start()
        try:
            first = await service.handle(speak("welcome", vars={"name": "Elif", "minutes": 60}))
            await asyncio.sleep(0.05)  # the worker takes it and stops at the gate

            queued = [
                await service.handle(speak("warn_5", vars={"minutes": 5})),
                await service.handle(speak("time_up", priority="high", vars={"name": "Elif"})),
                await service.handle(speak("warn_1", vars={"minutes": 1})),
            ]
            assert [reply.data["status"] for reply in queued] == [STATUS_QUEUED] * 3

            player.gate.set()
            await recorder.wait_for_count(4)
        finally:
            await service.aclose()

    assert len(player.played) == 4, "a queued line was lost"
    assert recorder.started() == [
        first.data["id"],
        queued[1].data["id"],  # high priority, although it arrived second
        queued[0].data["id"],  # then the normals, in the order they arrived
        queued[2].data["id"],
    ]


async def test_a_full_queue_drops_the_oldest_normal_line(
    voice_config: Config, stub: Synthesizer
) -> None:
    """VOICE.md §6: drop, never talk over — and dismiss the dropped line's card."""
    player = GatedPlayer()
    service = build_service(voice_config, synthesizer=stub, player=player)
    async with Recorder(service) as recorder:
        await service.start()
        try:
            await service.handle(speak("welcome", vars={"name": "Elif", "minutes": 60}))
            await asyncio.sleep(0.05)

            oldest = await service.handle(speak("warn_10", vars={"minutes": 10}))
            await service.handle(speak("warn_5", vars={"minutes": 5}))
            await service.handle(speak("warn_1", vars={"minutes": 1}))
            newcomer = await service.handle(speak("time_up", vars={"name": "Elif"}))

            assert newcomer.data["status"] == STATUS_QUEUED
            dismissed = await recorder.wait_for(str(oldest.data["id"]))
            assert dismissed == STATUS_DROPPED, "a dropped line must dismiss its card"

            player.gate.set()
            await recorder.wait_for_count(5)
        finally:
            await service.aclose()

    assert len(player.played) == 4
    assert oldest.data["id"] not in recorder.started()


async def test_the_last_line_of_the_day_is_urgent_without_being_asked(
    voice_config: Config, stub: Synthesizer
) -> None:
    """VOICE.md §6: `time_up` is high whether or not the caller fills the field.

    The daemon that sends it is not the place this belongs: a caller that omits
    the field would otherwise leave the last line of the day queued behind a
    cheerful one, or lose it altogether when the queue is full.
    """
    player = GatedPlayer()
    service = build_service(voice_config, synthesizer=stub, player=player)
    async with Recorder(service) as recorder:
        await service.start()
        try:
            await service.handle(speak("welcome", vars={"name": "Elif", "minutes": 60}))
            await asyncio.sleep(0.05)  # the worker takes it and stops at the gate

            warning = await service.handle(speak("warn_5", vars={"minutes": 5}))
            over = await service.handle(speak("time_up", vars={"name": "Elif"}))
            assert over.data["priority"] == "high"
            assert warning.data["priority"] == "normal"

            player.gate.set()
            await recorder.wait_for_count(3)
        finally:
            await service.aclose()

    started = recorder.started()
    assert started.index(over.data["id"]) < started.index(warning.data["id"])


async def test_a_queue_of_urgent_lines_drops_the_newcomer(
    voice_config: Config, stub: Synthesizer
) -> None:
    """Nothing urgent is thrown away to make room for something else urgent."""
    player = GatedPlayer()
    service = build_service(voice_config, synthesizer=stub, player=player)
    await service.start()
    try:
        await service.handle(speak("welcome", vars={"name": "Elif", "minutes": 60}))
        await asyncio.sleep(0.05)
        for _ in range(3):
            reply = await service.handle(speak("time_up", priority="high"))
            assert reply.data["status"] == STATUS_QUEUED

        refused = await service.handle(speak("warn_5", priority="high", vars={"minutes": 5}))
        assert refused.data["status"] == STATUS_DROPPED
        assert refused.ok, "a dropped line is still an answer, not an error"

        player.gate.set()
    finally:
        await service.aclose()


# ---------------------------------------------------------------- the cache


def test_the_second_identical_request_is_a_cache_hit(stub: Synthesizer) -> None:
    """VOICE.md §9: almost nothing is synthesised live after the first day."""
    backend = stub.backend
    assert isinstance(backend, StubSynth)
    voice = VoiceSpec(persona="zio", locale="tr", profile_id="stock:zio:tr")

    first = stub.render("On dakikan kaldı.", [voice])
    second = stub.render("On dakikan kaldı.", [voice])

    assert first.path == second.path
    assert first.cached is False
    assert second.cached is True
    assert backend.calls == 1, "the same line was synthesised twice"
    assert stub.cache.hits == 1


def test_a_half_written_line_never_becomes_a_cache_entry(cache: VoiceCache) -> None:
    """A `piper` killed mid-line must not silence that line for ever.

    The cache is keyed on the sentence, so a truncated WAV would be served as a
    hit every afternoon from then on, and nothing would say why the warning
    stopped being heard.
    """

    class KilledSynth(StubSynth):
        def render(self, text: str, voice: VoiceSpec, destination: Path) -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"RIFF" + b"\x00" * 16)  # a header and no audio
            raise SynthError("piper was killed")

    synth = Synthesizer(KilledSynth(), cache)
    voice = VoiceSpec(persona="zio", locale="tr", profile_id="stock:zio:tr")

    result = synth.render("Bir dakika kaldı.", [voice])

    assert not result.spoken
    assert cache.get(cache_key("Bir dakika kaldı.", voice)) is None
    assert list(cache.directory.glob("*")) == [], "a partial file was left behind"


def test_a_finished_line_leaves_nothing_half_written_behind(stub: Synthesizer) -> None:
    voice = VoiceSpec(persona="zio", locale="tr", profile_id="stock:zio:tr")
    result = stub.render("On dakikan kaldı.", [voice])

    assert result.path is not None
    assert list(stub.cache.directory.glob("*")) == [result.path]


def test_a_new_piper_voice_invalidates_the_audio_cached_for_it(
    voice_config: Config,
) -> None:
    """VOICE.md §6 keys audio on the voice profile, so the profile has to move.

    Without this a family who re-records Zio keeps hearing the voice they threw
    away: every sentence they hear every day is already in the cache.
    """
    model = voice_config.paths.model_dir / "piper" / "tr-zio.onnx"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.write_bytes(b"the first voice")

    bank = VoiceBank(voice_config)
    before = cache_key("Merhaba, ben Zio.", bank.candidates("zio", "tr")[0])

    model.write_bytes(b"a second voice, of a different length")
    after = cache_key("Merhaba, ben Zio.", bank.candidates("zio", "tr")[0])

    assert before != after


def test_silence_is_cached_apart_from_real_audio(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing Piper later must not leave a family with yesterday's silence."""
    monkeypatch.setattr(PiperSynth, "available", lambda _self: False)
    # And no espeak-ng either, or on a machine that has it - every machine with
    # biokids-voice installed - the chain speaks and the stub is never reached.
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: False)
    stubbed = Synthesizer.from_config(voice_config)
    assert isinstance(stubbed.backend, StubSynth)
    assert stubbed.cache.directory == voice_config.paths.voice_cache_dir / STUB_CACHE_SUBDIR

    monkeypatch.setattr(PiperSynth, "available", lambda _self: True)
    real = Synthesizer.from_config(voice_config)
    assert real.cache.directory == voice_config.paths.voice_cache_dir


def test_a_different_persona_is_a_different_cache_entry(stub: Synthesizer) -> None:
    text = "Bir dakika kaldı."
    zio = VoiceSpec(persona="zio", locale="tr", profile_id="stock:zio:tr")
    sio = VoiceSpec(persona="sio", locale="tr", profile_id="stock:sio:tr")

    assert cache_key(text, zio) != cache_key(text, sio)
    assert stub.render(text, [zio]).path != stub.render(text, [sio]).path


def test_the_cache_evicts_least_recently_used_first(cache: VoiceCache) -> None:
    """A hit bumps recency, so yesterday's greeting outlives a one-off preview."""
    for index in range(4):
        write_silence(cache.path_for(f"line{index}"), seconds=1.0)
        # mtimes are set by hand: four files written in the same millisecond
        # would otherwise sort in an order the filesystem chose, not the test.
        stamp = 1_000_000 + index * 100
        os.utime(cache.path_for(f"line{index}"), (stamp, stamp))

    revived = cache.get("line0")
    assert revived is not None
    cache.max_bytes = cache.size_bytes() - 1

    assert cache.evict() == 1
    assert revived.is_file(), "a file used a moment ago was evicted"
    assert not cache.path_for("line1").is_file(), "the oldest file should have gone"


# ------------------------------------------------------------- the fallback


class BrokenSynth(StubSynth):
    """A machine with no usable voice at all."""

    def render(self, text: str, voice: VoiceSpec, destination: Path) -> None:
        raise SynthError("no voice on this machine")


async def test_with_no_voice_at_all_the_card_still_appears(
    voice_config: Config, cache: VoiceCache
) -> None:
    """The last link of the chain: a banner is a success, not a failure."""
    service = build_service(
        voice_config,
        synthesizer=Synthesizer(BrokenSynth(), cache),
        player=NullPlayer(),
    )
    async with Recorder(service) as recorder:
        await service.start()
        try:
            reply = await service.handle(speak("warn_1", vars={"minutes": 1}))
            assert reply.ok and reply.data["text"]
            assert await recorder.wait_for(str(reply.data["id"])) == STATUS_BANNER
        finally:
            await service.aclose()


# ------------------------------------------------------------------ ducking


class FakeMixer:
    """Two other applications making noise, and a record of what we did to them."""

    def __init__(self, volumes: dict[int, float]) -> None:
        self.volumes = dict(volumes)
        self.original = dict(volumes)

    async def streams(self) -> dict[int, float]:
        return dict(self.volumes)

    async def set_volume(self, node_id: int, volume: float) -> None:
        self.volumes[node_id] = round(volume, 4)


async def test_ducking_lowers_other_audio_and_puts_it_back() -> None:
    mixer = FakeMixer({7: 0.80, 9: 0.50})
    ducker = AudioDucker(mixer, duck_percent=30)

    async with ducker.ducked():
        assert mixer.volumes == {7: 0.24, 9: 0.15}

    await ducker.aclose()
    assert mixer.volumes == mixer.original


async def test_ducking_restores_even_when_playback_is_killed() -> None:
    """VOICE.md §9: the previous volume returns even if the line is killed.

    A cancellation is what a killed `pw-play` and a service stopping mid-line
    both look like from in here, so it is the case worth pinning down.
    """
    mixer = FakeMixer({7: 0.80, 9: 0.50})
    ducker = AudioDucker(mixer, duck_percent=30)

    with pytest.raises(asyncio.CancelledError):
        async with ducker.ducked():
            assert mixer.volumes == {7: 0.24, 9: 0.15}
            raise asyncio.CancelledError

    await ducker.aclose()
    assert mixer.volumes == mixer.original


class StuckProcess:
    """A `pw-play` that never finishes on its own."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = asyncio.Event()

    async def wait(self) -> int:
        await self.killed.wait()
        raise AssertionError("this process is only ever killed")  # pragma: no cover

    def kill(self) -> None:
        self.returncode = -9
        self.killed.set()


async def test_the_player_restores_the_volume_when_its_process_is_killed(
    tmp_path: Path,
) -> None:
    """The same guarantee, through the real player, all the way to the signal."""
    mixer = FakeMixer({3: 1.0})
    process = StuckProcess()

    async def spawn(*_argv: str) -> StuckProcess:
        return process

    player = PipeWirePlayer(
        ducker=AudioDucker(mixer, duck_percent=30),
        commands=((sys.executable,),),
        spawn=spawn,  # type: ignore[arg-type]
    )
    wav = tmp_path / "line.wav"
    write_silence(wav, seconds=0.1)

    playing = asyncio.create_task(player.play(wav))
    await asyncio.sleep(0.05)
    assert mixer.volumes == {3: 0.3}, "other audio should be quieter while a line plays"

    playing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await playing

    await player.aclose()
    assert mixer.volumes == mixer.original
    assert process.returncode == -9, "the playback process must be killed, not orphaned"


# -------------------------------------------------------- silence and cards


async def test_the_card_renders_with_audio_disabled_entirely(voice_config: Config) -> None:
    """VOICE.md §9: the card renders and dismisses with audio switched off."""
    silent = replace(voice_config, voice=replace(voice_config.voice, enabled=False))
    service = build_service(silent)
    async with Recorder(service) as recorder:
        await service.start()
        try:
            reply = await service.handle(
                speak("warn_10", vars={"name": "Elif", "minutes": 10}, expression="neutral")
            )
            assert reply.ok
            assert reply.data["text"], "the card has nothing to show"
            assert reply.data["silent"] is True
            assert reply.data["expression"] == "neutral"
            assert reply.data["persona"] in {"zio", "sio"}

            assert await recorder.wait_for(str(reply.data["id"])) == STATUS_BANNER
        finally:
            await service.aclose()

    assert service.status()["enabled"] is False


async def test_a_machine_with_no_audio_reports_a_banner_not_a_spoken_line(
    voice_config: Config, stub: Synthesizer
) -> None:
    """`--no-audio` has to look exactly like a machine with no sound card.

    Telling the panel that a line was spoken when nothing came out of the
    speakers is the silent failure principle 3 forbids, in miniature.
    """
    player = SilentPlayer()
    service = build_service(voice_config, synthesizer=stub, player=player)
    async with Recorder(service) as recorder:
        await service.start()
        try:
            reply = await service.handle(speak("warn_1", vars={"minutes": 1}))
            assert reply.data["text"], "the card still has to say it"
            assert await recorder.wait_for(str(reply.data["id"])) == STATUS_BANNER
        finally:
            await service.aclose()

    assert service.status()["spoken"] == 0
    assert player.played == [], "nothing is synthesised when nothing can be played"


async def test_quiet_hours_show_the_card_without_speaking(
    voice_config: Config, stub: Synthesizer
) -> None:
    """VOICE.md §6: a parent-set window where the character card appears silently."""
    quiet = replace(voice_config, voice=replace(voice_config.voice, quiet_hours="21:00-08:00"))
    player = NullPlayer()
    service = build_service(
        quiet,
        synthesizer=stub,
        player=player,
        clock=FakeClock(wall=dt.datetime(2026, 9, 21, 22, 30, 0)),
    )
    async with Recorder(service) as recorder:
        await service.start()
        try:
            reply = await service.handle(speak("time_up", vars={"name": "Elif"}))
            assert reply.data["silent"] is True
            assert await recorder.wait_for(str(reply.data["id"])) == STATUS_SILENT
        finally:
            await service.aclose()

    assert player.played == [], "quiet hours must not reach the speakers"


async def test_outside_quiet_hours_the_same_line_is_spoken(
    voice_config: Config, stub: Synthesizer
) -> None:
    quiet = replace(voice_config, voice=replace(voice_config.voice, quiet_hours="21:00-08:00"))
    player = NullPlayer()
    service = build_service(
        quiet,
        synthesizer=stub,
        player=player,
        clock=FakeClock(wall=dt.datetime(2026, 9, 21, 17, 0, 0)),
    )
    async with Recorder(service) as recorder:
        await service.start()
        try:
            reply = await service.handle(speak("time_up", vars={"name": "Elif"}))
            assert await recorder.wait_for(str(reply.data["id"])) == STATUS_SPOKEN
        finally:
            await service.aclose()

    assert len(player.played) == 1


async def test_a_malformed_quiet_hours_window_never_silences_a_warning(
    voice_config: Config, stub: Synthesizer, caplog: pytest.LogCaptureFixture
) -> None:
    broken = replace(voice_config, voice=replace(voice_config.voice, quiet_hours="always"))
    with caplog.at_level(logging.WARNING, logger="biokids_voice.service"):
        service = build_service(broken, synthesizer=stub, player=NullPlayer())
    try:
        assert service.status()["quiet_now"] is False
        assert any("quiet_hours" in record.message for record in caplog.records)
    finally:
        await service.aclose()


# ----------------------------------------------------------------- protocol


async def test_personas_alternate_so_a_child_hears_both(
    voice_config: Config, stub: Synthesizer
) -> None:
    """VOICE.md §5: `auto` alternates, which is the default for every event."""
    service = build_service(voice_config, synthesizer=stub, player=NullPlayer())
    try:
        chosen = [
            (await service.handle(speak("warn_5", persona="auto", vars={"minutes": 5}))).data[
                "persona"
            ]
            for _ in range(4)
        ]
    finally:
        await service.aclose()

    assert chosen == ["zio", "sio", "zio", "sio"]


async def test_the_face_follows_the_event_when_the_caller_names_none(
    voice_config: Config, stub: Synthesizer
) -> None:
    """VOICE.md §7 maps the expression from the event, not from the request.

    `earned` also exercises the installed line file, which is where a packager
    and a future phase's events arrive from.
    """
    install_line(voice_config, "tr", "earned", "Aferin! {minutes} dakika kazandın.")
    service = build_service(voice_config, synthesizer=stub, player=NullPlayer())
    try:
        earned = await service.handle(speak("earned", vars={"minutes": 15}))
        warning = await service.handle(speak("warn_5", vars={"minutes": 5}))
        asked = await service.handle(speak("earned", expression="sleepy", vars={"minutes": 15}))
    finally:
        await service.aclose()

    assert earned.data["text"] == "Aferin! 15 dakika kazandın."
    assert earned.data["expression"] == "happy"
    assert warning.data["expression"] == "neutral"
    assert asked.data["expression"] == "sleepy", "a caller that names a face still wins"


async def test_an_explicit_persona_is_honoured(voice_config: Config, stub: Synthesizer) -> None:
    service = build_service(voice_config, synthesizer=stub, player=NullPlayer())
    try:
        reply = await service.handle(speak("warn_5", persona="sio", vars={"minutes": 5}))
    finally:
        await service.aclose()

    assert reply.data["persona"] == "sio"


async def test_the_panel_preview_goes_to_the_front(
    voice_config: Config, stub: Synthesizer
) -> None:
    """VOICE.md §8: `preview` is what the voice recorder and the line editor use."""
    player = NullPlayer()
    service = build_service(voice_config, synthesizer=stub, player=player)
    async with Recorder(service) as recorder:
        await service.start()
        try:
            reply = await service.handle(
                Request(
                    op="preview",
                    id="p1",
                    args={"text": "Merhaba, ben Zio.", "persona": "zio"},
                )
            )
            assert reply.ok
            assert reply.data["text"] == "Merhaba, ben Zio."
            assert reply.data["priority"] == "high"
            assert await recorder.wait_for(str(reply.data["id"])) == STATUS_SPOKEN
        finally:
            await service.aclose()

    assert len(player.played) == 1


async def test_a_malformed_request_is_refused_without_stopping_the_service(
    voice_config: Config, stub: Synthesizer
) -> None:
    service = build_service(voice_config, synthesizer=stub, player=NullPlayer())
    try:
        assert not (await service.handle(Request(op="speak", id="1", args={}))).ok
        assert not (await service.handle(speak("bedtime_for_bonzo"))).ok
        assert not (await service.handle(Request(op="sing", id="2", args={}))).ok
        assert (await service.handle(Request(op="ping", id="3"))).ok
    finally:
        await service.aclose()


async def test_status_explains_a_quiet_machine(voice_config: Config, stub: Synthesizer) -> None:
    service = build_service(voice_config, synthesizer=stub, player=NullPlayer())
    try:
        status = (await service.handle(Request(op="status", id="s"))).data
    finally:
        await service.aclose()

    assert status["enabled"] is True
    assert status["backend"] == "stub"
    assert status["queue_depth"] == 3
    assert status["quiet_now"] is False


# ---------------------------------------------------- the engine that is there


def test_espeak_speaks_when_piper_is_not_installed(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without this rung the chain went from "no Piper" straight to silence.

    Which is every stock Debian 13 machine: `piper-tts` is not in the archive,
    and the one Turkish Piper voice left in the catalogue is CC-BY-NC-SA, so
    BioKids may not ship it. The Phase 0 exit criterion asks for warnings
    *spoken* in Turkish, and until espeak-ng was tried nothing could speak them.
    """
    monkeypatch.setattr(PiperSynth, "available", lambda _self: False)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)

    chain = Synthesizer.from_config(voice_config)

    assert isinstance(chain.backend, EspeakSynth)
    assert chain.cache.directory == voice_config.paths.voice_cache_dir / ESPEAK_CACHE_SUBDIR


def test_piper_still_wins_when_a_family_has_installed_it(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """espeak-ng is the floor, not the ceiling."""
    monkeypatch.setattr(PiperSynth, "available", lambda _self: True)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)

    chain = Synthesizer.from_config(voice_config)

    assert isinstance(chain.backend, PiperSynth)
    assert chain.cache.directory == voice_config.paths.voice_cache_dir


def test_with_no_engine_at_all_the_card_still_appears(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(PiperSynth, "available", lambda _self: False)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: False)

    chain = Synthesizer.from_config(voice_config)

    assert isinstance(chain.backend, StubSynth)
    assert chain.cache.directory == voice_config.paths.voice_cache_dir / STUB_CACHE_SUBDIR


def test_each_engine_caches_apart_from_the_others(voice_config: Config) -> None:
    """The cache key names the sentence and the voice, never the engine.

    So one shared directory would leave a family who installed Piper on Tuesday
    listening to Monday's robot - or worse, Monday's silence - for every line
    they have already heard, with `status` cheerfully reporting `piper`.
    """
    root = voice_config.paths.voice_cache_dir
    assert len({root, root / ESPEAK_CACHE_SUBDIR, root / STUB_CACHE_SUBDIR}) == 3


def test_espeak_is_asked_for_a_language_not_a_locale(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """espeak-ng names languages. `-v tr_TR` is an error, not a fallback."""
    seen: list[list[str]] = []

    class Finished:
        returncode = 0
        stdout = b"RIFF....WAVEfmt "
        stderr = b""

    def record(argv: list[str], **_kwargs: object) -> Finished:
        seen.append(list(argv))
        return Finished()

    monkeypatch.setattr(synth_module.subprocess, "run", record)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)

    EspeakSynth().render(
        "Beş dakika kaldı.",
        VoiceSpec(persona="zio", locale="tr_TR", profile_id="stock:zio:tr"),
        tmp_path / "out.wav",
    )

    argv = seen[0]
    assert "-v" in argv
    language, _, variant = argv[argv.index("-v") + 1].partition("+")
    assert language == "tr"
    assert variant, "a voice variant is part of the name, not a replacement for it"
    assert "tr_TR" not in argv


def test_zio_speaks_as_a_woman_and_sio_as_a_man(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Zio is the mother and Sio the father (D-85).

    espeak-ng has one Turkish voice, so its female and male variants and the
    pitch carry them apart. Until D-161 the pitches were the wrong way round
    and the mother spoke an octave below the father.
    """
    seen: dict[str, list[str]] = {}

    class Finished:
        returncode = 0
        stdout = b"RIFF....WAVEfmt "
        stderr = b""

    def record(argv: list[str], **_kwargs: object) -> Finished:
        seen[argv[argv.index("-v") + 1]] = list(argv)
        return Finished()

    monkeypatch.setattr(synth_module.subprocess, "run", record)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)

    for persona in ("zio", "sio"):
        EspeakSynth().render(
            "Merhaba.",
            VoiceSpec(persona=persona, locale="tr", profile_id=f"stock:{persona}:tr"),
            tmp_path / f"{persona}.wav",
        )

    zio = next(argv for name, argv in seen.items() if name.startswith("tr+f"))
    sio = next(argv for name, argv in seen.items() if name.startswith("tr+m"))
    assert int(zio[zio.index("-p") + 1]) > int(sio[sio.index("-p") + 1])


def test_espeak_speaks_more_slowly_than_its_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """espeak-ng's 175 words a minute is for adults reading along, not a four-year-old."""
    rates: list[int] = []

    class Finished:
        returncode = 0
        stdout = b"RIFF....WAVEfmt "
        stderr = b""

    def record(argv: list[str], **_kwargs: object) -> Finished:
        rates.append(int(argv[argv.index("-s") + 1]))
        return Finished()

    monkeypatch.setattr(synth_module.subprocess, "run", record)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)

    EspeakSynth().render(
        "Merhaba.",
        VoiceSpec(persona="zio", locale="tr", profile_id="stock:zio:tr"),
        tmp_path / "out.wav",
    )

    assert rates and rates[0] < 150


def test_espeak_writing_nothing_is_a_synth_error_not_an_empty_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty WAV plays as success. The chain has to see the failure."""

    class Finished:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(synth_module.subprocess, "run", lambda *_a, **_k: Finished())
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)

    with pytest.raises(SynthError, match="no audio"):
        EspeakSynth().render(
            "Merhaba.",
            VoiceSpec(persona="zio", locale="tr", profile_id="stock:zio:tr"),
            tmp_path / "out.wav",
        )


# ------------------------------------------- MBROLA, when a family adds it (D-161)


@pytest.fixture
def mbrola_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Debian's layout for both Turkish databases, and an `mbrola` on the PATH."""
    directory = tmp_path / "mbrola"
    for name in ("tr1", "tr2"):
        (directory / name).mkdir(parents=True)
        (directory / name / name).write_bytes(b"diphones")
    real_which = synth_module.which
    monkeypatch.setattr(
        synth_module,
        "which",
        lambda binary: "/usr/bin/mbrola" if binary == "mbrola" else real_which(binary),
    )
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)
    return directory


class FakePrograms:
    """espeak-ng working out the phonemes, then MBROLA writing its WAV."""

    #: What `espeak-ng -v mb-tr2 --pho` prints for "kaldı ... gel": every
    #: phoneme its tr1 table forgot is in here.
    PHO = (
        "; comment\nk\t111\na\t50\t 0 225 80 217\nL/\t78\nd\t78\n@\t60\n"
        "_\t215\nJ\t60\n&\t73\nl/\t78\n"
    )

    def __init__(self, *, mbrola_fails: bool = False) -> None:
        self.calls: list[tuple[list[str], bytes]] = []
        self.mbrola_fails = mbrola_fails

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(argv), kwargs["input"]))
        if argv[0] == "mbrola":
            if self.mbrola_fails:
                return subprocess.CompletedProcess(argv, 1, b"", b"Fatal error")
            Path(argv[-1]).write_bytes(b"RIFF" + b"\x00" * 200)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "--pho" in argv:
            return subprocess.CompletedProcess(argv, 0, self.PHO.encode(), b"")
        return subprocess.CompletedProcess(argv, 0, b"RIFF....WAVEfmt espeak", b"")


@pytest.mark.parametrize(("persona", "database"), [("zio", "tr2"), ("sio", "tr1")])
def test_mbrola_speaks_when_its_turkish_voices_are_installed(
    mbrola_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    persona: str,
    database: str,
) -> None:
    """tr2 is a woman and tr1 a man, so Zio is tr2 and Sio tr1 (D-85)."""
    programs = FakePrograms()
    monkeypatch.setattr(synth_module.subprocess, "run", programs)
    out = tmp_path / f"{persona}.wav"

    EspeakSynth(mbrola_dir=mbrola_dir).render(
        "Beş dakikan kaldı.",
        VoiceSpec(persona=persona, locale="tr_TR", profile_id=f"stock:{persona}:tr"),
        out,
    )

    (espeak, text), (mbrola, _pho) = programs.calls
    assert espeak[espeak.index("-v") + 1] == f"mb-{database}"
    assert "--pho" in espeak and "-q" in espeak, "espeak-ng only works out the phonemes"
    assert text == "Beş dakikan kaldı.".encode()
    assert mbrola[0] == "mbrola"
    assert str(mbrola_dir / database / database) in mbrola
    assert out.read_bytes().startswith(b"RIFF")


def test_the_phonemes_mbrola_does_not_know_are_renamed_not_dropped(
    mbrola_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """espeak-ng 1.52's Turkish uses three phonemes its own tr1 table lacks.

    Let espeak-ng call MBROLA itself and each becomes a gap: "kaldı" is said
    "ka-dı", in every warning there is. Found on the VM, by MBROLA's warnings.
    """
    programs = FakePrograms()
    monkeypatch.setattr(synth_module.subprocess, "run", programs)

    EspeakSynth(mbrola_dir=mbrola_dir).render(
        "Kaldı, gel.",
        VoiceSpec(persona="zio", locale="tr", profile_id="stock:zio:tr"),
        tmp_path / "out.wav",
    )

    pho = programs.calls[1][1].decode()
    phonemes = [line.split()[0] for line in pho.splitlines() if not line.startswith(";")]
    assert not {"L/", "l/", "&"} & set(phonemes)
    assert phonemes.count("l") == 2
    assert "e" in phonemes
    assert "a\t50\t 0 225 80 217" in pho, "timing and pitch are left alone"


def test_only_the_phoneme_field_of_a_pho_line_is_renamed() -> None:
    fixes = {"L/": "l"}
    assert translate_phonemes("L/\t78\t 0 100\n", fixes) == "l\t78\t 0 100\n"
    assert translate_phonemes("L/ 78\n", fixes) == "l 78\n"
    assert translate_phonemes("a\t50\tL/\n", fixes) == "a\t50\tL/\n"
    assert translate_phonemes("; L/\n", fixes) == "; L/\n"


def test_a_failing_mbrola_falls_back_to_espeaks_own_voice(
    mbrola_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken MBROLA is the robot, not silence, and the log says why."""
    programs = FakePrograms(mbrola_fails=True)
    monkeypatch.setattr(synth_module.subprocess, "run", programs)
    out = tmp_path / "out.wav"

    EspeakSynth(mbrola_dir=mbrola_dir).render(
        "Merhaba.", VoiceSpec(persona="zio", locale="tr", profile_id="stock:zio:tr"), out
    )

    last, _ = programs.calls[-1]
    assert last[last.index("-v") + 1] == "tr+f3"
    assert out.read_bytes().endswith(b"espeak")
    assert "using espeak-ng's own voice" in caplog.text


def test_english_is_never_sent_to_a_turkish_mbrola_voice(
    mbrola_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    programs = FakePrograms()
    monkeypatch.setattr(synth_module.subprocess, "run", programs)

    EspeakSynth(mbrola_dir=mbrola_dir).render(
        "Five minutes left.",
        VoiceSpec(persona="zio", locale="en", profile_id="stock:zio:en"),
        tmp_path / "out.wav",
    )

    ((argv, _),) = programs.calls
    assert argv[argv.index("-v") + 1] == "en+f3"


def test_mbrola_voices_without_the_mbrola_program_are_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """espeak-ng lists every MBROLA voice there is, installed or not: no guide."""
    directory = tmp_path / "mbrola"
    (directory / "tr2").mkdir(parents=True)
    (directory / "tr2" / "tr2").write_bytes(b"diphones")
    monkeypatch.setattr(synth_module, "which", lambda _binary: None)

    assert find_mbrola_voices("mbrola", directory, EspeakSynth.MBROLA) == {}


def test_a_database_installed_by_hand_is_found_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "mbrola"
    directory.mkdir()
    (directory / "tr2").write_bytes(b"diphones")
    monkeypatch.setattr(synth_module, "which", lambda binary: f"/usr/bin/{binary}")

    found = find_mbrola_voices("mbrola", directory, EspeakSynth.MBROLA)

    assert found == {("tr", "zio"): directory / "tr2"}


def test_a_cache_made_another_way_is_emptied(tmp_path: Path) -> None:
    """The key is the sentence and the voice; how it was made has to live beside it."""
    cache = VoiceCache(tmp_path / "espeak", max_bytes=10**6)
    cache.directory.mkdir()
    robot = cache.path_for("a")
    robot.write_bytes(b"robot")

    cache.made_by("espeak-ng tuning 2; mbrola: zio=tr2")
    assert not robot.exists(), "a cache from before the record is emptied"

    kept = cache.path_for("b")
    kept.write_bytes(b"voice")
    cache.made_by("espeak-ng tuning 2; mbrola: zio=tr2")
    assert kept.exists(), "the same maker keeps its audio"

    cache.made_by("espeak-ng tuning 2; mbrola: none")
    assert not kept.exists()


def test_installing_mbrola_means_the_robot_is_not_heard_again(
    voice_config: Config, mbrola_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every line a child hears has been heard before. Without this, all of them."""
    monkeypatch.setattr(PiperSynth, "available", lambda _self: False)
    monkeypatch.setattr(synth_module, "MBROLA_DIR", tmp_path / "not-installed")

    robot = Synthesizer.from_config(voice_config).cache.path_for("warn_5")
    robot.write_bytes(b"robot")
    Synthesizer.from_config(voice_config)
    assert robot.exists(), "nothing changed, so nothing is thrown away"

    monkeypatch.setattr(synth_module, "MBROLA_DIR", mbrola_dir)
    chain = Synthesizer.from_config(voice_config)

    assert not robot.exists()
    assert chain.cache.directory == robot.parent


def test_a_piper_with_no_model_hands_over_to_espeak(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain has to be a chain, not a choice made once at construction.

    `which("piper")` says a binary exists, not that it can speak. BioKids ships
    no Piper model - Phase 0 bundles none, and the one Turkish voice is
    CC-BY-NC-SA - so a parent who reads the package description and installs
    piper got `SynthError("no voice model")` for every line, for ever, while
    espeak-ng sat installed and untried. The only trace was two lines in the
    child's own journal, and `status` still reported `backend: "piper"`.
    """

    class Finished:
        returncode = 0
        stdout = b"RIFF....WAVEfmt " + b"\x00" * 64
        stderr = b""

    monkeypatch.setattr(PiperSynth, "available", lambda _self: True)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)
    monkeypatch.setattr(synth_module.subprocess, "run", lambda *_a, **_k: Finished())

    chain = Synthesizer.from_config(voice_config)
    assert len(chain.engines) == 2, "espeak must be in the chain, not replaced by piper"

    # No model on disk, which is every Phase 0 machine.
    stock = VoiceSpec(persona="zio", locale="tr", kind="stock", profile_id="stock:zio:tr")
    result = chain.render("Beş dakika kaldı.", [stock])

    assert result.spoken, "the machine went silent with a working engine installed"
    assert result.degraded is True, "a silent downgrade is the bug"
    assert chain.backend.name == "espeak-ng", (
        f"status would report {chain.backend.name!r} while espeak did the speaking"
    )


def test_status_reports_the_engine_that_actually_spoke(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It reported the engine that had been selected, which is not the same thing."""
    monkeypatch.setattr(PiperSynth, "available", lambda _self: True)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)

    chain = Synthesizer.from_config(voice_config)
    assert chain.backend.name == "piper", "before anything is spoken, the preferred one"

    class Finished:
        returncode = 0
        stdout = b"RIFF....WAVEfmt " + b"\x00" * 64
        stderr = b""

    monkeypatch.setattr(synth_module.subprocess, "run", lambda *_a, **_k: Finished())
    chain.render(
        "Merhaba.",
        [VoiceSpec(persona="zio", locale="tr", kind="stock", profile_id="stock:zio:tr")],
    )
    assert chain.backend.name == "espeak-ng"


def test_a_cached_line_is_found_whichever_engine_made_it(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each engine has its own cache directory, so the lookup has to try all of them."""

    class Finished:
        returncode = 0
        stdout = b"RIFF....WAVEfmt " + b"\x00" * 64
        stderr = b""

    monkeypatch.setattr(PiperSynth, "available", lambda _self: True)
    monkeypatch.setattr(EspeakSynth, "available", lambda _self: True)
    monkeypatch.setattr(synth_module.subprocess, "run", lambda *_a, **_k: Finished())

    chain = Synthesizer.from_config(voice_config)
    stock = VoiceSpec(persona="zio", locale="tr", kind="stock", profile_id="stock:zio:tr")

    first = chain.render("Bir dakika kaldı.", [stock])
    second = chain.render("Bir dakika kaldı.", [stock])

    assert first.spoken and second.spoken
    assert second.cached is True, "espeak's cache entry was not found on the second ask"
    assert second.path == first.path


# ------------------------------------------- a child may not look in /var/lib (D-160)


def test_a_voice_directory_the_child_may_not_read_is_skipped_not_fatal(
    voice_config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The family's data directory is 0700 root and the service runs as the child.

    Python 3.13's `is_dir()` raises PermissionError there rather than answering
    no. On the first real machine that crashed every line before espeak-ng was
    tried, so a warning drew its card and said nothing at all.
    """
    from pathlib import Path

    real_is_file = Path.is_file
    models = voice_config.paths.model_dir

    def is_file(self: Path, *args: object, **kwargs: object) -> bool:
        if models in self.parents:
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)

    voices = VoiceBank(voice_config).candidates("zio", "tr")

    assert all(voice.model_path is None for voice in voices)
    assert "cannot be read by this user" in caplog.text


def test_a_long_answer_is_cut_after_a_whole_sentence() -> None:
    from biokids_voice.service import whole_sentences

    text = "Birinci cümle. İkinci cümle uzun! Üçüncü cümle burada bitmiyor"
    assert whole_sentences(text, 200) == text
    assert whole_sentences(text, 40) == "Birinci cümle. İkinci cümle uzun!"
    assert whole_sentences("kelimekelimekelime", 5) == "kelim"


# ------------------------------------------------- D-174 a parent's own voice


async def test_the_recording_is_played_instead_of_a_synthesiser(config: Config) -> None:
    from biokids_core import recorded_voice
    from test_recorded_voice import a_wav

    directory = config.paths.recorded_voice_dir
    recorded_voice.save(directory, "tr", "warn_5", a_wav(1.0), "Beş dakikan kaldı canım.")
    recorded_voice.set_persona(directory, "sio")
    player = NullPlayer()
    service = build_service(config, player=player)
    async with Recorder(service) as recorder:
        await service.start()
        try:
            reply = await service.handle(speak("warn_5", vars={"minutes": 5}))
            assert reply.data["recorded"] is True
            assert reply.data["text"] == "Beş dakikan kaldı canım."
            assert reply.data["persona"] == "sio"
            assert await recorder.wait_for(str(reply.data["id"])) == STATUS_SPOKEN
        finally:
            await service.aclose()
    assert player.played == [directory / "tr" / "warn_5.wav"]
