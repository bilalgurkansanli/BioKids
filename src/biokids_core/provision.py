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

import contextlib
import errno
import logging
import os
import re
import shutil
import stat
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

AVATAR_DIR = Path(__file__).resolve().parent / "data" / "avatars"
"""The panel's avatars as pictures, for the login screen (D-170).
`dev/make_greeter_art.py avatars` draws them."""
DEFAULT_AVATAR = "fox"
ACCOUNTS_BUS = "org.freedesktop.Accounts"
_ACCOUNTS_USER_RE = re.compile(r'"(/org/freedesktop/Accounts/User[0-9]+)"')

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
    greeter_identity: bool = False
    """The login screen shows the child's name and picture (D-170). Not a
    problem when it fails: the child can still log in, under their user name."""
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
            "greeter_identity": self.greeter_identity,
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


def avatar_picture(avatar: str) -> Path:
    """The picture for a panel avatar slug, or the default one."""
    if avatar.isalnum():
        picture = AVATAR_DIR / f"{avatar}.png"
        if picture.is_file():
            return picture
    return AVATAR_DIR / f"{DEFAULT_AVATAR}.png"


def real_name(display_name: str) -> str:
    """The child's name as a GECOS field: commas separate its subfields and
    colons the fields of /etc/passwd, so neither may appear in it."""
    return " ".join(display_name.replace(",", " ").replace(":", " ").split())[:64]


def set_greeter_identity(unix_user: str, display_name: str, avatar: str) -> tuple[bool, str]:
    """Put the child's name and picture on the login screen (D-170).

    The greeter showed every child as their Unix user name, in lower case, next
    to Debian's grey silhouette: provisioning set neither, and the `.face` a new
    account gets from /etc/skel sits in a 0700 home the greeter cannot read. A
    four-year-old does not log in by reading "ahmet".

    The name goes into GECOS, where every greeter looks. The picture goes to
    AccountsService, which copies it to /var/lib/AccountsService/icons/ where
    LightDM's greeter can read it; without AccountsService there is nowhere a
    greeter will look that a child's home does not hide, and the child keeps
    the silhouette.
    """
    problems: list[str] = []
    name = real_name(display_name)
    if name:
        ok, problem = _run(["usermod", "--comment", name, unix_user])
        if not ok:
            problems.append(f"name: {problem}")
    found = _output(
        [
            "busctl",
            "call",
            ACCOUNTS_BUS,
            "/org/freedesktop/Accounts",
            ACCOUNTS_BUS,
            "FindUserByName",
            "s",
            unix_user,
        ]
    )
    match = _ACCOUNTS_USER_RE.search(found or "")
    if match is None:
        problems.append("picture: AccountsService did not answer")
    else:
        ok, problem = _run(
            [
                "busctl",
                "call",
                ACCOUNTS_BUS,
                match.group(1),
                f"{ACCOUNTS_BUS}.User",
                "SetIconFile",
                "s",
                str(avatar_picture(avatar)),
            ]
        )
        if not ok:
            problems.append(f"picture: {problem}")
    return not problems, "; ".join(problems)


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
    # Every autostart entry, not only the launcher's: `light-locker.desktop`
    # overrides the system one of the same name so that a child's screen locks
    # only when BioKids locks it (D-158).
    for entry in sorted(source.glob("*.desktop")):
        plan.append((entry, home / AUTOSTART_DIR / entry.name))

    for src, dst in plan:
        try:
            if _write_into_home(src, dst, home, uid, gid, overwrite=overwrite):
                written.append(str(dst))
        except OSError as exc:
            problems.append(f"{dst}: {exc}")

    return written, problems


# POSIX-only names, read through getattr for the same reason as `pwd` above.
_O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
_O_DIRECTORY: int = getattr(os, "O_DIRECTORY", 0)
_O_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC
_SAFE_OPEN = os.open in os.supports_dir_fd and _O_NOFOLLOW != 0
"""Whether this host can walk a directory tree without following symlinks.

Every Linux can. The Windows host this is developed on cannot, and has no
child accounts to protect either; there the plain path below is used."""


def _write_into_home(
    src: Path, dst: Path, home: Path, uid: int, gid: int, *, overwrite: bool
) -> bool:
    """Copy `src` to `dst` inside the child's home as root, safely. True if written.

    This runs as root in a directory the child owns, which is the textbook
    setting for a symlink attack: a child who makes `~/.config` a link to
    `/etc` would have had root create directories there and then chown `/etc`
    to them, and a dangling link in `~/.config/autostart` would have had root
    create a file anywhere on the disk. So nothing here follows a link. Each
    level is opened relative to the one above with O_NOFOLLOW, created when it
    is missing, and given to the child through its file descriptor; the file
    itself is opened the same way and refused if it is anything but a regular
    file with one name. A link where a directory should be is an error the
    parent sees, not a path root walks down.

    Every level is owned by the child, not just the leaf: `mkdir` as root
    creates root-owned directories, and a child who cannot write `~/.config`
    cannot start a session at all.
    """
    relative = dst.relative_to(home)
    if not _SAFE_OPEN:
        if dst.exists() and not overwrite:
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)
        walked = dst.parent
        while walked != home and home in walked.parents:
            _own(walked, None, uid, gid)
            walked = walked.parent
        shutil.copyfile(src, dst)
        dst.chmod(0o644)
        _own(dst, None, uid, gid)
        return True

    data = src.read_bytes()
    directory = os.open(home, _DIR_FLAGS)
    try:
        walked = home
        for part in relative.parent.parts:
            walked = walked / part
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o755, dir_fd=directory)
            # ELOOP or ENOTDIR here is a link or a file where a directory
            # belongs; the OSError reaches the caller as a problem.
            inner = os.open(
                part,
                _DIR_FLAGS,
                dir_fd=directory,
            )
            os.close(directory)
            directory = inner
            _own(walked, directory, uid, gid)

        name = relative.name
        try:
            existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not overwrite:
            return False

        target = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC,
            0o644,
            dir_fd=directory,
        )
        try:
            info = os.fstat(target)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                # A hard link to someone else's file would be truncated as root.
                raise OSError(f"{dst} is not a plain file; refusing to write it")
            os.ftruncate(target, 0)
            os.write(target, data)
            os.fchmod(target, 0o644)
            _own(dst, target, uid, gid)
        finally:
            os.close(target)
    finally:
        os.close(directory)
    return True


def _own(path: Path, fd: int | None, uid: int, gid: int) -> None:
    """Give `path` to the child: through `fd` when there is one, so it is the
    file that was opened and not whatever a name points at now."""
    fchown = getattr(os, "fchown", None)
    if fd is not None and fchown is not None:
        fchown(fd, uid, gid)
        return
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
    avatar: str = "",
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
    if granted:
        # A child removed and added again (D-191).
        restored, problem = restore_login_shell(unix_user)
        if not restored:
            result.problems.append(
                f"{unix_user} is still hidden from the login screen: {problem}"
            )
    else:
        if result.account_created:
            # An account BioKids made has no password, so without this the child
            # has no way in at all.
            result.problems.append(f"{unix_user} cannot log in by their picture: {reason}")
        else:
            log.info("%s keeps logging in with its own password: %s", unix_user, reason)

    if display_name or avatar:
        shown, why = set_greeter_identity(unix_user, display_name, avatar)
        result.greeter_identity = shown
        if not shown:
            log.warning("%s keeps their user name on the login screen: %s", unix_user, why)

    written, problems = install_session_files(unix_user, config, overwrite=overwrite)
    result.files_written = written
    result.problems.extend(problems)
    return result


# ------------------------------------------------------------ removing a child

UNPROVISION_UNIT = "biokids-unprovision@{user}.service"
"""The unit that does this on a packaged machine, as PROVISION_UNIT for adding."""

HIDDEN_SHELL = "/usr/sbin/nologin"
"""A removed child's shell. AccountsService counts an account with a nologin
shell as a system account and LightDM leaves it off the login screen, so a
removed child's picture is not left there leading nowhere (D-191)."""
NOLOGIN_SHELLS = frozenset(
    {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false"}
)
CHILD_SHELL = "/bin/bash"
"""What adduser gives a new account, and what a child added again gets back."""


def login_shell(unix_user: str) -> str:
    entry = _passwd_entry(unix_user)
    return str(getattr(entry, "pw_shell", "") or "") if entry is not None else ""


def restore_login_shell(unix_user: str) -> tuple[bool, str]:
    """Undo `unprovision`'s HIDDEN_SHELL for a child added back (D-191)."""
    if login_shell(unix_user) not in NOLOGIN_SHELLS:
        return True, ""
    return _run(["usermod", "--shell", CHILD_SHELL, unix_user])


def remove_session_files(
    unix_user: str, config: Config | None = None
) -> tuple[list[str], list[str]]:
    """Remove the session files `install_session_files` put in the child's home.

    Only a file that is still exactly what BioKids wrote: an account that was
    somebody's before it was a child's may have had an xfwm4.xml of its own,
    which provisioning left alone and this must leave alone too. Root works in
    a directory the child owns, so - as when writing - nothing here follows a
    link. Nothing else in the home is touched: a child's drawings stay theirs.
    """
    config = config or get_config()
    source = config.paths.share_dir / "xfce"
    home = home_of(unix_user)
    removed: list[str] = []
    problems: list[str] = []
    if home is None or not source.is_dir():
        return removed, problems
    shipped = [(XFCE_CHANNEL_DIR / xml.name, xml) for xml in sorted(source.glob("*.xml"))]
    shipped += [
        (AUTOSTART_DIR / entry.name, entry) for entry in sorted(source.glob("*.desktop"))
    ]
    for relative, original in shipped:
        try:
            if _remove_from_home(home, relative, original.read_bytes()):
                removed.append(str(home / relative))
        except OSError as exc:
            problems.append(f"{home / relative}: {exc}")
    return removed, problems


def _remove_from_home(home: Path, relative: Path, expected: bytes) -> bool:
    """Unlink `home/relative` if it is a regular file holding `expected`."""
    if not _SAFE_OPEN:
        target = home / relative
        if target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
            return False
        target.unlink()
        return True
    try:
        directory = os.open(home, _DIR_FLAGS)
    except FileNotFoundError:
        return False
    try:
        for part in relative.parent.parts:
            try:
                inner = os.open(part, _DIR_FLAGS, dir_fd=directory)
            except (FileNotFoundError, NotADirectoryError):
                return False
            os.close(directory)
            directory = inner
        try:
            fd = os.open(
                relative.name, os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC, dir_fd=directory
            )
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                # A link where our file was is the child's own doing, and with
                # the account off the login screen it leads nowhere.
                return False
            raise
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return False
            content = os.read(fd, len(expected) + 1)
        finally:
            os.close(fd)
        if content != expected:
            return False
        os.unlink(relative.name, dir_fd=directory)
        return True
    finally:
        os.close(directory)


def unprovision(unix_user: str, config: Config | None = None) -> ProvisionResult:
    """What removing a child from BioKids does to their account (D-191).

    The account and its home stay - removing a child from the panel must not
    delete their drawings - but an account BioKids made passwordless leaves
    CHILD_GROUP, so no picture lets anyone in, and gets HIDDEN_SHELL, so the
    picture is not on the login screen either. The session files go where they
    are still BioKids' own. An account that kept its own password keeps
    everything but those files.
    """
    if not UNIX_USER_RE.match(unix_user):
        return ProvisionResult(
            unix_user=unix_user, problems=[f"{unix_user!r} is not a valid user name"]
        )
    result = ProvisionResult(unix_user=unix_user, account_existed=account_exists(unix_user))
    if not result.account_existed:
        return result
    groups = groups_of(unix_user)
    if groups is None:
        result.problems.append(f"could not read the groups of {unix_user}")
        return result
    if CHILD_GROUP in groups:
        for argv in (
            ["gpasswd", "--delete", unix_user, CHILD_GROUP],
            ["usermod", "--shell", HIDDEN_SHELL, unix_user],
        ):
            ok, problem = _run(argv)
            if not ok:
                result.problems.append(f"{argv[0]}: {problem}")
    removed, problems = remove_session_files(unix_user, config)
    result.files_written = removed
    result.problems.extend(problems)
    if result.ok:
        log.info("%s is no longer a BioKids child; the account and its home stay", unix_user)
    return result


def unprovision_via_systemd(
    unix_user: str, *, timeout: float = 120.0
) -> ProvisionResult | None:
    """Ask `biokids-unprovision@<user>.service` to do it, or return None when
    there is no systemd to ask - the same arrangement as `provision_via_systemd`."""
    if not UNIX_USER_RE.match(unix_user):
        return ProvisionResult(
            unix_user=unix_user, problems=[f"{unix_user!r} is not a valid user name"]
        )
    systemctl = _find("systemctl")
    if systemctl is None:
        return None
    unit = UNPROVISION_UNIT.format(user=systemd_escape(unix_user))
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
    return ProvisionResult(unix_user=unix_user, account_existed=account_exists(unix_user))
