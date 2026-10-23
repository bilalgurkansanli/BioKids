"""Which app is in front, told to the daemon while it stays there (D-228).

The daemon trusts a report of the app in front for `ACTIVITY_TTL_SECONDS` (90).
Until D-228 the launcher reported only the tap that opened an app, so a game
played for an hour was a game for its first ninety seconds and "nothing in
particular" after: its kind of time was never used up, a learning app never
earned anything, and the history had no app for the rest of the hour. Only the
English corner, a page of the launcher's own, said it again.

So this asks the X server every `FOCUS_REPORT_S` which window is active, finds
the process behind it, and from that process's cgroup the `biokids-app-*.scope`
the launcher started it in (`ARCHITECTURE.md` §7) - the same way the app bar
knows which app it sits on. The scope gives the tile: an app tile by its own
scope, a browser window as the web tile tapped last (`AppLauncher.slug_of`).
The report names the tile and the scope, never a kind: the daemon looks that up
in the child's own apps, so nothing the child's session says decides which
limit applies.

When the launcher itself is in front, or the window is no app of this child's,
nothing is reported: the last report runs out on its own, and the English
corner's page keeps reporting itself (`english.js`). Reports are a convenience
for the limits on kinds of app, not the limit on the day, which the daemon
counts from logind whatever is in front; a reporter that cannot look costs the
kinds of app their limits, and never the child their screen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

from biokids_shell.appbar import own_uid, scope_of_pid
from biokids_shell.launcher import AppLauncher
from biokids_shell.relay import XdoRunner, run_xdotool

log = logging.getLogger(__name__)

FOCUS_REPORT_S: Final = 30.0
"""How often the app in front is reported. A third of the daemon's 90 seconds,
so two reports in a row can be lost - a busy machine, a daemon restarting -
before the app stops counting as itself, and a learning streak survives them."""

Report = Callable[[str, str, str], Awaitable[None]]
"""`ShellClient.report_activity(app_slug, app_name, unit)`."""
ScopeOf = Callable[[int, int | None], str | None]


@dataclass(frozen=True)
class Front:
    """The tile in front, and the scope its app runs in."""

    slug: str
    scope: str


async def front_pid(run: XdoRunner = run_xdotool) -> int | None:
    """The process of the active window, or None when X cannot say."""
    out = await run(["getactivewindow", "getwindowpid"])
    try:
        pid = int((out or "").split()[-1])
    except (IndexError, ValueError):
        return None
    return pid if pid > 0 else None


class FocusReporter:
    def __init__(
        self,
        launcher: AppLauncher,
        report: Report,
        *,
        run: XdoRunner = run_xdotool,
        scope_of: ScopeOf = scope_of_pid,
        uid: int | None = None,
        every: float = FOCUS_REPORT_S,
    ) -> None:
        self._launcher = launcher
        self._report = report
        self._run = run
        self._scope_of = scope_of
        self._uid = own_uid() if uid is None else uid
        self._every = every

    async def front(self) -> Front | None:
        """The child's app in front; None for the launcher, or anything else."""
        pid = await front_pid(self._run)
        if pid is None:
            return None
        scope = self._scope_of(pid, self._uid)
        if scope is None:
            return None
        slug = self._launcher.slug_of(scope)
        return None if slug is None else Front(slug, scope)

    async def report_once(self) -> Front | None:
        found = await self.front()
        if found is not None:
            await self._report(found.slug, "", found.scope)
        return found

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.report_once()
            except Exception:  # one bad look must not end the reporting
                log.exception("could not tell the daemon which app is in front")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._every)
