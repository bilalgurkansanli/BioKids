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
import logging
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from biokids_core.config import Config, get_config

log = logging.getLogger(__name__)

UNIT_PREFIX = "biokids-app"


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    slug: str
    name: str
    kind: str
    """`app` | `web` | `builtin`."""
    exec_cmd: str = ""
    url: str = ""
    category: str = ""


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

    async def launch(self, request: LaunchRequest) -> LaunchResult:
        argv = self.build_argv(request)
        if argv is None:
            return LaunchResult(ok=False, error="app.not_allowed")

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
            # systemd-run --scope returns once the scope exists, so a non-zero
            # exit here means the scope was refused and nothing is running.
            _out, err = await process.communicate()
            if process.returncode not in (0, None):
                log.error(
                    "systemd-run refused %s: %s",
                    request.slug,
                    err.decode("utf-8", "replace").strip(),
                )
                return LaunchResult(ok=False, error="error.unknown")
            self.running[request.slug] = unit
        return LaunchResult(ok=True, unit=unit)

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
                if shutil.which(argv[0]) is None:
                    log.warning("%s is not installed", argv[0])
                    return None
                return argv

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


def _safe_unit(slug: str) -> str:
    """A systemd unit name is not a place to put user input unescaped."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in slug)[:48] or "app"
