"""A child's app windows: filling the screen, with one big close button (D-163).

Until D-163 a child's windows had no buttons at all and a maximised window had
no title bar either, so an app that filled the screen could only be left
through its own menus. Stellarium starts full screen and hides its menus; a
six-year-old could not find the way out, and on the first try neither could the
owner. Two halves put that right, and the launcher runs both at the start of
every session:

* `apply_window_manager_settings` sets the window manager's keys live, through
  xfconf: the BioKids theme with its 64-pixel title bar, a layout with a single
  button - close - on the right, and a title bar on maximised windows too. Live
  rather than only in the `xfwm4.xml` that provisioning copies into a new
  child's home, because provisioning never overwrites a file that is already
  there: every existing child would have kept the buttonless layout for ever,
  and a session that was open during the upgrade would have written its old
  settings back over the new file at logout.
* `WindowRules` keeps devilspie2 running with BioKids' one rule: maximise every
  ordinary window that is not the launcher. So the close button is always in
  the same place, the top right corner of the screen, whichever app is open.
  devilspie2 is GPL-3 and runs as a separate process, never imported (D-30).

Apps that would start full screen - which hides the title bar and its button -
are started windowed instead, by their catalog entries.

Since D-197 the close button is BioKids' own: `AppBar` keeps the app bar
running (`biokids_shell.appbar`) - home, the app's volume, and a close that
stops an app which will not close - at the screen's top right, over whichever
app is in front. While the bar is up the title bar has no button, so there is
one way out and not two; whenever it is not, the title bar's close button is
back. A child is never left without one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Final

from biokids_shell.appbar import EXIT_UNAVAILABLE, MODULE, READY

log = logging.getLogger(__name__)

THEME_NAME: Final = "BioKids"

BUTTON_LAYOUT: Final = "/general/button_layout"
CLOSE_BUTTON_LAYOUT: Final = "|C"
"""Title on the left, then the close button; no other button anywhere. The
layout every session starts with, and the one it falls back to (D-197)."""
NO_BUTTONS_LAYOUT: Final = "|"
"""The title alone, while the app bar carries the close button."""

#: (property, xfconf type, value) in the `xfwm4` channel. The same values as
#: `xfce/xfwm4.xml`, which a test keeps in step with this table.
WINDOW_MANAGER_SETTINGS: Final[tuple[tuple[str, str, str], ...]] = (
    ("/general/theme", "string", THEME_NAME),
    (BUTTON_LAYOUT, "string", CLOSE_BUTTON_LAYOUT),
    # The title bar is where the close button is when the app bar is not, so
    # a maximised window keeps it; and with the bar it is the strip the bar
    # sits in, so the bar covers nothing of the app. Borders still go: they
    # are of no use to a child.
    ("/general/titleless_maximize", "bool", "false"),
    ("/general/borderless_maximize", "bool", "true"),
    ("/general/title_font", "string", "Sans Bold 16"),
    ("/general/title_alignment", "string", "center"),
    # A window that cannot fill the screen - Frozen Bubble's fixed 640x480 -
    # opens in the middle of it, whatever its size, rather than in a corner
    # the window manager picked (D-197).
    ("/general/placement_ratio", "int", "100"),
    ("/general/placement_mode", "string", "center"),
)

RULES_SUBDIR: Final = "devilspie2"
RESTART_SECONDS: Final = 5.0
RESTART_BACKOFF_MAX: Final = 60.0
COMMAND_TIMEOUT_S: Final = 5.0

Runner = Callable[[Sequence[str]], Awaitable[bool]]


async def run_quietly(argv: Sequence[str]) -> bool:
    """Run one short command; True if it exited 0."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(process.communicate(), timeout=COMMAND_TIMEOUT_S)
    except (OSError, TimeoutError) as exc:
        log.warning("%s could not be run: %s", argv[0], exc)
        return False
    if process.returncode != 0:
        log.warning(
            "%s exited %s: %s",
            " ".join(argv[:5]),
            process.returncode,
            err.decode("utf-8", "replace").strip()[:200],
        )
        return False
    return True


async def apply_window_manager_settings(
    run: Runner = run_quietly, *, xfconf_query: str | None = None
) -> int:
    """Set the BioKids window manager keys in this session. Returns how many took.

    Harmless to repeat, and repeated at every login on purpose: it is what
    reaches a child whose home already had an older `xfwm4.xml`.
    """
    program = xfconf_query or shutil.which("xfconf-query")
    if program is None:
        log.warning("xfconf-query is not installed; app windows keep the old window settings")
        return 0
    applied = 0
    for prop, kind, value in WINDOW_MANAGER_SETTINGS:
        # `--create` with `--type` sets the key whether or not it exists yet.
        if await run([program, "-c", "xfwm4", "-p", prop, "--create", "-t", kind, "-s", value]):
            applied += 1
    if applied < len(WINDOW_MANAGER_SETTINGS):
        log.warning(
            "only %d of %d window manager settings could be applied",
            applied,
            len(WINDOW_MANAGER_SETTINGS),
        )
    return applied


class WindowRules:
    """devilspie2, with BioKids' rule, for as long as the launcher runs."""

    def __init__(self, rules_dir: Path, *, binary: str = "devilspie2") -> None:
        self.rules_dir = rules_dir
        self.binary = binary
        self.process: asyncio.subprocess.Process | None = None
        self.restarts = 0
        self._stopping = False

    def argv(self) -> list[str] | None:
        program = shutil.which(self.binary)
        if program is None:
            return None
        return [program, "--folder", str(self.rules_dir)]

    async def start(self) -> bool:
        argv = self.argv()
        if argv is None:
            log.warning(
                "%s is not installed: app windows will not fill the screen, and the "
                "close button will not always be in the same corner",
                self.binary,
            )
            return False
        if not self.rules_dir.is_dir():
            log.warning("%s is missing; is biokids-shell installed?", self.rules_dir)
            return False
        try:
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("%s could not be started: %s", self.binary, exc)
            return False
        return True

    async def supervise(self, stop_event: asyncio.Event) -> None:
        """Restart devilspie2 whenever it exits, until asked to stop."""
        backoff = RESTART_SECONDS
        while not stop_event.is_set():
            if self.process is None and not await self.start():
                return
            if self.process is None:  # pragma: no cover - start() returned True
                return
            await self.process.wait()
            if stop_event.is_set() or self._stopping:
                return
            self.restarts += 1
            log.warning("%s exited; restarting (%s)", self.binary, self.restarts)
            self.process = None
            await asyncio.sleep(backoff)
            backoff = min(RESTART_BACKOFF_MAX, backoff * 1.5)

    async def stop(self) -> None:
        self._stopping = True
        process = self.process
        self.process = None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=COMMAND_TIMEOUT_S)
        except TimeoutError:  # pragma: no cover - a wedged helper
            process.kill()


# ------------------------------------------------------------- the app bar

APPBAR_STABLE_SECONDS: Final = 60.0
"""A bar that ran this long before it stopped is restarted at once, not after a
backoff that grew while it was crash-looping an hour ago."""

Spawn = Callable[..., Awaitable[Any]]


async def set_button_layout(
    layout: str, run: Runner = run_quietly, *, xfconf_query: str | None = None
) -> bool:
    """Which buttons the title bar has, in this session, now."""
    program = xfconf_query or shutil.which("xfconf-query")
    if program is None:
        return False
    return await run(
        [program, "-c", "xfwm4", "-p", BUTTON_LAYOUT, "--create", "-t", "string", "-s", layout]
    )


class AppBar:
    """The app bar (D-197), and the title bar's close button standing in for it.

    The order is the safety property: the title bar loses its close button only
    after the bar has said it is on screen (`READY`), and gets it back the
    moment the bar's process is gone - crashed, killed, or never started for
    want of GTK. Every session also starts with the button
    (`apply_window_manager_settings`), so a layout left behind by a launcher
    that was killed outright lasts only until the next login.
    """

    def __init__(
        self,
        *,
        python: str = sys.executable,
        run: Runner = run_quietly,
        xfconf_query: str | None = None,
        spawn: Spawn | None = None,
    ) -> None:
        self.python = python
        self.run = run
        self.xfconf_query = xfconf_query
        self.spawn: Spawn = spawn or asyncio.create_subprocess_exec
        self.process: Any = None
        self.ready = False
        self.restarts = 0
        self._started = False
        self._stopping = False

    def argv(self) -> list[str]:
        return [self.python, "-m", MODULE]

    async def _layout(self, layout: str) -> None:
        await set_button_layout(layout, self.run, xfconf_query=self.xfconf_query)

    async def run_once(self) -> int | None:
        """Run the bar until it exits. Its exit code; None if it would not start."""
        try:
            process = await self.spawn(
                *self.argv(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                # Its log goes where the launcher's goes: the journal.
                stderr=None,
            )
        except OSError as exc:
            log.warning("the app bar could not be started: %s", exc)
            return None
        self.process = process
        self._started = True
        try:
            if await self._announced(process):
                self.ready = True
                await self._layout(NO_BUTTONS_LAYOUT)
            if process.stdout is not None:
                await process.stdout.read()
            code: int | None = await process.wait()
        finally:
            self.ready = False
            await self._layout(CLOSE_BUTTON_LAYOUT)
        return code

    @staticmethod
    async def _announced(process: Any) -> bool:
        """Read the bar's output until it says it is ready, or ends."""
        if process.stdout is None:
            return False
        while True:
            line = await process.stdout.readline()
            if not line:
                return False
            if line.strip() == READY.encode():
                return True

    async def supervise(self, stop_event: asyncio.Event) -> None:
        """Keep the bar running for as long as the launcher runs."""
        loop = asyncio.get_running_loop()
        backoff = RESTART_SECONDS
        while not (stop_event.is_set() or self._stopping):
            started = loop.time()
            code = await self.run_once()
            if stop_event.is_set() or self._stopping:
                return
            if code is None or code == EXIT_UNAVAILABLE:
                log.warning(
                    "the app bar cannot run in this session; every app keeps the "
                    "title bar's close button"
                )
                return
            self.restarts += 1
            if loop.time() - started >= APPBAR_STABLE_SECONDS:
                backoff = RESTART_SECONDS
            log.warning(
                "the app bar exited (%s); the title bar has the close button until it "
                "is back (restart %s)",
                code,
                self.restarts,
            )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            backoff = min(RESTART_BACKOFF_MAX, backoff * 1.5)

    async def stop(self) -> None:
        self._stopping = True
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=COMMAND_TIMEOUT_S)
            except TimeoutError:  # pragma: no cover - a wedged bar
                process.kill()
        if self._started:
            await self._layout(CLOSE_BUTTON_LAYOUT)
