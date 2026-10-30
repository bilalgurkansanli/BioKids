"""Starting an application, in a way the daemon can account for.

Every application the child starts goes into its own transient systemd scope:

    systemd-run --user --scope --unit=biokids-app-<slug>-<n> -- <argv>

That one line buys four things (`ARCHITECTURE.md` §7):

* a **cgroup**, so the active category is known from the focused window's cgroup
  rather than guessed from a window title;
* correct **category attribution** even with several applications open;
* a **clean stop** at limit time — `systemctl --user stop` ends the whole scope,
  not just the process we happened to spawn;
* somewhere for Phase 4's per-application network rules to attach.

A web tile is the same mechanism pointed at the child's Chromium profile rather
than at a packaged binary, so "İnternet" is accounted for exactly like TuxPaint.

A tap on the tile of an app that is already open brings that app's window back
rather than starting a second copy (D-197): the app bar's 🏠 sends an app to the
background, and its tile is the way back to it. The launcher's footer lists the
apps that are open that way, from `AppLauncher.open_apps` (D-201).

Never two copies of one app (D-208). Taps on one tile are taken one at a time,
and an app whose scope is still running is not started again unless it has
been seen to have no window long after it started - not because the app bar
could not look in time, which on a busy machine is exactly when a slow app is
still opening.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from biokids_core.catalog import resolve_program
from biokids_core.config import Config, get_config
from biokids_shell.appbar import (
    EXIT_BAD_UNIT,
    EXIT_NOT_FOUND,
    MODULE,
    is_app_scope,
    scope_unit,
    stop_command,
)
from biokids_shell.appbar import control as appbar_control

log = logging.getLogger(__name__)

SCOPE_REFUSAL_TIMEOUT_S = 1.5
"""The longest wait before deciding systemd-run did not refuse, and how long a
started app is watched for failing as it starts.

A refusal happens before the exec and so is immediate; anything still alive
after this has become the application itself. Usually the exec is seen long
before (`EXEC_POLL_S`), and the tap is answered then - a moment before an app
that cannot start has had time to fail, so it is still watched until this much
time has passed since it was started (`AppLauncher._watch`). The page asks
/running two seconds after its answer (shell.js), after this has run out."""
EXEC_POLL_S = 0.05
"""How often `/proc/<pid>/exe` is looked at while systemd-run sets the scope up."""

BAR_ANSWER_TIMEOUT_S = 1.0
"""How long the running app bar gets to show an open app's window (D-208)."""
BRING_BACK_TIMEOUT_S = 5.0
"""How long the app bar's helper gets, where no bar is running to ask."""
STILL_OPENING_S = 30.0
"""An app with no window yet this soon after it started is still opening it; a
second tap waits for it rather than starting another. Later, a scope with no
window is something left behind, and is stopped before the app starts again."""
BROWSER_SCOPES_KEPT = 64
"""Web tiles' scopes remembered (`AppLauncher.browser_scopes`). Each tap is one,
and all but the browser's own end at once; a day of taps is far fewer."""
WINDOW_SIZE = "{window}"
"""In a catalog `exec`: the size of a window that fills the screen inside its
title bar and borders (D-227). For an app whose window cannot be maximised but
which takes its size on its command line - Tux Paint's `--{window}` becomes
`--1592x832` on a 1600x900 screen - so that it does not float in the middle of
the screen with the launcher around it. Where the screen's size cannot be read,
the argument is left out and the app opens at its own size."""
FRAME_WIDTH = 2 * 4
"""The BioKids xfwm4 theme's borders, left and right (dev/make_xfwm4_theme.py)."""
FRAME_HEIGHT = 64 + 4
"""Its title bar and its bottom border."""
SMALLEST_WINDOW = (640, 480)
"""No app is asked for less: Tux Paint's own minimum."""
UNIT_COMMAND_TIMEOUT_S = 30.0
UNIT_STATE_TIMEOUT_S = 5.0
"""`systemctl --user is-active` for one scope: a tap waits for it."""
SYSTEMCTL_INACTIVE = frozenset({3, 4})
"""`is-active`'s "not running" (3, and 4 for a unit systemd has forgotten);
any other failure is systemd not answering, which says nothing about the app."""
OPEN_APPS_TIMEOUT_S = 5.0
"""The launcher's page asks every few seconds which apps are open; a slow answer
is no answer, and the footer stays as it was."""


UNIT_PREFIX = "biokids-app"

CHROMIUM_PREFERENCES = Path("Default") / "Preferences"


def fit_window(argv: Sequence[str], screen: tuple[int, int] | None) -> list[str]:
    """`argv` with `WINDOW_SIZE` filled in for this screen, or its argument left out."""
    size = ""
    if screen is not None:
        width, height = screen[0] - FRAME_WIDTH, screen[1] - FRAME_HEIGHT
        if width >= SMALLEST_WINDOW[0] and height >= SMALLEST_WINDOW[1]:
            size = f"{width}x{height}"
    return [part.replace(WINDOW_SIZE, size) for part in argv if size or WINDOW_SIZE not in part]


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    slug: str
    name: str
    kind: str
    """`app` | `web` | `builtin`."""
    exec_cmd: str = ""
    url: str = ""
    category: str = ""
    locale: str = ""
    """The family's language: the child's browser asks websites for it."""


@dataclass(frozen=True, slots=True)
class LaunchResult:
    ok: bool
    unit: str = ""
    error: str = ""
    """An i18n key, never a sentence."""
    tracked: bool = False
    """The tile's app is in `AppLauncher.running`, so /running lists it while it
    runs - and stops listing it at once when it ends with an error as it starts
    (`AppLauncher._watch`). The page asks, a moment after the answer, to tell
    an app that is opening from one that is gone (D-208). Not a web tile's."""


@dataclass
class AppLauncher:
    config: Config = field(default_factory=get_config)
    counter: int = 0
    running: dict[str, str] = field(default_factory=dict)
    """slug -> unit name of each app tile's app, so a second tap re-focuses
    rather than starting a second copy (`_open_again`). Not web tiles: every
    browser window is one Chromium, and their tiles open a window each time."""
    started: dict[str, float] = field(default_factory=dict)
    """unit -> when it started (monotonic): an app still opening its window is
    told apart from a scope that has none. By unit, not by tile, so the time is
    always that of the copy being asked about."""
    browser_scopes: dict[str, None] = field(default_factory=dict)
    """The scopes web tiles were started in, oldest first. The child's browser
    runs in the first of them still alive, so a browser window in front is in
    one of these (D-228). Kept to `BROWSER_SCOPES_KEPT`, the first always."""
    last_web: str = ""
    """The web tile tapped last: what a browser window in front is taken to be,
    since every web tile's window is one Chromium's."""
    web_open: dict[str, None] = field(default_factory=dict)
    """The web tiles opened while the child's browser has been running, oldest
    first (D-254): the footer lists them with the apps, and a tap on one goes
    back to its tab, which the extension brings back rather than open twice."""
    appbar_socket: Path | None = None
    """Where the running app bar listens (D-208); its usual place when None."""
    _watches: set[asyncio.Task[None]] = field(default_factory=set)
    """Started apps still watched (`_watch`). Held so the event loop cannot
    collect one mid-flight, which would put the stderr pipe back where it started."""
    _taps: dict[str, asyncio.Lock] = field(default_factory=dict)
    """One lock per tile: a second tap waits for the first to be answered, and
    then finds the app it started instead of starting another."""

    async def launch(self, request: LaunchRequest) -> LaunchResult:
        argv = self.build_argv(request)
        if argv is None:
            return LaunchResult(ok=False, error="app.not_allowed")
        if any(WINDOW_SIZE in part for part in argv):
            from biokids_shell.kiosk import screen_size

            argv = fit_window(argv, await screen_size())
        if request.kind != "app":
            return await self._start(request, argv)
        # Not web tiles: every browser window is one Chromium, in the scope of
        # whichever tile started it first, so "the window of this tile's scope"
        # can be another tile's site. A web tile opens a window.
        lock = self._taps.setdefault(request.slug, asyncio.Lock())
        async with lock:
            again = await self._open_again(request.slug)
            if again is not None:
                return again
            return await self._start(request, argv)

    async def _start(self, request: LaunchRequest, argv: list[str]) -> LaunchResult:
        if request.kind == "web" or (request.kind == "builtin" and request.slug == "browser"):
            prepare_browser_profile(self.child_browser_profile(), request.locale)

        self.counter += 1
        unit = f"{UNIT_PREFIX}-{_safe_unit(request.slug)}-{self.counter}"
        command = [
            "systemd-run",
            "--user",
            "--scope",
            "--collect",
            "--quiet",
            f"--unit={unit}",
            f"--property=TimeoutStopSec={self.config.daemon.app_term_grace_seconds}",
            "--",
            *argv,
        ]

        runner = shutil.which("systemd-run")
        if runner is None:
            # A development machine, or a session without a user manager. Start
            # the process directly so the launcher is still usable; it will not
            # be accounted for by cgroup, and the log says so rather than
            # pretending everything is normal.
            log.warning(
                "systemd-run is not available; starting %s unaccounted. "
                "Category time will not be attributed for this process.",
                request.slug,
            )
            command = list(argv)
            unit = ""

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            log.exception("could not start %s", request.slug)
            return LaunchResult(ok=False, error="error.unknown")

        if unit:
            # `systemd-run --scope` does NOT fork and return. It registers its
            # own PID into the transient scope and then execs the command, so
            # the process we are holding *is* the application: awaiting it waits
            # for the child to close TuxPaint.
            #
            # What that cost: `report_activity` reached the daemon only after
            # the app was closed, so the daemon believed the child was in an app
            # they had just left; and the launch result was the application's own
            # exit status, so when the daemon stopped the scope at quota time -
            # SIGTERM, non-zero - the launcher reported that the launch had
            # failed.
            #
            # A refusal happens before the exec and is therefore fast, so a short
            # bounded wait separates "systemd said no" from "the app is running".
            until = asyncio.get_running_loop().time() + SCOPE_REFUSAL_TIMEOUT_S
            refused = await self._refused_quickly(
                process, request.slug, _resolved(runner), until
            )
            if refused:
                return LaunchResult(ok=False, error="error.unknown")
            tracked = request.kind == "app"
            if tracked:
                self._remember(request.slug, unit)
            else:
                self._remember_browser(request.slug, unit)
            # Remembered first, so that an app found to have failed is forgotten.
            self._keep_watching(process, request.slug, unit, until)
            return LaunchResult(ok=True, unit=unit, tracked=tracked)
        return LaunchResult(ok=True, unit=unit)

    # ------------------------------------------------------- open it again

    async def _open_again(self, slug: str) -> LaunchResult | None:
        """The second tap on an open app's tile: its window, not a second copy.

        A result when the tap is answered by the app already running; None when
        it should be started as usual. Only a scope systemd says has ended is
        started again, or one seen with no window long after it started. A bar
        that could not look - busy, restarting, or no GTK at all - is not an
        answer to "is it running": three copies of a game the owner was waiting
        for came from taking it for one (D-208).
        """
        unit = self.running.get(slug)
        if not unit:
            return None
        shown = await self.bring_back(unit)
        if shown == 0:
            log.info("%s is open already; its window is in front again", unit)
            return LaunchResult(ok=True, unit=unit, tracked=True)
        active = await self.unit_state(unit)
        if active is False:
            self._forget(slug, unit)
            return None
        if shown != EXIT_NOT_FOUND:
            log.info(
                "%s is %s, and its window could not be looked for; not starting it twice",
                unit,
                "running" if active else "perhaps running",
            )
            return LaunchResult(ok=True, unit=unit, tracked=True)
        if active is None or self.age(unit) < STILL_OPENING_S:
            # Still opening its window: a tile tapped again while a slow app starts.
            return LaunchResult(ok=True, unit=unit, tracked=True)
        log.info("%s is running with no window; stopping it to start it again", unit)
        await self.stop_unit(unit)
        self._forget(slug, unit)
        return None

    def _remember(self, slug: str, unit: str) -> None:
        old = self.running.pop(slug, None)  # the newest last, for the footer
        if old is not None:
            self.started.pop(old, None)
        self.running[slug] = unit
        self.started[unit] = time.monotonic()

    def _remember_browser(self, slug: str, unit: str) -> None:
        self.last_web = slug
        self.web_open.pop(slug, None)
        self.web_open[slug] = None
        self.browser_scopes[scope_unit(unit)] = None
        while len(self.browser_scopes) > BROWSER_SCOPES_KEPT:
            # Never the oldest: that is the one the browser most likely runs in.
            del self.browser_scopes[list(self.browser_scopes)[1]]

    def slug_of(self, scope: str) -> str | None:
        """The tile whose app runs in `scope`; None when it is not one of ours.

        An app tile by its own scope. A browser window is the web tile tapped
        last: the browser is one Chromium for every web tile, in the scope of
        whichever started it, so the scope cannot tell which site is in front.
        """
        for slug, unit in self.running.items():
            if scope_unit(unit) == scope:
                return slug
        if scope in self.browser_scopes and self.last_web:
            return self.last_web
        return None

    def _forget(self, slug: str, unit: str) -> None:
        """Forget `unit`, and the tile's app only if `unit` is still it."""
        if self.running.get(slug) == unit:
            del self.running[slug]
        self.started.pop(unit, None)

    def age(self, unit: str) -> float:
        """Seconds since this very unit was started; very old when not known."""
        since = self.started.get(unit)
        return float("inf") if since is None else time.monotonic() - since

    async def open_apps(self) -> list[str]:
        """The app tiles whose app is still running, in the order they were opened.

        What the launcher's footer shows as "Açık" (D-201): an app sent to the
        background with 🏠, which a tap on it brings back. Each is checked with
        systemd, all in one `systemctl --user is-active`; an app that has closed
        is forgotten. When systemd cannot be asked, the list is what the
        launcher started, unchecked.
        """
        apps = dict(self.running)
        browser = list(self.browser_scopes) if self.web_open else []
        if not apps and not browser:
            return []
        active = await self.active_units([*apps.values(), *browser])
        if active is None:
            return [*apps, *self.web_open]
        open_now: list[str] = []
        for slug, unit in apps.items():
            if scope_unit(unit) in active:
                open_now.append(slug)
            else:
                # Not one started again while systemctl was answering: that
                # one is the tile's app now, and `_forget` leaves it be.
                self._forget(slug, unit)
        # The sites opened since the browser started, while it still runs
        # (D-254). One whose tab was closed is listed until the browser is,
        # and a tap on it opens it again: never a dead entry.
        if browser and active.intersection(browser):
            open_now.extend(slug for slug in self.web_open if slug not in open_now)
        elif browser:
            self.web_open.clear()
        return open_now

    async def active_units(self, units: Sequence[str]) -> set[str] | None:
        """Which of these app scopes are running; None when systemd cannot say."""
        scopes = [scope_unit(unit) for unit in units if is_app_scope(scope_unit(unit))]
        if not scopes:
            return set()
        out = await _stdout(["systemctl", "--user", "is-active", *scopes], OPEN_APPS_TIMEOUT_S)
        return None if out is None else active_scopes(scopes, out)

    async def bring_back(self, unit: str) -> int | None:
        """Ask the app bar to show this app's window again.

        Its answer: 0 shown, `EXIT_NOT_FOUND` no window, anything else (or None)
        could not look. The bar is what knows the windows - which are the app's,
        and which are only minimised - so it is asked first, on its own socket
        (D-208). Only where no bar is running is its code run once as a helper,
        a new Python that loads GTK, which on a busy machine takes seconds.
        """
        if not is_app_scope(scope_unit(unit)):
            return EXIT_BAD_UNIT
        socket_path = self.appbar_socket or default_appbar_socket()
        try:
            return await appbar_control.ask_bar(socket_path, unit, BAR_ANSWER_TIMEOUT_S)
        except appbar_control.BarNotRunning as exc:
            # INFO, not DEBUG: whether a tap went to the bar or to a new Python
            # is what VM-RUNBOOK §8.10 reads in the journal.
            log.info("no app bar to ask (%s); running its helper", exc)
        return await _exit_code(
            [sys.executable, "-m", MODULE, "--bring-back", unit], BRING_BACK_TIMEOUT_S
        )

    async def unit_state(self, unit: str) -> bool | None:
        """Whether the app's scope is running; None when systemd does not say."""
        code = await _exit_code(
            ["systemctl", "--user", "is-active", "--quiet", scope_unit(unit)],
            UNIT_STATE_TIMEOUT_S,
        )
        if code == 0:
            return True
        return False if code in SYSTEMCTL_INACTIVE else None

    async def unit_active(self, unit: str) -> bool:
        return await self.unit_state(unit) is True

    async def stop_unit(self, unit: str) -> bool:
        try:
            argv = stop_command(scope_unit(unit))
        except ValueError:
            log.warning("not stopping %s: not an app's scope", unit)
            return False
        return await _exit_code(argv, UNIT_COMMAND_TIMEOUT_S) == 0

    async def _refused_quickly(
        self,
        process: asyncio.subprocess.Process,
        slug: str,
        runner: str | None,
        until: float,
    ) -> bool:
        """Did systemd-run refuse, or is the application now running?

        A refusal is a non-zero exit within the first moment, before the exec.
        Anything still alive after that has become the application, and waiting
        on it is waiting for the child to finish playing.

        The moment of the exec can be seen: `/proc/<pid>/exe` stops being
        systemd-run (`runner`) and becomes the app. Once it has, nothing can be
        refused any more and the tap is answered then - a tenth of a second,
        usually - instead of at `until`, the end of `SCOPE_REFUSAL_TIMEOUT_S`
        (D-208). Where it cannot be read, the whole wait it is.
        """
        loop = asyncio.get_running_loop()
        pid = getattr(process, "pid", None)
        watched = (pid, runner) if isinstance(pid, int) and runner is not None else None
        exited = asyncio.ensure_future(process.wait())
        try:
            while not exited.done():
                left = until - loop.time()
                if left <= 0:
                    break
                step = left if watched is None else min(EXEC_POLL_S, left)
                await asyncio.wait({exited}, timeout=step)
                if not exited.done() and watched is not None and has_execed(*watched):
                    break
            still_running = not exited.done()
        finally:
            if not exited.done():
                exited.cancel()
        if still_running or process.returncode in (0, None):
            # Running - or exited cleanly and immediately: some applications
            # fork a real window and exit, or hand the tap to a copy already
            # running. Launched, not refused; `_watch` takes it from here.
            return False

        err = b""
        if process.stderr is not None:
            with contextlib.suppress(Exception):
                err = await process.stderr.read()
        log.error("systemd-run refused %s: %s", slug, err.decode("utf-8", "replace").strip())
        return True

    def _keep_watching(
        self, process: asyncio.subprocess.Process, slug: str, unit: str, until: float
    ) -> None:
        """`_watch` in the background, held in a set: a task with no reference
        can be collected mid-flight."""
        task = asyncio.create_task(self._watch(process, slug, unit, until))
        self._watches.add(task)
        task.add_done_callback(self._watches.discard)

    async def _watch(
        self, process: asyncio.subprocess.Process, slug: str, unit: str, until: float
    ) -> None:
        """A started app, from the answer on: a failure as it starts, and its stderr.

        The tap is answered once systemd-run has become the app (D-208), a
        moment before an app that cannot start - a missing library, a broken
        setting - has had time to fail. Before D-208 the whole 1.5 s wait saw
        that exit and the child got the calm screen; answered at the exec, the
        tile said "Açılıyor…" for 25 seconds over nothing, and every tap again
        did the same. So the app is watched until `until`, the end of
        `SCOPE_REFUSAL_TIMEOUT_S`: one that ends with an error by then is
        forgotten at once, /running stops listing it, and the page - which
        asks two seconds after its answer - ends the tile's opening and shows
        the calm screen. One that ends cleanly may have handed the tap to a
        copy already running, as KDE apps do: systemd says what is open.

        All the while stderr is read to its end, so a chatty app cannot fill the
        pipe and block itself.
        """
        reading = asyncio.ensure_future(_read_stderr(process))
        failed = False
        try:
            code = await _exit_within(process, until - asyncio.get_running_loop().time())
            if code not in (None, 0):
                failed = True
                self._forget(slug, unit)
                log.warning(
                    "%s ended as it started, with exit status %s; it is not open", unit, code
                )
            data = await reading
        finally:
            reading.cancel()
        if data:
            log.log(
                logging.WARNING if failed else logging.DEBUG,
                "%s wrote to stderr: %s",
                slug,
                data.decode("utf-8", "replace").strip()[:500],
            )

    def build_argv(self, request: LaunchRequest) -> list[str] | None:
        """What to actually run, or None when the tile has nothing to start."""
        match request.kind:
            case "web":
                return self.browser_argv(request.url) if request.url else None
            case "builtin":
                if request.slug == "browser":
                    return self.browser_argv(self.home_page())
                # Volume, brightness and shutdown are drawn by the launcher and
                # have no process of their own.
                return None
            case _:
                argv = shlex.split(request.exec_cmd)
                if not argv:
                    return None
                argv = [
                    str(Path(part).expanduser()) if part.startswith("~") else part
                    for part in argv
                ]
                program = resolve_program(argv[0])
                if program is None:
                    log.warning("%s is not installed", argv[0])
                    return None
                return [program, *argv[1:]]

    def browser_argv(self, url: str) -> list[str]:
        """The child's Chromium: its own profile, so its managed policy applies.

        Deliberately *not* the kiosk instance. That one is locked to the
        launcher's own origin and must stay that way; browsing happens in a
        separate window with the child's profile, which is where the URL
        allowlist and SafeSearch settings live (D-14, D-21).
        """
        return [
            self.config.shell.kiosk_command,
            f"--user-data-dir={self.child_browser_profile()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            # Or Chromium asks GNOME's keyring for somewhere to keep passwords,
            # and the keyring asks a six-year-old to choose a password for a
            # "Default Keyring" - on an account that deliberately has none (D-156).
            "--password-store=basic",
            # After a power cut or an end-of-day shutdown, Chromium greets the
            # child with "Restore pages? Chromium didn't shut down correctly".
            # Both spellings: the older one is what the kiosk already passes,
            # and current Chromium reads the newer one.
            "--hide-crash-restore-bubble",
            "--disable-session-crashed-bubble",
            "--start-maximized",
            url,
        ]

    def child_browser_profile(self) -> Path:
        return Path.home() / ".config" / "biokids" / "browser"

    def home_page(self) -> str:
        """Where the plain "İnternet" tile goes.

        In allowlist mode for a 4–7 year old, the browser's own new-tab page is
        a wall of Google suggestions, so the launcher serves a start page of the
        child's own allowed sites instead.
        """
        from biokids_shell.kiosk import launcher_port

        return f"http://{self.config.shell.host}:{launcher_port(self.config)}/start"


ACCEPT_LANGUAGES = {"tr": "tr-TR,tr,en-US,en", "en": "en-US,en"}
"""What a child's browser asks websites for, by the family's language. A profile
made while Debian's chromium-l10n was missing asks for English for good, and
YouTube Kids then sets itself up in English."""

CHROMIUM_DEFAULT_LANGUAGES = ("", "en-US,en")


def prepare_browser_profile(profile: Path, locale: str = "") -> bool:
    """Set up the child's Chromium profile before it starts.

    The window manager's title bar (D-163): Chromium draws its own frame by
    default, with its own small close button in its own place.
    `browser.custom_chrome_frame: false` hands the frame to xfwm4, so a web
    tile has the same big close button in the same corner as every other app.

    The family's language, unless someone chose other languages in that
    browser: then theirs stay. Returns whether the settings are in place.

    Written only while that profile's Chromium is not running - a running one
    holds its preferences in memory and writes them back over ours - and never
    over a preferences file that cannot be read, which may be the child's.
    """
    lock = profile / "SingletonLock"
    if lock.is_symlink() or lock.exists():
        return False
    path = profile / CHROMIUM_PREFERENCES
    prefs: dict[str, object] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("%s could not be read (%s); leaving it alone", path, exc)
            return False
        if not isinstance(loaded, dict):
            return False
        prefs = loaded
    browser = prefs.setdefault("browser", {})
    intl = prefs.setdefault("intl", {})
    if not isinstance(browser, dict) or not isinstance(intl, dict):
        return False
    changed = False
    if browser.get("custom_chrome_frame") is not False:
        browser["custom_chrome_frame"] = False
        changed = True
    languages = ACCEPT_LANGUAGES.get(locale)
    current = intl.get("selected_languages", "")
    if languages and current != languages and current in CHROMIUM_DEFAULT_LANGUAGES:
        intl["selected_languages"] = languages
        changed = True
    if not prefs["intl"]:
        del prefs["intl"]
    if not changed:
        return True
    partial = path.with_name(path.name + ".biokids")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text(json.dumps(prefs), encoding="utf-8")
        partial.replace(path)
    except OSError as exc:
        log.warning("%s could not be written: %s", path, exc)
        with contextlib.suppress(OSError):
            partial.unlink()
        return False
    return True


def active_scopes(scopes: Sequence[str], output: str) -> set[str] | None:
    """`systemctl is-active a b c` prints one state per unit, in order.

    Only `active` is running: `inactive` is a scope that has ended (and with
    `--collect`, been forgotten), `deactivating` one being stopped. Output that
    does not line up with the question is no answer.
    """
    states = output.split()
    if len(states) != len(scopes):
        return None
    return {scope for scope, state in zip(scopes, states, strict=True) if state == "active"}


def default_appbar_socket() -> Path | None:
    """Where this session's app bar listens: the launcher started it, with the
    same `$XDG_RUNTIME_DIR` (D-208)."""
    return appbar_control.socket_path()


def process_exe(pid: int, proc: Path = Path("/proc")) -> str | None:
    """The program process `pid` is running now, or None when that cannot be read."""
    try:
        return str((proc / str(pid) / "exe").readlink())
    except (OSError, ValueError):
        return None


def has_execed(pid: int, runner: str, proc: Path = Path("/proc")) -> bool:
    """Whether systemd-run (`runner`, its real path) has become the application.

    `systemd-run --scope` sets the scope up and then execs the command in the
    same process; from then on its `/proc/<pid>/exe` is the app's program (or
    the interpreter of an app that is a script). A process that cannot be read
    - gone, or no `/proc` - has not been seen to exec.
    """
    exe = process_exe(pid, proc)
    return exe is not None and exe != runner


def _resolved(program: str | None) -> str | None:
    """A program's real path, as the kernel names it in `/proc/<pid>/exe`."""
    if program is None:
        return None
    try:
        return str(Path(program).resolve())
    except (OSError, ValueError, RuntimeError):
        return None


async def _exit_within(process: asyncio.subprocess.Process, within: float) -> int | None:
    """The process's exit status if it has ended `within` seconds from now; None
    while it runs.

    Its `returncode`, not what `wait()` returns: `wait()` also waits for the
    pipes to close, which a helper the app left behind may hold open after the
    app itself is gone.
    """
    if within > 0:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), within)
    return process.returncode


async def _read_stderr(process: asyncio.subprocess.Process) -> bytes:
    """All the process writes to stderr, read until it closes; nothing when it cannot be."""
    if process.stderr is not None:
        with contextlib.suppress(Exception):  # an app's output is never worth a failure
            return await process.stderr.read()
    return b""


async def _stdout(argv: list[str], within: float) -> str | None:
    """stdout of a short command whatever its exit code; None if it could not run in time.

    `systemctl is-active` exits 3 when any unit is not active, and still says
    which are.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        log.debug("%s could not be run: %s", argv[0], exc)
        return None
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=within)
    except TimeoutError:
        log.warning("%s took longer than %s s", " ".join(argv[:4]), within)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return None
    return out.decode("utf-8", "replace")


async def _exit_code(argv: list[str], within: float) -> int | None:
    """Run a short command; its exit code, or None if it could not be run in time."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        log.warning("%s could not be run: %s", argv[0], exc)
        return None
    try:
        return await asyncio.wait_for(process.wait(), timeout=within)
    except TimeoutError:
        log.warning("%s took longer than %s s", " ".join(argv[:4]), within)
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        return None


def _safe_unit(slug: str) -> str:
    """A systemd unit name is not a place to put user input unescaped."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug)[:48] or "app"
