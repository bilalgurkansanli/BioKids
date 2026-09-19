"""The kiosk Chromium: the only window the child ever sees.

`biokids-shell` serves HTML; this starts a Chromium locked to it and puts it
back when it goes away. The lockdown is three layers, because any one of them
alone is a bypass:

1. **A managed policy for this profile alone.** `URLBlocklist: ["*"]` with the
   launcher's own origin as the single exception, no developer tools, no
   extensions, no incognito, no printing, no downloads. Written to the child's
   own Chromium user-data-dir, not the system policy directory, so it cannot
   affect the parent's browser.
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
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from biokids_core.config import Config, get_config

log = logging.getLogger(__name__)

RESTART_BACKOFF_MAX = 30.0
"""If Chromium is crash-looping, back off rather than spinning the CPU. The
child still sees a locked screen, because the session is the desktop and the
desktop has nothing on it."""


def kiosk_policy(origin: str) -> dict[str, object]:
    """The managed policy for the kiosk profile.

    Everything here is a `SECURITY.md` §5 setting narrowed to one window. The
    allowlist has exactly one entry, so "type another address" has nowhere to
    go even before DNS is involved.
    """
    return {
        "URLBlocklist": ["*"],
        "URLAllowlist": [origin],
        "URLBlocklistExceptions": [],
        "DeveloperToolsAvailability": 2,
        "IncognitoModeAvailability": 1,
        "ExtensionInstallBlocklist": ["*"],
        "BrowserSignin": 0,
        "SyncDisabled": True,
        "PasswordManagerEnabled": False,
        "DownloadRestrictions": 3,
        "PrintingEnabled": False,
        "DefaultPopupsSetting": 2,
        "DefaultNotificationsSetting": 2,
        "DefaultGeolocationSetting": 2,
        "TranslateEnabled": False,
        "ShowHomeButton": False,
        "BookmarkBarEnabled": False,
        "EditBookmarksEnabled": False,
        "AutofillAddressEnabled": False,
        "AutofillCreditCardEnabled": False,
        "DnsOverHttpsMode": "off",
    }


@dataclass
class KioskSupervisor:
    """Starts the kiosk browser and keeps it there."""

    config: Config = field(default_factory=get_config)
    profile_dir: Path | None = None
    process: asyncio.subprocess.Process | None = None
    restarts: int = 0
    _stopping: bool = False

    @property
    def url(self) -> str:
        return f"http://{self.config.shell.host}:{self.config.shell.port}/"

    @property
    def origin(self) -> str:
        return f"{self.config.shell.host}:{self.config.shell.port}"

    def profile(self) -> Path:
        return self.profile_dir or (Path.home() / ".config" / "biokids" / "kiosk")

    def write_policy(self) -> Path:
        """Chromium reads `<user-data-dir>/policies/managed/*.json` for a profile."""
        target = self.profile() / "policies" / "managed"
        target.mkdir(parents=True, exist_ok=True)
        path = target / "biokids-kiosk.json"
        # Written atomically: a half-written policy file is one Chromium ignores
        # entirely, and a Chromium with no policy is a browser with no limits.
        temp = path.with_suffix(".json.tmp")
        temp.write_text(
            json.dumps(kiosk_policy(self.origin), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temp.replace(path)
        return path

    def argv(self) -> list[str]:
        return [
            self.config.shell.kiosk_command,
            "--kiosk",
            f"--app={self.url}",
            f"--user-data-dir={self.profile()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate,MediaRouter",
            "--disable-pinch",
            "--overscroll-history-navigation=0",
            "--disable-session-crashed-bubble",
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
