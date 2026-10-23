"""D-228: the app in front is told to the daemon while it stays in front.

Until D-228 the launcher reported the tap that opened an app and nothing after,
and the daemon trusts a report for ninety seconds: an hour of a game was ninety
seconds of games, so no limit on a kind of app ever came and a learning app
never earned anything. These pin the launcher's half - which window is in
front, which tile its scope is, and that the report says it again every
FOCUS_REPORT_S. The daemon's half, looking the kind up and stopping the app,
is in test_daemon.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from biokids_core.config import Config
from biokids_daemon.base import ACTIVITY_TTL_SECONDS
from biokids_shell import launcher as launcher_module
from biokids_shell.focus import FOCUS_REPORT_S, FocusReporter, Front, front_pid
from biokids_shell.launcher import AppLauncher

KIOSK_PID = 100
"""The launcher's own window: in biokids-shell.service, no app scope."""
SCOPES = {
    200: "biokids-app-tuxpaint-1.scope",
    300: "biokids-app-eba-2.scope",
    400: "biokids-app-firefox-9.scope",
}


class _X:
    """xdotool, answering `getactivewindow getwindowpid` with `pid`."""

    def __init__(self, pid: int | None) -> None:
        self.pid = pid
        self.asked: list[list[str]] = []

    async def __call__(self, args: Sequence[str]) -> str | None:
        self.asked.append(list(args))
        return None if self.pid is None else f"{self.pid}"


def _scope_of(pid: int, uid: int | None) -> str | None:
    return SCOPES.get(pid)


class _Reports:
    def __init__(self, enough: int = 1) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.enough = enough
        self.done = asyncio.Event()
        """Set once `enough` reports have come."""

    async def __call__(self, app_slug: str, app_name: str, unit: str) -> None:
        self.calls.append((app_slug, app_name, unit))
        if len(self.calls) >= self.enough:
            self.done.set()


@pytest.fixture
def launcher(config: Config) -> AppLauncher:
    launcher = AppLauncher(config)
    launcher._remember("tuxpaint", "biokids-app-tuxpaint-1")
    launcher._remember_browser("eba", "biokids-app-eba-2")
    return launcher


def _reporter(launcher: AppLauncher, x: _X, reports: _Reports) -> FocusReporter:
    return FocusReporter(launcher, reports, run=x, scope_of=_scope_of, uid=1001, every=0.01)


def test_reports_come_well_inside_what_the_daemon_trusts() -> None:
    """Two lost in a row - a busy machine, a daemon restarting - and the app
    is still itself: a learning streak survives them."""
    assert FOCUS_REPORT_S * 3 <= ACTIVITY_TTL_SECONDS


@pytest.mark.parametrize(
    ("out", "pid"),
    [("4242\n", 4242), ("4242", 4242), ("", None), (None, None), ("abc", None), ("0", None)],
)
async def test_the_pid_of_the_active_window(out: str | None, pid: int | None) -> None:
    async def run(args: Sequence[str]) -> str | None:
        assert list(args) == ["getactivewindow", "getwindowpid"]
        return out

    assert await front_pid(run) == pid


async def test_an_app_tile_in_front_is_reported_with_its_scope(launcher: AppLauncher) -> None:
    reports = _Reports()
    found = await _reporter(launcher, _X(200), reports).report_once()
    assert found == Front("tuxpaint", "biokids-app-tuxpaint-1.scope")
    # The tile and the scope; its kind is the daemon's to look up.
    assert reports.calls == [("tuxpaint", "", "biokids-app-tuxpaint-1.scope")]


async def test_a_browser_window_is_the_web_tile_tapped_last(launcher: AppLauncher) -> None:
    """One Chromium for every web tile: its scope is the first web tile's, the
    site in front the one tapped last."""
    launcher._remember_browser("scratch", "biokids-app-scratch-3")
    reports = _Reports()
    await _reporter(launcher, _X(300), reports).report_once()
    assert reports.calls == [("scratch", "", "biokids-app-eba-2.scope")]


@pytest.mark.parametrize(
    "pid",
    [KIOSK_PID, 400, None],
    ids=["the launcher itself", "a scope the launcher did not start", "X cannot say"],
)
async def test_nothing_is_reported_for_anything_else(
    launcher: AppLauncher, pid: int | None
) -> None:
    """The last report runs out on its own; the English corner reports itself."""
    reports = _Reports()
    assert await _reporter(launcher, _X(pid), reports).report_once() is None
    assert reports.calls == []


async def test_the_report_is_said_again_while_the_app_stays_in_front(
    launcher: AppLauncher,
) -> None:
    reports = _Reports(enough=3)
    stop = asyncio.Event()
    task = asyncio.create_task(_reporter(launcher, _X(200), reports).run(stop))
    await asyncio.wait_for(reports.done.wait(), 1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert set(reports.calls) == {("tuxpaint", "", "biokids-app-tuxpaint-1.scope")}


async def test_one_bad_look_does_not_end_the_reporting(launcher: AppLauncher) -> None:
    reports = _Reports()
    looks = 0

    async def flaky(args: Sequence[str]) -> str | None:
        nonlocal looks
        looks += 1
        if looks == 1:
            raise RuntimeError("X went away for a moment")
        return "200"

    stop = asyncio.Event()
    reporter = FocusReporter(launcher, reports, run=flaky, scope_of=_scope_of, every=0.01)
    task = asyncio.create_task(reporter.run(stop))
    await asyncio.wait_for(reports.done.wait(), 1)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert reports.calls[0][0] == "tuxpaint"


# -------------------------------------------------------- AppLauncher.slug_of


def test_slug_of_knows_only_the_scopes_it_started(launcher: AppLauncher) -> None:
    assert launcher.slug_of("biokids-app-tuxpaint-1.scope") == "tuxpaint"
    assert launcher.slug_of("biokids-app-eba-2.scope") == "eba"
    assert launcher.slug_of("biokids-app-firefox-9.scope") is None
    assert launcher.slug_of("biokids-shell.service") is None


def test_the_browsers_scope_is_never_the_one_forgotten(
    launcher: AppLauncher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every web tap is a scope, and all but the browser's own end at once."""
    monkeypatch.setattr(launcher_module, "BROWSER_SCOPES_KEPT", 3)
    for n in range(10):
        launcher._remember_browser("scratch", f"biokids-app-scratch-{n + 10}")
    assert len(launcher.browser_scopes) == 3
    assert next(iter(launcher.browser_scopes)) == "biokids-app-eba-2.scope"
    assert launcher.slug_of("biokids-app-eba-2.scope") == "scratch"
