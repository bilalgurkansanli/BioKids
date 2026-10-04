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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from biokids_core.catalog import resolve_program
from biokids_core.config import Config, get_config

log = logging.getLogger(__name__)

SCOPE_REFUSAL_TIMEOUT_S = 1.5
"""How long to wait before deciding systemd-run did not refuse.

A refusal happens before the exec and so is immediate; anything still alive
after this has become the application itself."""


UNIT_PREFIX = "biokids-app"

CHROMIUM_PREFERENCES = Path("Default") / "Preferences"


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


@dataclass
class AppLauncher:
    config: Config = field(default_factory=get_config)
    counter: int = 0
    running: dict[str, str] = field(default_factory=dict)
    """slug -> unit name, so a second tap re-focuses rather than starting a second copy."""
    _drains: set[asyncio.Task[None]] = field(default_factory=set)
    """Live stderr drains. Held so the event loop cannot collect one mid-flight."""

    async def launch(self, request: LaunchRequest) -> LaunchResult:
        argv = self.build_argv(request)
        if argv is None:
            return LaunchResult(ok=False, error="app.not_allowed")
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

        if shutil.which("systemd-run") is None:
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
            refused = await self._refused_quickly(process, request.slug)
            if refused:
                return LaunchResult(ok=False, error="error.unknown")
            self.running[request.slug] = unit
        return LaunchResult(ok=True, unit=unit)

    async def _refused_quickly(self, process: asyncio.subprocess.Process, slug: str) -> bool:
        """Did systemd-run refuse, or is the application now running?

        A refusal is a non-zero exit within the first moment, before the exec.
        Anything still alive after that has become the application, and waiting
        on it is waiting for the child to finish playing.
        """
        try:
            await asyncio.wait_for(process.wait(), timeout=SCOPE_REFUSAL_TIMEOUT_S)
        except TimeoutError:
            # Still running: the exec happened. Drain stderr in the background
            # so a chatty application cannot fill the pipe and block itself.
            # Held in a set: a task with no reference can be collected
            # mid-flight, which would put the pipe back where it started.
            task = asyncio.create_task(self._drain(process, slug))
            self._drains.add(task)
            task.add_done_callback(self._drains.discard)
            return False

        if process.returncode in (0, None):
            # Exited cleanly and immediately. Some applications fork a real
            # window and exit; treat that as launched rather than refused.
            return False

        err = b""
        if process.stderr is not None:
            with contextlib.suppress(Exception):
                err = await process.stderr.read()
        log.error("systemd-run refused %s: %s", slug, err.decode("utf-8", "replace").strip())
        return True

    async def _drain(self, process: asyncio.subprocess.Process, slug: str) -> None:
        """Read the application's stderr so the pipe cannot fill, and log it."""
        if process.stderr is None:
            return
        with contextlib.suppress(Exception):
            data = await process.stderr.read()
            if data:
                log.debug("%s wrote to stderr: %s", slug, data.decode("utf-8", "replace")[:500])

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
        return f"http://{self.config.shell.host}:{self.config.shell.port}/start"

    def kiosk_profile(self) -> Path:
        return Path.home() / ".config" / "biokids" / "kiosk"


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


def _safe_unit(slug: str) -> str:
    """A systemd unit name is not a place to put user input unescaped."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug)[:48] or "app"
