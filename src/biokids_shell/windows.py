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
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Final

log = logging.getLogger(__name__)

THEME_NAME: Final = "BioKids"

#: (property, xfconf type, value) in the `xfwm4` channel. The same values as
#: `xfce/xfwm4.xml`, which a test keeps in step with this table.
WINDOW_MANAGER_SETTINGS: Final[tuple[tuple[str, str, str], ...]] = (
    ("/general/theme", "string", THEME_NAME),
    # Title on the left, then the close button; no other button anywhere.
    ("/general/button_layout", "string", "|C"),
    # The title bar is the only place the close button can be, so a maximised
    # window keeps it. Borders still go: they are of no use to a child.
    ("/general/titleless_maximize", "bool", "false"),
    ("/general/borderless_maximize", "bool", "true"),
    ("/general/title_font", "string", "Sans Bold 16"),
    ("/general/title_alignment", "string", "center"),
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
