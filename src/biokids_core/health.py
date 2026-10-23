"""Koruma durumu: whether everything that protects the child is in place (D-237).

BioKids is careful underneath - a filter checked every minute, a lock that
falls back to ending the session, a daemon whose death locks the children -
and a parent sees almost none of it. This turns it into one list on Ayarlar,
with a line on Today when something needs them: green where all is well, and
where it is not, what it means and what to do, in words a parent can follow.

It only reads. Each check is something the panel can look at for itself or
was told by the daemon's status, so the list is there with the daemon down,
which is when it matters most. A check that cannot be made says so rather
than guessing green.
"""

from __future__ import annotations

import datetime as dt
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from biokids_core.browser_policy import POLICY_FILENAME, packed_extension
from biokids_core.config import Config

OTHER_BROWSERS: Final = (
    Path("/usr/bin/firefox-esr"),
    Path("/usr/bin/firefox"),
    Path("/usr/bin/epiphany-browser"),
    Path("/usr/bin/falkon"),
)
"""Browsers BioKids does not manage. Any app's help link may open one (D-237)."""

GRUB_CONFIGS: Final = (Path("/boot/grub/grub.cfg"),)
TIMESYNC_FLAG: Final = Path("/run/systemd/timesync/synchronized")
"""Written by systemd-timesyncd once the clock is set from the network."""

LOW_DISK_BYTES: Final = 1024**3
"""Under a gigabyte free, the database and the voices start to fail to write."""

BACKUP_STALE: Final = dt.timedelta(days=30)


class Level(StrEnum):
    OK = "ok"
    INFO = "info"
    WARN = "warn"
    BAD = "bad"


@dataclass(frozen=True, slots=True)
class Check:
    key: str
    """`health.<key>.<level>` is the sentence; `health.<key>.title` its name."""
    level: Level
    link: str = ""
    """Where the parent can do something about it, when there is a place."""
    values: Mapping[str, Any] | None = None
    variant: str = ""
    """A sentence other than the level's own: `health.<key>.<variant>`."""


def checks(
    config: Config,
    status: Mapping[str, Any] | None,
    *,
    mail_configured: bool,
    last_backup: dt.datetime | None,
    now: dt.datetime | None = None,
    other_browsers: Sequence[Path] = OTHER_BROWSERS,
    grub_configs: Sequence[Path] = GRUB_CONFIGS,
    timesync_flag: Path = TIMESYNC_FLAG,
) -> list[Check]:
    """Every check, the ones that need the parent first."""
    found = [
        _daemon(status),
        _filter(status),
        _browser_policy(config),
        _extension(config),
        _other_browser(other_browsers),
        _mail(mail_configured),
        _backup(last_backup, now),
        _disk(config),
        _clock(timesync_flag),
        _boot_password(grub_configs),
    ]
    order = {Level.BAD: 0, Level.WARN: 1, Level.INFO: 2, Level.OK: 3}
    return sorted(found, key=lambda check: order[check.level])


def needing_attention(found: Sequence[Check]) -> int:
    """How many a parent should do something about: Today says so in one line."""
    return sum(1 for check in found if check.level in (Level.BAD, Level.WARN))


def _daemon(status: Mapping[str, Any] | None) -> Check:
    return Check("daemon", Level.OK if status is not None else Level.BAD)


def _filter(status: Mapping[str, Any] | None) -> Check:
    if status is None:
        return Check("filter", Level.INFO)
    dns = status.get("dns_filter") or {}
    if dns.get("open"):
        return Check("filter", Level.BAD, "/guides/internet")
    if not dns.get("running", False):
        return Check("filter", Level.BAD, "/guides/internet")
    return Check("filter", Level.OK)


def _browser_policy(config: Config) -> Check:
    path = config.paths.chromium_policy_dir / POLICY_FILENAME
    return Check("browser_policy", Level.OK if path.is_file() else Level.BAD)


def _extension(config: Config) -> Check:
    return Check(
        "extension", Level.OK if packed_extension(config.paths.extension_dir) else Level.WARN
    )


def _other_browser(candidates: Sequence[Path]) -> Check:
    present = [path.name for path in candidates if path.exists()]
    if not present:
        return Check("other_browser", Level.OK)
    return Check("other_browser", Level.WARN, "/guides/machine", {"names": ", ".join(present)})


def _mail(configured: bool) -> Check:
    return Check("mail", Level.OK if configured else Level.WARN, "/guides/email")


def _backup(last: dt.datetime | None, now: dt.datetime | None) -> Check:
    if last is None:
        return Check("backup", Level.INFO, "/settings/backup")
    now = now or dt.datetime.now(dt.UTC).replace(tzinfo=None)
    days = max(0, (now - last).days)
    level = Level.INFO if now - last > BACKUP_STALE else Level.OK
    variant = "ok_today" if level is Level.OK and days == 0 else ""
    return Check("backup", level, "/settings/backup", {"days": days}, variant)


def _disk(config: Config) -> Check:
    try:
        free = shutil.disk_usage(config.paths.data_dir).free
    except OSError:
        return Check("disk", Level.INFO)
    gigabytes = round(free / 1024**3, 1)
    return Check(
        "disk", Level.WARN if free < LOW_DISK_BYTES else Level.OK, "", {"gb": gigabytes}
    )


def _clock(flag: Path) -> Check:
    # BioKids counts time without the wall clock (D-40); the clock decides only
    # which day it is and when the hours open, so a clock never set is a note.
    return Check("clock", Level.OK if flag.exists() else Level.INFO)


def _boot_password(configs: Sequence[Path]) -> Check:
    for path in configs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        return Check(
            "boot_password",
            Level.OK if "password_pbkdf2" in text else Level.INFO,
            "/guides/machine",
        )
    return Check("boot_password", Level.INFO, "/guides/machine")
