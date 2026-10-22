"""D-208: opening an app is answered at once, and never starts a second copy.

The owner, on the VM: "whatever the app, it keeps me waiting on the 'Yapboz
açılıyor' screen for a very, very long time". The journal of that session had
three Palapelis running at once. These pin the launcher's half of the fix: taps
on one tile are taken one at a time; an app whose scope is running is not
started again because the app bar could not look in time; "still opening"
counts from the start of the copy being asked about; the running bar is asked
before a helper is run; and a launch is answered when systemd-run is seen to
have become the app, not a second and a half later - while an app that fails
in that second and a half is still noticed, and forgotten at once so that the
page sees it gone. The page's half is in test_shell.py, the bar's socket in
test_appbar_control.py.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from biokids_core.config import Config
from biokids_shell import launcher as launcher_module
from biokids_shell.appbar import EXIT_BAD_UNIT, EXIT_NOT_FOUND, control
from biokids_shell.launcher import AppLauncher, LaunchRequest

PUZZLE = LaunchRequest(slug="palapeli", name="Yapboz", kind="app", exec_cmd="palapeli")
RUNNER = "/usr/bin/systemd-run"


class _App:
    """`systemd-run --scope` that has become the app, and keeps running."""

    returncode: int | None = None
    stderr = None

    def __init__(self, pid: int) -> None:
        self.pid = pid

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


class _Starts:
    """`asyncio.create_subprocess_exec`, counting the copies started."""

    def __init__(self) -> None:
        self.argv: list[tuple[str, ...]] = []

    @property
    def count(self) -> int:
        return len(self.argv)

    async def __call__(self, *argv: str, **_kwargs: Any) -> Any:
        self.argv.append(argv)
        return _App(4000 + self.count)


@pytest.fixture
def starts(monkeypatch: pytest.MonkeyPatch) -> _Starts:
    started = _Starts()
    monkeypatch.setattr(shutil, "which", lambda name, **_kw: f"/usr/bin/{name}")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", started)
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 0.01)
    # Not the /proc of the machine running the tests: these pids are made up.
    monkeypatch.setattr(launcher_module, "process_exe", lambda _pid, _proc=None: None)
    return started


class _Session:
    """What the app bar and systemd say, and what the launcher asked them."""

    def __init__(
        self,
        launcher: AppLauncher,
        *,
        shown: int | None = EXIT_NOT_FOUND,
        active: bool | None = True,
        bar_delay: float = 0.0,
    ) -> None:
        self.shown, self.active, self.bar_delay = shown, active, bar_delay
        self.brought: list[str] = []
        self.stopped: list[str] = []

        async def bring_back(unit: str) -> int | None:
            self.brought.append(unit)
            await asyncio.sleep(self.bar_delay)
            return self.shown

        async def unit_state(_unit: str) -> bool | None:
            return self.active

        async def stop_unit(unit: str) -> bool:
            self.stopped.append(unit)
            return True

        launcher.bring_back = bring_back  # type: ignore[method-assign]
        launcher.unit_state = unit_state  # type: ignore[method-assign]
        launcher.stop_unit = stop_unit  # type: ignore[method-assign]


# ------------------------------------------------------- never two copies


async def test_taps_on_one_tile_at_once_start_one_copy(config: Config, starts: _Starts) -> None:
    """Three taps before the first was answered: one Palapeli, not three."""
    launcher = AppLauncher(config)
    session = _Session(launcher, shown=EXIT_NOT_FOUND, active=True)

    results = await asyncio.gather(*(launcher.launch(PUZZLE) for _ in range(3)))

    assert starts.count == 1, "a copy was started for every tap"
    assert all(result.ok for result in results)
    assert {result.unit for result in results} == {"biokids-app-palapeli-1"}
    assert session.stopped == []


async def test_the_owners_journal_does_not_happen_again(
    config: Config, starts: _Starts
) -> None:
    """14:13:47 palapeli-3, 14:13:50 palapeli-4, 14:13:53 palapeli-5: a game slow
    to show its window, tapped every three seconds, on a machine so busy the bar
    did not answer in time. Now: one copy, and the taps wait for its window."""
    launcher = AppLauncher(config)
    first = await launcher.launch(PUZZLE)
    session = _Session(launcher, shown=None, active=True)

    for _ in range(3):
        again = await launcher.launch(PUZZLE)
        assert again.ok and again.unit == first.unit

    assert starts.count == 1
    assert session.brought == [first.unit] * 3
    assert session.stopped == []


async def test_a_web_tile_opens_a_window_for_every_tap(
    config: Config, starts: _Starts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Web tiles open a window each time (D-197): no lock, nothing remembered."""
    monkeypatch.setattr(launcher_module, "prepare_browser_profile", lambda *_a: True)
    launcher = AppLauncher(config)
    site = LaunchRequest(slug="eba", name="EBA", kind="web", url="https://www.eba.gov.tr")

    await asyncio.gather(launcher.launch(site), launcher.launch(site))

    assert starts.count == 2
    assert launcher.running == {}


# ------------------------------------------------- still opening, this copy


async def test_still_opening_counts_from_the_copy_being_asked_about(
    config: Config, starts: _Starts
) -> None:
    """The first copy started long ago and was closed; the second has just
    started and has no window yet. It is the second's age that counts: it is
    left to open, not stopped as something left behind."""
    launcher = AppLauncher(config)
    first = await launcher.launch(PUZZLE)
    launcher.started[first.unit] -= launcher_module.STILL_OPENING_S + 60
    session = _Session(launcher, shown=EXIT_NOT_FOUND, active=False)
    second = await launcher.launch(PUZZLE)
    assert second.unit != first.unit
    assert first.unit not in launcher.started, "a forgotten copy's time was kept"

    session.active = True
    third = await launcher.launch(PUZZLE)

    assert third.unit == second.unit
    assert session.stopped == []
    assert starts.count == 2


async def test_a_copy_left_without_a_window_is_replaced_once(
    config: Config, starts: _Starts
) -> None:
    launcher = AppLauncher(config)
    first = await launcher.launch(PUZZLE)
    launcher.started[first.unit] -= launcher_module.STILL_OPENING_S + 1
    session = _Session(launcher, shown=EXIT_NOT_FOUND, active=True)

    second = await launcher.launch(PUZZLE)
    third = await launcher.launch(PUZZLE)

    assert session.stopped == [first.unit]
    assert second.unit == third.unit != first.unit
    assert starts.count == 2


def test_an_unknown_copy_is_old(config: Config) -> None:
    assert AppLauncher(config).age("biokids-app-palapeli-9") == float("inf")


def test_forgetting_an_old_copy_keeps_the_new_one(config: Config) -> None:
    launcher = AppLauncher(config)
    launcher.running["palapeli"] = "biokids-app-palapeli-2"
    launcher.started["biokids-app-palapeli-1"] = 1.0
    launcher.started["biokids-app-palapeli-2"] = 2.0

    launcher._forget("palapeli", "biokids-app-palapeli-1")

    assert launcher.running == {"palapeli": "biokids-app-palapeli-2"}
    assert launcher.started == {"biokids-app-palapeli-2": 2.0}


async def test_the_open_apps_list_keeps_a_copy_started_while_it_asked(
    config: Config,
) -> None:
    """systemctl answers about the copy it was asked about; a new one started
    meanwhile is the tile's app and stays."""
    launcher = AppLauncher(config)
    launcher.running["palapeli"] = "biokids-app-palapeli-1"
    launcher.started["biokids-app-palapeli-1"] = 1.0

    async def active_units(_units: Any) -> set[str]:
        launcher._remember("palapeli", "biokids-app-palapeli-2")
        return set()

    launcher.active_units = active_units  # type: ignore[method-assign]

    assert await launcher.open_apps() == []
    assert launcher.running == {"palapeli": "biokids-app-palapeli-2"}
    assert set(launcher.started) == {"biokids-app-palapeli-2"}


@pytest.mark.parametrize(
    ("code", "state"), [(0, True), (3, False), (4, False), (1, None), (None, None)]
)
async def test_systemd_saying_nothing_is_not_an_app_that_closed(
    config: Config, monkeypatch: pytest.MonkeyPatch, code: int | None, state: bool | None
) -> None:
    asked: list[list[str]] = []

    async def exit_code(argv: list[str], _within: float) -> int | None:
        asked.append(argv)
        return code

    monkeypatch.setattr(launcher_module, "_exit_code", exit_code)
    assert await AppLauncher(config).unit_state("biokids-app-palapeli-3") is state
    assert asked == [
        ["systemctl", "--user", "is-active", "--quiet", "biokids-app-palapeli-3.scope"]
    ]


# ------------------------------------------------------ asking the bar first


class _Bar:
    """`control.ask_bar`, as the running bar answers it, or no bar at all."""

    def __init__(self, answer: int | type[Exception] | None) -> None:
        self.answer = answer
        self.asked: list[tuple[Path | None, str, float]] = []

    async def __call__(self, path: Path | None, unit: str, within: float) -> int | None:
        self.asked.append((path, unit, within))
        if isinstance(self.answer, type):
            raise self.answer("no bar")
        return self.answer


@pytest.fixture
def helper(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    ran: list[list[str]] = []

    async def exit_code(argv: list[str], _within: float) -> int:
        ran.append(argv)
        return EXIT_NOT_FOUND

    monkeypatch.setattr(launcher_module, "_exit_code", exit_code)
    return ran


async def test_the_running_bar_is_asked_and_no_helper_is_run(
    config: Config, monkeypatch: pytest.MonkeyPatch, helper: list[list[str]], tmp_path: Path
) -> None:
    bar = _Bar(0)
    monkeypatch.setattr(control, "ask_bar", bar)
    launcher = AppLauncher(config, appbar_socket=tmp_path / "bar.sock")

    assert await launcher.bring_back("biokids-app-palapeli-3") == 0
    assert bar.asked == [
        (tmp_path / "bar.sock", "biokids-app-palapeli-3", launcher_module.BAR_ANSWER_TIMEOUT_S)
    ]
    assert launcher_module.BAR_ANSWER_TIMEOUT_S <= 1.5
    assert helper == [], "a new Python was started although the bar answered"


async def test_a_busy_bar_is_could_not_look_not_a_helper(
    config: Config, monkeypatch: pytest.MonkeyPatch, helper: list[list[str]]
) -> None:
    """A machine too busy for the bar to answer is the worst moment for a new
    Python that loads GTK from nothing."""
    monkeypatch.setattr(control, "ask_bar", _Bar(None))
    assert await AppLauncher(config).bring_back("biokids-app-palapeli-3") is None
    assert helper == []


async def test_without_a_bar_the_helper_is_run(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    helper: list[list[str]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(control, "ask_bar", _Bar(control.BarNotRunning))
    with caplog.at_level(logging.INFO, logger=launcher_module.__name__):
        assert await AppLauncher(config).bring_back("biokids-app-palapeli-3") == EXIT_NOT_FOUND
    assert helper == [
        [sys.executable, "-m", "biokids_shell.appbar", "--bring-back", "biokids-app-palapeli-3"]
    ]
    # In the journal at the shell's usual level: VM-RUNBOOK §8.10 reads it there.
    assert any(
        record.levelno == logging.INFO and "no app bar to ask" in record.getMessage()
        for record in caplog.records
    )


async def test_nothing_but_an_app_is_asked_for(
    config: Config, monkeypatch: pytest.MonkeyPatch, helper: list[list[str]]
) -> None:
    bar = _Bar(0)
    monkeypatch.setattr(control, "ask_bar", bar)
    assert await AppLauncher(config).bring_back("biokids-shell") == EXIT_BAD_UNIT
    assert bar.asked == [] and helper == []


# --------------------------------------------- answered when the exec is seen


class _Scope:
    """systemd-run setting the scope up, then the app - or refused."""

    def __init__(self, *, exits_with: int | None = None, after: float = 3600.0) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self._exits_with, self._after = exits_with, after
        self.stderr = _Stderr()

    async def wait(self) -> int:
        if self._exits_with is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self._after)
        self.returncode = self._exits_with
        return self.returncode if self.returncode is not None else 0


class _Stderr:
    async def read(self) -> bytes:
        return b"Failed to start transient scope unit"


def _exe_after(polls: int, app: str | None = "/usr/games/palapeli") -> Any:
    """`/proc/<pid>/exe`: systemd-run for `polls` looks, then `app`."""
    seen = [0]

    def exe(_pid: int, _proc: Path = Path("/proc")) -> str | None:
        seen[0] += 1
        return RUNNER if seen[0] <= polls else app

    exe.seen = seen  # type: ignore[attr-defined]
    return exe


@pytest.fixture
def scope_start(monkeypatch: pytest.MonkeyPatch) -> Any:
    def start(process: Any) -> None:
        async def spawn(*_argv: Any, **_kwargs: Any) -> Any:
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    monkeypatch.setattr(shutil, "which", lambda name, **_kw: f"/usr/bin/{name}")
    monkeypatch.setattr(launcher_module, "_resolved", lambda program: program)
    return start


async def test_a_launch_is_answered_when_systemd_run_has_become_the_app(
    config: Config, monkeypatch: pytest.MonkeyPatch, scope_start: Any
) -> None:
    """No longer the whole 1.5 s on every tap: the exec is seen, and after it
    nothing can be refused."""
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 30.0)
    exe = _exe_after(3)
    monkeypatch.setattr(launcher_module, "process_exe", exe)
    scope_start(_Scope())
    loop = asyncio.get_running_loop()
    began = loop.time()

    result = await asyncio.wait_for(AppLauncher(config).launch(PUZZLE), timeout=5)

    assert result.ok and result.unit == "biokids-app-palapeli-1"
    assert loop.time() - began < 2.0
    assert exe.seen[0] == 4


async def test_a_refusal_is_still_seen(
    config: Config, monkeypatch: pytest.MonkeyPatch, scope_start: Any
) -> None:
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 30.0)
    monkeypatch.setattr(launcher_module, "process_exe", _exe_after(10**6))
    scope_start(_Scope(exits_with=1, after=0.12))
    launcher = AppLauncher(config)

    result = await asyncio.wait_for(launcher.launch(PUZZLE), timeout=5)

    assert not result.ok
    assert launcher.running == {}


async def test_an_app_that_hands_over_and_exits_is_launched(
    config: Config, monkeypatch: pytest.MonkeyPatch, scope_start: Any
) -> None:
    """A KDE app passing the tap to a copy already running exits 0 at once."""
    monkeypatch.setattr(launcher_module, "process_exe", _exe_after(10**6))
    scope_start(_Scope(exits_with=0, after=0.05))
    assert (await asyncio.wait_for(AppLauncher(config).launch(PUZZLE), timeout=5)).ok


async def test_where_the_exec_cannot_be_seen_the_whole_wait_it_is(
    config: Config, monkeypatch: pytest.MonkeyPatch, scope_start: Any
) -> None:
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(launcher_module, "process_exe", lambda _pid, _proc=None: None)
    scope_start(_Scope())
    loop = asyncio.get_running_loop()
    began = loop.time()

    result = await asyncio.wait_for(AppLauncher(config).launch(PUZZLE), timeout=5)

    assert result.ok
    assert loop.time() - began >= 0.29


# ------------------------------------------- an app that ends as it starts


class _EndsBySelf:
    """systemd-run that has become the app, which ends by itself `after` seconds
    after it was started - however often, and from whenever, it is waited for."""

    def __init__(self, code: int, after: float) -> None:
        self.pid = 4242
        self.returncode: int | None = None
        self.code = code
        self.ends_at = asyncio.get_running_loop().time() + after
        self.stderr = _Says(b"palapeli: error while loading shared libraries")

    async def wait(self) -> int:
        await asyncio.sleep(max(0.0, self.ends_at - asyncio.get_running_loop().time()))
        self.returncode = self.code
        return self.code


class _Says:
    def __init__(self, text: bytes) -> None:
        self.text = text

    async def read(self) -> bytes:
        return self.text


async def _launched_then_watched(
    config: Config, monkeypatch: pytest.MonkeyPatch, scope_start: Any, app: _EndsBySelf
) -> AppLauncher:
    """Launch it, check the tap was answered at the exec, then let the watch end."""
    monkeypatch.setattr(launcher_module, "process_exe", _exe_after(1))
    scope_start(app)
    launcher = AppLauncher(config)
    loop = asyncio.get_running_loop()
    result = await asyncio.wait_for(launcher.launch(PUZZLE), timeout=5)
    assert result.ok and result.tracked, "an app tile's app is followed by the page"
    assert loop.time() < app.ends_at, "the tap was not answered at the exec"
    assert launcher.running == {"palapeli": result.unit}
    await asyncio.wait_for(asyncio.gather(*list(launcher._watches)), timeout=5)
    return launcher


async def test_an_app_that_fails_as_it_starts_is_forgotten_at_once(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    scope_start: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Answered at the exec, before a missing library had made it exit 1: the
    tile said "Açılıyor…" for 25 seconds over nothing. Now /running drops it
    as soon as it ends - also where systemd cannot be asked - which is what
    the page asks two seconds after its answer."""
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 1.0)
    with caplog.at_level(logging.DEBUG, logger=launcher_module.__name__):
        launcher = await _launched_then_watched(
            config, monkeypatch, scope_start, _EndsBySelf(code=1, after=0.4)
        )

    assert launcher.running == {} and launcher.started == {}

    async def systemd_says_nothing(_units: Any) -> None:
        return None

    launcher.active_units = systemd_says_nothing  # type: ignore[method-assign]
    assert await launcher.open_apps() == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ended as it started, with exit status 1" in line for line in warnings)
    assert any("error while loading shared libraries" in line for line in warnings)


async def test_an_app_that_ends_cleanly_as_it_starts_is_left_to_systemd(
    config: Config, monkeypatch: pytest.MonkeyPatch, scope_start: Any
) -> None:
    """Exit 0: a KDE app that handed the tap to a copy already running, or a
    program that forked its window. Not a failure; systemd says what is open."""
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 1.0)
    launcher = await _launched_then_watched(
        config, monkeypatch, scope_start, _EndsBySelf(code=0, after=0.4)
    )
    assert launcher.running == {"palapeli": "biokids-app-palapeli-1"}


async def test_an_app_is_watched_only_for_the_refusal_wait(
    config: Config, monkeypatch: pytest.MonkeyPatch, scope_start: Any
) -> None:
    """After `SCOPE_REFUSAL_TIMEOUT_S` the watch lets go: an app that ends
    later was played with, and systemd is what says it has closed."""
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 0.3)
    app = _EndsBySelf(code=1, after=0.8)
    launcher = await _launched_then_watched(config, monkeypatch, scope_start, app)
    assert asyncio.get_running_loop().time() < app.ends_at, "the watch waited for the app"
    assert launcher.running == {"palapeli": "biokids-app-palapeli-1"}
    assert app.returncode is None


async def test_what_the_page_follows(
    config: Config, starts: _Starts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app tile's app, started or found open; never a web tile's window."""
    monkeypatch.setattr(launcher_module, "prepare_browser_profile", lambda *_a: True)
    launcher = AppLauncher(config)
    site = LaunchRequest(slug="eba", name="EBA", kind="web", url="https://www.eba.gov.tr")

    assert (await launcher.launch(PUZZLE)).tracked
    assert not (await launcher.launch(site)).tracked
    _Session(launcher, shown=0)
    again = await launcher.launch(PUZZLE)
    assert again.tracked and again.unit == "biokids-app-palapeli-1"


def test_the_exec_is_systemd_run_becoming_something_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for exe, execed in ((None, False), (RUNNER, False), ("/usr/games/palapeli", True)):
        monkeypatch.setattr(launcher_module, "process_exe", lambda _p, _proc=None, e=exe: e)
        assert launcher_module.has_execed(4242, RUNNER) is execed


def test_the_program_is_read_from_proc(tmp_path: Path) -> None:
    (tmp_path / "4242").mkdir()
    try:
        (tmp_path / "4242" / "exe").symlink_to("/usr/games/palapeli")
    except OSError:
        pytest.skip("this machine does not let the tests make symbolic links")
    assert launcher_module.process_exe(4242, tmp_path) == str(Path("/usr/games/palapeli"))
    assert launcher_module.process_exe(4343, tmp_path) is None


# ---------------------------------------------- D-227 a window the screen's size


@pytest.mark.parametrize(
    ("screen", "argv"),
    [
        ((1600, 900), ["tuxpaint", "--windowed", "--1592x832"]),
        ((1024, 768), ["tuxpaint", "--windowed", "--1016x700"]),
        ((640, 480), ["tuxpaint", "--windowed"]),
        (None, ["tuxpaint", "--windowed"]),
    ],
)
def test_tux_paint_is_given_the_screens_size(
    screen: tuple[int, int] | None, argv: list[str]
) -> None:
    """It cannot be maximised, and floated mid-screen with the launcher's tiles
    tappable around it. Too small a screen, or none known: its own size."""
    from biokids_shell.launcher import fit_window

    assert fit_window(["tuxpaint", "--windowed", "--{window}"], screen) == argv
    assert fit_window(["gcompris-qt", "--window"], screen) == ["gcompris-qt", "--window"]


def test_the_catalog_asks_for_it_for_tux_paint() -> None:
    from biokids_core.catalog import load_catalog

    (paint,) = [entry for entry in load_catalog().entries if entry.slug == "tuxpaint"]
    assert paint.exec_argv == ("tuxpaint", "--windowed", "--{window}")
