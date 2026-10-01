"""D-160: what the daemon says reaches the voice service and the screen.

Until the first real run of the exit criterion, a warning drew a card and said
nothing: the daemon published SPEAK and nothing in the child's session carried
it to the voice service. And the card was drawn in the launcher, behind
whatever app the child had open. These pin both halves of the fix.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from biokids_core.config import Config
from biokids_core.errors import IpcError
from biokids_core.ipc import Event, EventKind, Response
from biokids_shell.kiosk import KIOSK_WINDOW_CLASS
from biokids_shell.relay import (
    SPEAK_OP,
    SessionRelay,
    WindowRaiser,
    voice_socket_path,
)

WARN_10 = {
    "child_id": 1,
    "event": "warn_10",
    "locale": "tr",
    "vars": {"name": "Elif", "minutes": 10},
    "priority": "normal",
    "expression": "neutral",
    "persona": "auto",
}


class FakeEvents:
    """The daemon's event stream for one child, scripted."""

    def __init__(self, events: list[Event]) -> None:
        self.events = events

    async def events(self) -> AsyncIterator[Event]:  # pragma: no cover - not used directly
        for event in self.events:
            yield event


class FakeVoice:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.socket_path = Path("/run/user/1001/biokids-voice.sock")

    async def call(self, op: str, **args: Any) -> Response:
        self.calls.append((op, args))
        if self.fail:
            raise IpcError("no such file or directory")
        return Response(
            id="1",
            ok=True,
            data={
                "event": args["event"],
                "text": "On dakikan kaldı. İstersen yavaştan toparlanmaya başla.",
                "persona": "sio",
                "duration_ms": 3500,
            },
        )


class FakeRaiser:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def bring_forward(self) -> str | None:
        self.calls.append("forward")
        return "4242"

    async def give_back(self, previous: str | None) -> None:
        self.calls.append(f"back:{previous}")


def relay(**kwargs: Any) -> SessionRelay:
    return SessionRelay(FakeEvents([]), **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------------ speech


async def test_a_spoken_line_reaches_the_voice_service() -> None:
    """The daemon never speaks; this is the only way a warning becomes sound."""
    voice = FakeVoice()
    await relay(voice=voice).handle(Event(EventKind.SPEAK, payload=WARN_10))

    assert len(voice.calls) == 1
    op, args = voice.calls[0]
    assert op == SPEAK_OP
    assert args["event"] == "warn_10"
    assert args["vars"] == {"name": "Elif", "minutes": 10}
    assert args["locale"] == "tr"
    assert "child_id" not in args, "the voice service has no use for it"


async def test_the_words_actually_said_are_kept_for_the_page() -> None:
    """The card must show what is heard, and in the voice of who is heard."""
    kept: list[dict[str, Any]] = []
    await relay(voice=FakeVoice(), on_spoken=kept.append).handle(
        Event(EventKind.SPEAK, payload=WARN_10)
    )

    assert kept[0]["text"].startswith("On dakikan kaldı")
    assert kept[0]["persona"] == "sio"
    assert kept[0]["event"] == "warn_10"
    assert kept[0]["at"] > 0


async def test_a_missing_voice_service_is_not_an_error_for_the_launcher(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """biokids-voice is a Recommends: the launcher must carry on without it."""
    kept: list[dict[str, Any]] = []
    await relay(voice=FakeVoice(fail=True), on_spoken=kept.append).handle(
        Event(EventKind.SPEAK, payload=WARN_10)
    )

    assert kept == []
    assert "could not reach the voice service" in caplog.text


# ------------------------------------------------------------- the pause


async def test_a_warning_brings_the_launcher_forward_and_then_gives_the_screen_back() -> None:
    raiser = FakeRaiser()
    subject = relay(raiser=raiser, notice_seconds=0.01)

    await subject.handle(
        Event(EventKind.WARNING, payload={"minutes": 10, "remaining_seconds": 600})
    )
    assert subject._pause is not None
    await subject._pause

    assert raiser.calls == ["forward", "back:4242"]


async def test_the_spoken_line_is_not_held_up_by_the_pause() -> None:
    """WARNING and SPEAK arrive together; the voice must not wait seven seconds."""
    voice = FakeVoice()
    subject = relay(voice=voice, raiser=FakeRaiser(), notice_seconds=5.0)

    await subject.handle(Event(EventKind.WARNING, payload={"minutes": 10}))
    await subject.handle(Event(EventKind.SPEAK, payload=WARN_10))

    assert len(voice.calls) == 1
    assert subject._pause is not None and not subject._pause.done()
    subject._pause.cancel()


async def test_a_second_warning_during_a_pause_does_not_stack_another() -> None:
    raiser = FakeRaiser()
    subject = relay(raiser=raiser, notice_seconds=0.05)

    await subject.handle(Event(EventKind.WARNING, payload={"minutes": 5}))
    first = subject._pause
    await subject.handle(Event(EventKind.WARNING, payload={"minutes": 5}))

    assert subject._pause is first
    assert first is not None
    await first
    assert raiser.calls.count("forward") == 1


async def test_without_a_kiosk_there_is_nothing_to_bring_forward() -> None:
    """`--no-kiosk`, a developer's machine: the relay still speaks."""
    subject = relay(raiser=None, notice_seconds=0.01)
    await subject.handle(Event(EventKind.WARNING, payload={"minutes": 1}))
    assert subject._pause is not None
    await subject._pause


# --------------------------------------------------------- the window manager


class ScriptedXdo:
    def __init__(self, answers: dict[str, str | None]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, args: Sequence[str]) -> str | None:
        self.calls.append(tuple(args))
        return self.answers.get(args[0], "")


async def test_the_launcher_is_raised_over_the_app_and_the_app_gets_it_back() -> None:
    xdo = ScriptedXdo({"search": "77\n88", "getactivewindow": "4242"})
    raiser = WindowRaiser(xdo)

    previous = await raiser.bring_forward()
    await raiser.give_back(previous)

    assert ("search", "--onlyvisible", "--class", KIOSK_WINDOW_CLASS) in xdo.calls
    assert ("windowactivate", "--sync", "88") in xdo.calls
    assert xdo.calls[-1] == ("windowactivate", "4242")


async def test_a_launcher_already_in_front_is_left_alone() -> None:
    xdo = ScriptedXdo({"search": "88", "getactivewindow": "88"})
    raiser = WindowRaiser(xdo)

    assert await raiser.bring_forward() is None
    assert not any(call[0] == "windowactivate" for call in xdo.calls)


async def test_no_launcher_window_means_no_raise_and_no_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    xdo = ScriptedXdo({"search": None})
    assert await WindowRaiser(xdo).bring_forward() is None
    assert "could not find the launcher window" in caplog.text


# ------------------------------------------------------------------ plumbing


def test_the_voice_socket_is_in_the_childs_runtime_directory(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1001")
    assert voice_socket_path(config) == Path("/run/user/1001") / config.voice.socket_name


async def test_the_relay_stops_when_asked() -> None:
    class Endless:
        async def events(self) -> AsyncIterator[Event]:
            while True:
                await asyncio.sleep(3600)
                yield Event(EventKind.TICK, payload={})

    stop = asyncio.Event()
    subject = SessionRelay(Endless())  # type: ignore[arg-type]
    task = asyncio.create_task(subject.run(stop))
    await asyncio.sleep(0)
    stop.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
