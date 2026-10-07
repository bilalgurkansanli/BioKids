"""The kiosk Chromium: the only window the child ever sees.

`biokids-shell` serves HTML; this starts a Chromium locked to it and puts it
back when it goes away. The lockdown is three layers, because any one of them
alone is a bypass:

1. **A managed policy** (`biokids_core.browser_policy`): no developer tools, no
   extensions, no incognito, no printing, no downloads, no `file://`, and in
   "only the sites I choose" mode `URLBlocklist: ["*"]` with the launcher and
   the web tiles as the exceptions. Chromium reads it machine-wide from
   `/etc/chromium/policies/managed`; the daemon writes it.
2. **Chromium's own switches**: `--kiosk`, `--app=`, no first-run bubbles, no
   default-browser prompt, no translate bar.
3. **The window manager.** Chromium cannot suppress `Alt+F4` or `Ctrl+Alt+F2` —
   those never reach it. `xfce/` holds the xfwm4 and logind configuration that
   does, and `SECURITY.md` §1 is honest that a determined ten-year-old with a
   phone and a search engine gets past all of this.

If Chromium exits, it is started again. That is the whole supervisor: a child
who finds a way to close the window gets it back two seconds later, which is
both the enforcement and, for a four-year-old, the explanation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Re-exported: the policy lives in biokids_core so that the daemon, which writes
# it, does not need this package installed.
from biokids_core.browser_policy import (
    INTERNAL_PAGES as INTERNAL_PAGES,
)
from biokids_core.browser_policy import (
    MANAGED_POLICY_DIR as MANAGED_POLICY_DIR,
)
from biokids_core.browser_policy import (
    POLICY_FILENAME,
    kiosk_policy,
)
from biokids_core.config import Config, get_config

log = logging.getLogger(__name__)


KIOSK_WINDOW_CLASS = "biokids-kiosk"
"""The launcher window's WM_CLASS, set with Chromium's `--class`. It is how the
relay finds the launcher among the app windows to bring a warning to the front
(D-160)."""


def launcher_port(config: Config) -> int:
    """This child's launcher port: `shell.port + uid % 100` (D-188)."""
    getuid = getattr(os, "getuid", None)
    return config.shell.launcher_port(getuid() if getuid is not None else 0)


RESTART_BACKOFF_MAX = 30.0
"""If Chromium is crash-looping, back off rather than spinning the CPU. The
child still sees a locked screen, because the session is the desktop and the
desktop has nothing on it."""


@dataclass
class KioskSupervisor:
    """Starts the kiosk browser and keeps it there."""

    config: Config = field(default_factory=get_config)
    profile_dir: Path | None = None
    _policy_dir: Path | None = None
    """Overridden only by the tests. Production always uses MANAGED_POLICY_DIR,
    because that is the one Chromium reads."""
    process: asyncio.subprocess.Process | None = None
    restarts: int = 0
    _stopping: bool = False

    @property
    def url(self) -> str:
        return f"http://{self.origin}/"

    @property
    def origin(self) -> str:
        return f"{self.config.shell.host}:{launcher_port(self.config)}"

    def profile(self) -> Path:
        return self.profile_dir or (Path.home() / ".config" / "biokids" / "kiosk")

    @property
    def policy_dir(self) -> Path:
        """Where Chromium on Linux actually looks. Not the profile directory."""
        return self._policy_dir or self.config.paths.chromium_policy_dir

    def policy_path(self) -> Path:
        return self.policy_dir / POLICY_FILENAME

    def policy_in_force(self) -> bool:
        """Is there a managed policy at the path Chromium actually reads?

        Not "did we write one": for a long time the answer to that was yes and
        the answer to this was no, because the file went to
        `<user-data-dir>/policies/managed/`, which Chromium's Linux policy
        loader does not look at. `--user-data-dir` adds no policy source; only
        `/etc/chromium/policies/{managed,recommended}` is read.
        """
        path = self.policy_path()
        if not path.is_file():
            return False
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        allowed = loaded.get("URLAllowlist") or []
        return any(self.origin in str(entry) for entry in allowed)

    def write_policy(self) -> Path | None:
        """Put the policy where Chromium reads it, or say loudly that we cannot.

        `/etc/chromium/policies/managed/` is root-owned and the launcher runs as
        the child, so on a packaged machine this is already there - the package
        ships it, because the policy depends on nothing but host:port and that is
        the same for every child. This method is what covers a developer running
        as root, and what turns "no policy" from silence into an error.

        Returns None when the policy is not in force. A caller that treated a
        returned path as "the kiosk is locked down" was the shape of the
        original bug, so there is no path to return when it is not true.
        """
        path = self.policy_path()
        try:
            self.policy_dir.mkdir(parents=True, exist_ok=True)
            # Written atomically: a half-written policy file is one Chromium
            # ignores entirely, and a Chromium with no policy is a browser with
            # no limits.
            temp = path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(kiosk_policy(self.origin), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            temp.replace(path)
            return path
        except OSError as exc:
            if self.policy_in_force():
                # The package put it there. This is the normal path: the child
                # cannot write /etc and does not need to.
                log.debug("the managed policy is already in place at %s", path)
                return path
            log.error(
                "no Chromium managed policy at %s (%s). The kiosk will start with "
                "NO policy at all: no URL allowlist, DevTools available, downloads "
                "unrestricted. Install biokids-shell, or run "
                "`biokids-admin` as root to write it.",
                path,
                exc,
            )
            return None

    def argv(self) -> list[str]:
        return [
            self.config.shell.kiosk_command,
            "--kiosk",
            f"--app={self.url}",
            f"--class={KIOSK_WINDOW_CLASS}",
            f"--user-data-dir={self.profile()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            "--disable-pinch",
            "--overscroll-history-navigation=0",
            "--disable-session-crashed-bubble",
            "--hide-crash-restore-bubble",
            "--noerrdialogs",
            "--disable-infobars",
            "--check-for-update-interval=31536000",
            "--password-store=basic",
            "--autoplay-policy=no-user-gesture-required",
        ]

    async def start(self) -> bool:
        if shutil.which(self.config.shell.kiosk_command) is None:
            log.error(
                "%s is not installed; the child has no launcher window. "
                "The session will show an empty desktop, which is a broken "
                "install, not a supervised machine.",
                self.config.shell.kiosk_command,
            )
            return False
        self.write_policy()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            log.exception("could not start the kiosk browser")
            return False
        log.info("kiosk browser started on %s", self.url)
        return True

    async def supervise(self, stop_event: asyncio.Event) -> None:
        """Restart the kiosk whenever it exits, until asked to stop."""
        backoff = float(self.config.shell.kiosk_restart_seconds)
        while not stop_event.is_set():
            if self.process is None and not await self.start():
                return
            if self.process is None:  # pragma: no cover - start() returned True
                return
            await self.process.wait()
            if stop_event.is_set() or self._stopping:
                return
            self.restarts += 1
            log.warning("the kiosk browser exited; restarting (%s)", self.restarts)
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
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:  # pragma: no cover - a wedged browser
            process.kill()
