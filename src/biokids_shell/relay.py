"""What the daemon says, carried to the parts of the child's session that act on it.

Two jobs, on one subscription that lives as long as the launcher does - and a
third that rides along: the evening's dimming (D-214, `dimmer.py`), which
follows the ticks.

**Speech.** The daemon publishes SPEAK and never makes a sound itself: the voice
service runs inside the child's session, where the audio is, and the child
cannot reach the daemon's own socket (D-123). Something in the session has to
carry each line across, and nothing did. A warning drew a card and said
nothing, on every machine, until the first real run of the exit criterion
heard the silence (D-160).

**Bringing the launcher forward.** A warning drawn inside the launcher is drawn
behind whatever the child opened. At ten, five and one minute the launcher comes
to the front for a few seconds - Zio, the sentence, the ring - and then gives the
screen back to the app. The owner chose a short full-screen pause over a corner
card that an app could still cover (D-160).

One subscription here, not one per browser tab. Relaying from the page's own
event stream would speak once for every open tab, and not at all when none was
open.

The voice service's reply carries the sentence it resolved and the character it
chose. Both are kept here, so the page can show exactly what is being said
(VOICE.md section 7) rather than a catalog sentence that differs from it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from biokids_core.config import Config
from biokids_core.errors import IpcError
from biokids_core.ipc import AsyncDaemonClient, Event, EventKind
from biokids_shell.client import ShellClient
from biokids_shell.dimmer import BedtimeDimmer
from biokids_shell.kiosk import KIOSK_WINDOW_CLASS

log = logging.getLogger(__name__)

NOTICE_SECONDS = 7.0
"""How long a warning holds the screen. Long enough for the slowest spoken line
to finish; short enough that a child in the middle of a drawing is not kept
from it. The page reads the same number, so the two cannot drift."""

RECONNECT_SECONDS = 2.0
SPEAK_OP = "speak"
"""`biokids_voice.service.VoiceOp.SPEAK`. Spelled out rather than imported:
biokids-voice is a Recommends of biokids-shell, not a Depends, and the launcher
must start on a machine without it."""

SPEECH_FIELDS = ("event", "persona", "vars", "locale", "priority", "expression")
"""What the daemon's SPEAK event carries that the voice service wants."""

XdoRunner = Callable[[Sequence[str]], Awaitable[str | None]]


def voice_socket_path(config: Config) -> Path:
    """`$XDG_RUNTIME_DIR/biokids-voice.sock`, where the voice service listens."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / config.voice.socket_name
    getuid = getattr(os, "getuid", None)
    uid = getuid() if getuid is not None else 0
    return Path(f"/run/user/{uid}") / config.voice.socket_name


async def run_xdotool(args: Sequence[str]) -> str | None:
    """stdout of one xdotool call, or None if it could not be had."""
    program = shutil.which("xdotool")
    if program is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            program,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except (OSError, TimeoutError):
        return None
    if process.returncode != 0:
        return None
    return out.decode("utf-8", "replace").strip()


class WindowRaiser:
    """Put the launcher in front of the child's app, and give the screen back.

    Through xdotool, because the launcher is a Chromium window among the app's
    windows and only the window manager decides which is on top. The launcher is
    found by its WM_CLASS, which the kiosk sets with `--class`.
    """

    def __init__(self, run: XdoRunner = run_xdotool) -> None:
        self._run = run

    async def kiosk_window(self) -> str | None:
        found = await self._run(["search", "--onlyvisible", "--class", KIOSK_WINDOW_CLASS])
        windows = (found or "").split()
        return windows[-1] if windows else None

    async def bring_forward(self) -> str | None:
        """Raise the launcher. Returns the window to give the screen back to.

        None when there is nothing to give back: the launcher was already in
        front, or it could not be found - in which case the warning is still
        drawn and still spoken, only behind the app.
        """
        kiosk = await self.kiosk_window()
        if kiosk is None:
            log.warning("could not find the launcher window to bring forward")
            return None
        previous = await self._run(["getactivewindow"])
        if not previous or previous == kiosk:
            return None
        await self._run(["windowactivate", "--sync", kiosk])
        return previous

    async def give_back(self, previous: str | None) -> None:
        """Return the screen to the app the child was in. Gone is fine."""
        if previous:
            await self._run(["windowactivate", previous])


class SessionRelay:
    """Speech to the voice service, warnings to the screen."""

    def __init__(
        self,
        client: ShellClient,
        *,
        voice: AsyncDaemonClient | None = None,
        raiser: WindowRaiser | None = None,
        notice_seconds: float = NOTICE_SECONDS,
        on_spoken: Callable[[dict[str, Any]], None] | None = None,
        dimmer: BedtimeDimmer | None = None,
    ) -> None:
        self.client = client
        self.voice = voice
        self.raiser = raiser
        self.notice_seconds = notice_seconds
        self.on_spoken = on_spoken
        self.dimmer = dimmer
        self._pause: asyncio.Task[None] | None = None

    async def run(self, stop: asyncio.Event) -> None:
        """Follow the daemon for as long as the launcher runs, reconnecting as needed."""
        while not stop.is_set():
            try:
                async for event in self.client.events():
                    await self.handle(event)
                    if stop.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the daemon restarting is not an error here
                log.debug("event stream dropped: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), RECONNECT_SECONDS)
        if self._pause is not None:
            await self._pause

    async def handle(self, event: Event) -> None:
        if event.kind in (EventKind.WARNING, EventKind.PARENT_NOTE):
            # A parent's "well done" or note (D-233) comes in front of the app
            # for the same few seconds as a warning: it is for now.
            # A task, so that the SPEAK that follows straight after is relayed
            # now rather than when the pause is over.
            if self._pause is None or self._pause.done():
                self._pause = asyncio.create_task(self.pause(), name="warning-pause")
        elif event.kind == EventKind.SPEAK:
            await self.speak(event.payload)
        elif event.kind == EventKind.TICK and self.dimmer is not None:
            # The evening's last ten minutes dim the screen (D-214). A failure
            # costs the dimming, never the relay: the next warning matters more.
            try:
                await self.dimmer.on_tick(event.payload)
            except Exception:
                log.exception("the bedtime dimming failed")

    async def speak(self, payload: dict[str, Any]) -> None:
        if self.voice is None:
            return
        fields = {key: payload[key] for key in SPEECH_FIELDS if key in payload}
        try:
            response = await self.voice.call(SPEAK_OP, **fields)
        except (IpcError, OSError) as exc:
            log.warning(
                "could not reach the voice service at %s: %s; the line is shown, not said",
                self.voice.socket_path,
                exc,
            )
            return
        if not response.ok:
            log.warning("the voice service refused %s: %s", fields.get("event"), response.error)
            return
        if self.on_spoken is not None:
            card = dict(response.data)
            card["at"] = time.time()
            self.on_spoken(card)

    async def pause(self) -> None:
        """Hold the screen for the warning, then hand it back."""
        if self.raiser is None:
            return
        previous = await self.raiser.bring_forward()
        try:
            await asyncio.sleep(self.notice_seconds)
        finally:
            await self.raiser.give_back(previous)
