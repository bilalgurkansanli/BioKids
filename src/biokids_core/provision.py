"""Making a child real on the machine, not only in the database.

Adding a child in the panel used to write a row and stop there. The row named a
Unix account that did not exist, so the child had nothing to log in to; and even
once an account existed, nothing put the session configuration in their home, so
they would have landed on an ordinary XFCE desktop with a file manager, window
decorations and Alt+F4 — while the parent's Today screen said the child was
managed. That gap is the worst kind: everything looks right.

This module closes it. It is deliberately not in the packaging: `.deb`
maintainer scripts run before any child exists and cannot know their names, and
`/etc/xdg/autostart` would start a kiosk launcher inside the *parent's* session
too. Provisioning belongs at the moment a parent adds a child, which is here.

Everything is idempotent and nothing is destructive. An account that exists is
left alone, a file a parent edited is not overwritten unless asked, and nothing
here ever deletes a home directory — removing a child from BioKids must not
remove their drawings.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from biokids_core.config import Config, get_config

# `pwd` is POSIX-only and typeshed says so, so it is reached through a module
# shim rather than spelled out: `mypy --strict` runs on the Windows host this
# project is developed from and production is Linux (D-12). Everything here
# degrades to "this user does not exist", which is the honest answer on a host
# that has no password database.
_pwd: Any
try:  # pragma: no cover - an import-time platform check
    import pwd as _pwd_module

    _pwd = _pwd_module
except ImportError:  # pragma: no cover - the Windows development host
    _pwd = None

log = logging.getLogger(__name__)

COMMAND_TIMEOUT = 30

XFCE_CHANNEL_DIR = Path(".config/xfce4/xfconf/xfce-perchannel-xml")
AUTOSTART_DIR = Path(".config/autostart")
AUTOSTART_FILE = "biokids-shell.desktop"


@dataclass
class ProvisionResult:
    """What happened, in enough detail for an audit entry."""

    unix_user: str
    account_existed: bool = False
    account_created: bool = False
    files_written: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_detail(self) -> dict[str, object]:
        return {
            "unix_user": self.unix_user,
            "account_created": self.account_created,
            "files": self.files_written,
            "problems": self.problems,
        }


def _passwd_entry(unix_user: str) -> Any | None:
    if _pwd is None:
        return None
    try:
        return _pwd.getpwnam(unix_user)
    except KeyError:
        return None


def account_exists(unix_user: str) -> bool:
    return _passwd_entry(unix_user) is not None


def home_of(unix_user: str) -> Path | None:
    entry = _passwd_entry(unix_user)
    return Path(entry.pw_dir) if entry is not None else None


def _run(argv: Sequence[str]) -> tuple[bool, str]:
    if shutil.which(argv[0]) is None:
        return False, f"{argv[0]} is not installed"
    try:
        result = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[:300]
    return True, ""


def create_account(unix_user: str, display_name: str = "") -> tuple[bool, str]:
    """Create the child's Unix account, with nothing it should not have.

    No password: the login screen is a set of avatars and the real gate is the
    quota, not the login (D-99). No sudo, no supplementary groups, and
    emphatically not `biokids` — that group can grant time and power the machine
    off (D-123).
    """
    argv = [
        "adduser",
        "--disabled-password",
        "--gecos",
        display_name or unix_user,
        unix_user,
    ]
    ok, problem = _run(argv)
    if ok:
        log.info("created the account %s", unix_user)
    return ok, problem


def install_session_files(
    unix_user: str,
    config: Config | None = None,
    *,
    overwrite: bool = False,
) -> tuple[list[str], list[str]]:
    """Put the XFCE configuration and the autostart entry in the child's home.

    Returns (written, problems). A file the parent has edited is left alone
    unless `overwrite` is set: BioKids owns the defaults, not the child's home.
    """
    config = config or get_config()
    source = config.paths.share_dir / "xfce"
    home = home_of(unix_user)
    written: list[str] = []
    problems: list[str] = []

    if home is None:
        return written, [f"{unix_user} has no home directory"]
    if not source.is_dir():
        return written, [f"{source} is missing; is biokids-shell installed?"]

    entry = _passwd_entry(unix_user)
    if entry is None:
        return written, [f"{unix_user} is not a user on this machine"]
    uid, gid = int(entry.pw_uid), int(entry.pw_gid)

    plan: list[tuple[Path, Path]] = []
    for xml in sorted(source.glob("*.xml")):
        plan.append((xml, home / XFCE_CHANNEL_DIR / xml.name))
    autostart = source / AUTOSTART_FILE
    if autostart.is_file():
        plan.append((autostart, home / AUTOSTART_DIR / AUTOSTART_FILE))

    for src, dst in plan:
        if dst.exists() and not overwrite:
            continue
        try:
            _own(dst.parent, uid, gid, make=True)
            shutil.copyfile(src, dst)
            dst.chmod(0o644)
            _own(dst, uid, gid)
        except OSError as exc:
            problems.append(f"{dst}: {exc}")
            continue
        written.append(str(dst))

    return written, problems


def _own(path: Path, uid: int, gid: int, *, make: bool = False) -> None:
    if make:
        path.mkdir(parents=True, exist_ok=True)
    chown = getattr(os, "chown", None)
    if chown is not None:
        chown(path, uid, gid)


def provision(
    unix_user: str,
    display_name: str = "",
    config: Config | None = None,
    *,
    create: bool = True,
    overwrite: bool = False,
) -> ProvisionResult:
    """Make sure this child can log in and lands in the launcher.

    Safe to run again: an existing account is left alone and existing files are
    not overwritten. Called when a parent adds a child, and available as
    `biokids-admin provision-child` for repairing an install by hand.
    """
    result = ProvisionResult(unix_user=unix_user)
    result.account_existed = account_exists(unix_user)

    if not result.account_existed:
        if not create:
            result.problems.append(f"{unix_user} does not exist on this machine")
            return result
        created, problem = create_account(unix_user, display_name)
        result.account_created = created
        if not created:
            result.problems.append(f"could not create {unix_user}: {problem}")
            return result

    written, problems = install_session_files(unix_user, config, overwrite=overwrite)
    result.files_written = written
    result.problems.extend(problems)
    return result
