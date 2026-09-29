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
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from biokids_core.config import Config, get_config

UNIX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
"""What the panel already enforces when a parent types a user name. Repeated
here because this module interpolates it into a systemd unit name."""

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


CHILD_GROUP = "biokids-child"
"""The accounts the LightDM greeter lets in without a password (D-99, D-155).
/usr/lib/biokids/biokids-child-login is what reads it."""

ADMIN_GROUPS = frozenset({"sudo", "adm", "biokids"})
"""Membership in any of these keeps an account out of CHILD_GROUP, whatever
else is true of it: it can administer the machine, read its logs, or grant
time. The login helper refuses them too."""


@dataclass
class ProvisionResult:
    """What happened, in enough detail for an audit entry."""

    unix_user: str
    account_existed: bool = False
    account_created: bool = False
    passwordless_login: bool = False
    files_written: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_detail(self) -> dict[str, object]:
        return {
            "unix_user": self.unix_user,
            "account_created": self.account_created,
            "passwordless_login": self.passwordless_login,
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


SBIN_DIRS: tuple[str, ...] = ("/usr/sbin", "/sbin", "/usr/local/sbin")
"""Where `adduser` lives. systemd's default PATH includes these, but a panel
started by hand from a login shell inherits one that usually does not."""


def _find(program: str) -> str | None:
    found = shutil.which(program)
    if found is not None:
        return found
    for directory in SBIN_DIRS:
        candidate = Path(directory) / program
        if candidate.is_file():
            return str(candidate)
    return None


def _run(argv: Sequence[str]) -> tuple[bool, str]:
    program = _find(argv[0])
    if program is None:
        return False, f"{argv[0]} was not found on PATH or in {', '.join(SBIN_DIRS)}"
    try:
        result = subprocess.run(
            [program, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[:300]
    return True, ""


def _output(argv: Sequence[str]) -> str | None:
    """stdout of a command that succeeded, or None."""
    program = _find(argv[0])
    if program is None:
        return None
    try:
        result = subprocess.run(
            [program, *argv[1:]],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def groups_of(unix_user: str) -> set[str] | None:
    """The account's groups, or None when they cannot be read."""
    output = _output(["id", "-nG", "--", unix_user])
    return set(output.split()) if output is not None else None


def account_is_administrative(unix_user: str) -> bool:
    """True for an account in any of ADMIN_GROUPS.

    Never a child's account: made one, it would hand a parent's own session to
    the kiosk, the quota and the power-off. Unreadable counts as no here, which
    is safe because `grant_passwordless_login` refuses what it cannot read.
    """
    return bool((groups_of(unix_user) or set()) & ADMIN_GROUPS)


def is_child_account(unix_user: str) -> bool:
    """True for an account BioKids has already made a child's - from an earlier
    install, say, whose database was purged while the accounts stayed."""
    return CHILD_GROUP in (groups_of(unix_user) or set())


def grant_passwordless_login(unix_user: str) -> tuple[bool, str]:
    """Let this child in at the greeter by their picture alone - when that lowers nothing.

    Returns (granted, reason). The account joins CHILD_GROUP only if it has no
    usable password already and is in none of ADMIN_GROUPS. An account BioKids
    created meets both by construction. An existing one might not: a parent who
    types their own user name as the child's must not find their own account
    one click from anyone at the login screen. Such an account keeps its
    password and logs in with it, which is a reason, not a failure.

    Anything this cannot read counts as a no.
    """
    groups = groups_of(unix_user)
    if groups is None:
        return False, f"could not read the groups of {unix_user}"
    admin = sorted(groups & ADMIN_GROUPS)
    if admin:
        return False, f"{unix_user} is in {', '.join(admin)}"
    if CHILD_GROUP in groups:
        return True, ""

    status_out = _output(["passwd", "--status", unix_user])
    fields = status_out.split() if status_out else []
    if len(fields) < 2:
        return False, f"could not read the password status of {unix_user}"
    # passwd(1): L locked, NP no password, P usable password.
    if fields[1] == "P":
        return False, f"{unix_user} has a password of its own"

    ok, problem = _run(["adduser", "--quiet", unix_user, CHILD_GROUP])
    if not ok:
        return False, problem
    log.info("%s can now log in at the greeter without a password", unix_user)
    return True, ""


def create_account(unix_user: str, display_name: str = "") -> tuple[bool, str]:
    """Create the child's Unix account, with nothing it should not have.

    No password: the login screen is a set of avatars and the real gate is the
    quota, not the login (D-99). No sudo and emphatically not `biokids` — that
    group can grant time and power the machine off (D-123). The one group a
    child does get, CHILD_GROUP, is added afterwards by
    `grant_passwordless_login`, which checks first.
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
            _make_owned(dst.parent, home, uid, gid)
            shutil.copyfile(src, dst)
            dst.chmod(0o644)
            _own(dst, uid, gid)
        except OSError as exc:
            problems.append(f"{dst}: {exc}")
            continue
        written.append(str(dst))

    return written, problems


def _make_owned(directory: Path, home: Path, uid: int, gid: int) -> None:
    """Create `directory` and give the child every level of it.

    Not just the leaf. `mkdir(parents=True)` runs as root and creates the whole
    chain as root, so writing `~/.config/autostart/biokids-shell.desktop` used
    to leave `~/.config` itself root-owned - and a child who cannot write
    `~/.config` cannot start a session, which from where they are sitting looks
    exactly like BioKids refusing to let them in.

    Stops at `home`, which already belongs to the child and is not ours to
    change.
    """
    directory.mkdir(parents=True, exist_ok=True)
    walked = directory
    while True:
        _own(walked, uid, gid)
        if walked == home or walked.parent == walked:
            return
        walked = walked.parent
        try:
            walked.relative_to(home)
        except ValueError:
            # Above the home directory: nothing up there is ours to touch.
            return


def _own(path: Path, uid: int, gid: int) -> None:
    chown = getattr(os, "chown", None)
    if chown is not None:
        chown(path, uid, gid)


PROVISION_UNIT = "biokids-provision@{user}.service"
"""The unit that does this on a packaged machine. See `provision_via_systemd`."""


def systemd_escape(value: str) -> str:
    """`%i` for a Unix user name.

    Usernames are `[a-z_][a-z0-9_-]*` (UNIX_USER_RE in the panel), and of those
    characters only `-` is special to systemd, escaped as `\x2d`. Everything
    else passes through, which keeps the unit name readable in `journalctl`.
    Anything outside that set never reaches here, because `provision_via_systemd`
    refuses it.
    """
    return value.replace("-", r"\x2d")


def provision_via_systemd(
    unix_user: str,
    *,
    timeout: float = 150.0,
) -> ProvisionResult | None:
    """Ask `biokids-provision@<user>.service` to do it, or return None.

    The panel runs under `ProtectSystem=strict` and `ProtectHome=yes`, so it
    cannot write /etc/passwd or the child's home directory itself, and widening
    that sandbox would trade a real bound on every other panel bug for this one
    feature. The privileged half lives in its own unit instead.

    Returns None when there is no systemd to ask - a developer running the panel
    by hand, the test suite, a container - so the caller can fall back to doing
    the work in-process, which is what `biokids-admin provision-child` does and
    what the unit itself ends up running.
    """
    if not UNIX_USER_RE.match(unix_user):
        # Never interpolated into a unit name unchecked. A name that cannot be a
        # Unix user cannot be a child either.
        return ProvisionResult(
            unix_user=unix_user, problems=[f"{unix_user!r} is not a valid user name"]
        )

    systemctl = _find("systemctl")
    if systemctl is None:
        return None

    unit = PROVISION_UNIT.format(user=systemd_escape(unix_user))
    try:
        finished = subprocess.run(
            [systemctl, "start", "--wait", unit],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ProvisionResult(unix_user=unix_user, problems=[f"{unit}: {exc}"])

    if finished.returncode != 0:
        detail = (finished.stderr or finished.stdout).strip()[:300]
        return ProvisionResult(
            unix_user=unix_user,
            problems=[f"{unit} failed: {detail or f'exit {finished.returncode}'}"],
        )

    # The unit ran the same code this module exposes, so its outcome is on disk:
    # read it back rather than parsing the journal.
    result = ProvisionResult(unix_user=unix_user)
    result.account_existed = account_exists(unix_user)
    result.account_created = result.account_existed
    if not result.account_existed:
        result.problems.append(f"{unit} reported success but {unix_user} still does not exist")
    return result


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

    if result.account_existed and account_is_administrative(unix_user):
        # A parent who names their own account as the child's. Writing the
        # session files would turn their desktop into the kiosk, and the daemon
        # would ration their time and power the machine off (D-155).
        result.problems.append(
            f"{unix_user} can administer this machine; BioKids will not make it a child's"
        )
        return result

    if not result.account_existed:
        if not create:
            result.problems.append(f"{unix_user} does not exist on this machine")
            return result
        created, problem = create_account(unix_user, display_name)
        result.account_created = created
        if not created:
            result.problems.append(f"could not create {unix_user}: {problem}")
            return result

    granted, reason = grant_passwordless_login(unix_user)
    result.passwordless_login = granted
    if not granted:
        if result.account_created:
            # An account BioKids made has no password, so without this the child
            # has no way in at all.
            result.problems.append(f"{unix_user} cannot log in by their picture: {reason}")
        else:
            log.info("%s keeps logging in with its own password: %s", unix_user, reason)

    written, problems = install_session_files(unix_user, config, overwrite=overwrite)
    result.files_written = written
    result.problems.extend(problems)
    return result
