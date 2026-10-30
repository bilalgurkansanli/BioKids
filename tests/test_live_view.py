"""The child's screen, live, for a parent - and the child knows (D-253)."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from biokids_daemon.live import KEEPALIVE_S, STALE_FRAME_S, LiveView
from biokids_shell.live import LiveSender

JPEG = base64.b64encode(b"\xff\xd8\xff\xe0" + b"x" * 100).decode()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_watch_starts_announced_and_keeps_only_the_latest_picture() -> None:
    clock = Clock()
    live = LiveView(clock)
    watch, announce = live.ask(1, "Anne")
    assert announce and watch.by == "Anne" and watch.frame == ""
    assert live.push(1, JPEG)
    assert live.watching(1) is not None and live.watching(1).frame == JPEG  # type: ignore[union-attr]
    clock.now += 1
    _watch, announce = live.ask(1, "Anne")
    assert not announce, "the launcher is sending: no need to tell it again"
    assert not live.push(2, JPEG), "a child nobody watches takes no pictures"


def test_a_picture_that_is_not_a_jpeg_is_dropped_and_the_watch_goes_on() -> None:
    live = LiveView(Clock())
    live.ask(1, "Baba")
    assert live.push(1, base64.b64encode(b"GIF89a....").decode())
    assert live.push(1, "not base64 at all!")
    assert live.watching(1).frame == ""  # type: ignore[union-attr]


def test_a_watch_nobody_asks_about_ends_and_its_picture_goes() -> None:
    clock = Clock()
    live = LiveView(clock)
    live.ask(1, "Anne")
    live.push(1, JPEG)
    clock.now += KEEPALIVE_S + 1
    assert live.watching(1) is None
    assert not live.push(1, JPEG), "the launcher stops at its next picture"
    assert live.expire() == [1]
    assert live.expire() == []
    _watch, announce = live.ask(1, "Anne")
    assert announce, "a new watch is announced again"


def test_a_quiet_launcher_is_told_again() -> None:
    clock = Clock()
    live = LiveView(clock)
    live.ask(1, "Anne")
    clock.now += STALE_FRAME_S + 1
    _watch, announce = live.ask(1, "Anne")
    assert announce, "a launcher that restarted missed the first word"


def test_stop_ends_a_watch_at_once() -> None:
    live = LiveView(Clock())
    live.ask(3, "Anne")
    assert live.stop(3)
    assert live.watching(3) is None
    assert not live.stop(3)


def test_the_launcher_sends_pictures_until_nobody_watches() -> None:
    sent: list[str] = []
    answers = iter([True, True, False])

    async def send(frame: str) -> bool:
        sent.append(frame)
        return next(answers)

    async def run() -> None:
        sender = LiveSender(send, take=lambda: b"\xff\xd8\xff picture", every=0.001)
        sender.start()
        sender.start()  # one task at a time
        for _ in range(100):
            await asyncio.sleep(0.005)
            if not sender.running:
                break
        assert not sender.running

    asyncio.run(run())
    assert len(sent) == 3
    assert base64.b64decode(sent[0]).startswith(b"\xff\xd8\xff")


def test_a_screen_that_cannot_be_taken_gives_up_quietly() -> None:
    sent: list[Any] = []

    async def send(frame: str) -> bool:
        sent.append(frame)
        return True

    async def run() -> None:
        sender = LiveSender(send, take=lambda: None, every=0.001)
        sender.start()
        for _ in range(100):
            await asyncio.sleep(0.005)
            if not sender.running:
                break
        assert not sender.running

    asyncio.run(run())
    assert sent == []
