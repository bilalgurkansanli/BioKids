"""`biokids-admin` — the recovery and last-resort command line.

Two of its subcommands matter more than the rest.

`lockdown` is what `biokids-lockdown.service` runs when the daemon dies (D-106,
principle #3). It must work with the daemon gone, so it talks to `loginctl`
directly and touches the database only to find out which users are children and
to write one loud audit entry. If even that fails it still locks, because a
locked screen with no audit trail is better than an unsupervised machine.

`provision-child` is the repair path for the gap between a row in the database
and a user who can actually log in. The panel provisions a child when a parent
adds one; this is for when that could not happen, or happened before
biokids-shell was installed.

`reset-pin` is one of the two documented ways back in when a parent forgets
their PIN (D-50). The other is the eight-word recovery code in the panel. There
is no email reset and no back door; a parental-control product with a vendor
override is not a parental-control product.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import importlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from biokids_core import repo, security
from biokids_core.catalog import load_catalog, sync_catalog
from biokids_core.config import Config, set_config
from biokids_core.db.session import Database
from biokids_core.errors import ConfigError
from biokids_core.i18n import translator
from biokids_core.ipc import DaemonClient, Op
from biokids_core.provision import (
    CHILD_GROUP,
    account_exists,
    provision,
    set_greeter_identity,
)

log = logging.getLogger("biokids.admin")

LOGINCTL_TIMEOUT = 10

LOCK_SETTLE_SECONDS = 20.0
"""How long a screen locker gets to take the screen before the lockdown ends the
session. light-locker fades and hands the seat to the greeter in eight to ten
seconds on the development VM; three, the first value, ended a session that
was about to lock."""
LOCK_POLL_SECONDS = 1.0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = _load_config_or_default(args.config, args.command)
    set_config(config)

    handlers = {
        "lockdown": cmd_lockdown,
        "status": cmd_status,
        "reset-pin": cmd_reset_pin,
        "sync-catalog": cmd_sync_catalog,
        "provision-child": cmd_provision_child,
        "refresh-greeter": cmd_refresh_greeter,
        "remote-setup": cmd_remote_setup,
        "backup-db": cmd_backup_db,
        "version": cmd_version,
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects this first
        parser.print_help()
        return 2
    return handler(args, config)


def _load_config_or_default(path: Path | None, command: str) -> Config:
    """The configuration, or the defaults when the file will not parse.

    `lockdown` is what `biokids-lockdown.service` runs when the daemon has died
    (D-106), and one of the commonest ways for the daemon to die is a parent
    making a typo in `/etc/biokids/config.toml` - a documented conffile they are
    invited to edit. That typo used to take the backstop down with it: `main()`
    called `Config.load()` before any handler ran, `ConfigError` escaped, and no
    session was locked and nothing was written to the audit log. The child kept
    playing, unsupervised, and the parent's audit screen showed nothing at all.

    So lockdown falls back to the built-in defaults. It needs almost nothing
    from the file - the database path, to find out who the children are - and
    locking against the default path is worth far more than not locking. Every
    other subcommand still refuses to run on a file it cannot read, because
    guessing is the wrong answer when nobody is in danger.
    """
    try:
        return Config.load(path)
    except ConfigError:
        if command != "lockdown":
            raise
        log.error(
            "the configuration could not be read; locking with built-in defaults. "
            "This is the D-106 backstop: it runs precisely because something else "
            "is already broken, so it must not need the thing that broke.",
            exc_info=True,
        )
        return Config()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="biokids-admin",
        description="BioKids administration. Run as root.",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "lockdown",
        help="lock every child session immediately; run when the daemon has failed",
    )
    sub.add_parser("status", help="show what the daemon thinks is happening")
    sub.add_parser("sync-catalog", help="load the application catalog into the database")
    sub.add_parser(
        "remote-setup",
        help="install Tailscale if needed, sign in, and publish the panel (D-169)",
    )
    sub.add_parser(
        "refresh-greeter",
        help="show every child's name and picture on the login screen (D-170)",
    )
    sub.add_parser("version", help="print the version")

    backup = sub.add_parser(
        "backup-db",
        help="copy the database next to it, as biokids.db's postinst does before a migration",
    )
    backup.add_argument("--keep", type=int, default=3, help="how many copies to keep")
    backup.add_argument(
        "--reason", default="manual", help="a word for the file name, such as 'upgrade'"
    )

    reset = sub.add_parser("reset-pin", help="set a new parent PIN (D-50)")
    # Read from the terminal, not from argv: an argument is visible to every
    # user on the machine in `ps`, lands in ~/.bash_history, and on a family
    # machine the other user is the child the PIN exists to stop. --stdin is
    # for scripts, which is the one case where a pipe is safer than a prompt.
    reset.add_argument(
        "--stdin",
        action="store_true",
        help="read the new PIN from standard input instead of prompting",
    )

    provision = sub.add_parser(
        "provision-child",
        help="create the child's Unix account and session configuration",
    )
    provision.add_argument("unix_user", help="the child's Unix user name")
    provision.add_argument("--name", default="", help="display name for the new account")
    provision.add_argument(
        "--no-create",
        action="store_true",
        help="repair the session files only; fail if the account does not exist",
    )
    provision.add_argument(
        "--overwrite",
        action="store_true",
        help="replace session files the child or a parent has since edited",
    )

    return parser


# ------------------------------------------------------------------ lockdown


LIST_SESSIONS_JSON = ("loginctl", "list-sessions", "--json=short", "--no-pager")
"""`--json=`, not `--output=json`. For loginctl, `--output=` is the journal
format of `session-status`, so `--output=json` printed the ordinary table, the
JSON parse failed, and the lockdown locked nothing - on every machine, since the
day it was written (D-155). The tests fed it JSON, so they could not tell."""

LIST_SESSIONS_TABLE = ("loginctl", "list-sessions", "--no-legend", "--no-pager")
"""The fallback for a systemd without `--json`: SESSION UID USER first, in
every version, whatever columns follow."""


def list_sessions() -> list[dict[str, str]]:
    """Ask loginctl who is logged in, without needing the daemon or D-Bus bindings.

    JSON first; the plain table if this loginctl cannot do JSON. The table is
    read under LC_ALL=C so a translated header cannot change it, although with
    --no-legend there is none.
    """
    output = _loginctl_output(LIST_SESSIONS_JSON)
    if output is not None:
        try:
            rows = json.loads(output or "[]")
        except json.JSONDecodeError:
            log.warning("loginctl --json gave something else; reading the table instead")
        else:
            return [
                {
                    "session": str(row.get("session", "")),
                    "user": str(row.get("user", "")),
                    "class": str(row.get("class") or ""),
                }
                for row in rows
                if isinstance(row, dict)
            ]

    output = _loginctl_output(LIST_SESSIONS_TABLE)
    if output is None:
        return []
    sessions: list[dict[str, str]] = []
    for line in output.splitlines():
        fields = line.split()
        # A real row has a numeric UID second; anything else is not a session.
        if len(fields) >= 3 and fields[1].isdigit():
            sessions.append({"session": fields[0], "user": fields[2], "class": ""})
    return sessions


def _loginctl_output(argv: Sequence[str]) -> str | None:
    """stdout of a loginctl call, or None when it could not be had."""
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=LOGINCTL_TIMEOUT,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("loginctl is not answering")
        return None
    if result.returncode != 0:
        log.error("loginctl exited %s: %s", result.returncode, result.stderr.strip())
        return None
    return result.stdout


def lock_session(session_id: str) -> bool:
    try:
        result = subprocess.run(
            ["loginctl", "lock-session", session_id],
            capture_output=True,
            text=True,
            timeout=LOGINCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("could not lock session %s", session_id)
        return False
    return result.returncode == 0


def session_property(session_id: str, name: str) -> str:
    output = _loginctl_output(
        ("loginctl", "show-session", session_id, f"--property={name}", "--value")
    )
    return (output or "").strip()


def terminate_session(session_id: str) -> bool:
    return _loginctl_output(("loginctl", "terminate-session", session_id)) is not None


def _still_on_screen(session_id: str) -> bool:
    """On screen and not locked. light-locker never sets LockedHint (D-155); it
    locks by handing the seat to the greeter, which makes the session inactive."""
    if session_property(session_id, "Active") != "yes":
        return False
    return session_property(session_id, "LockedHint") != "yes"


def group_members(group: str) -> set[str]:
    """The members of a Unix group, or nothing on a host without `grp`."""
    try:
        getgrnam = importlib.import_module("grp").getgrnam
    except ImportError:  # the Windows development host
        return set()
    try:
        return set(getgrnam(group).gr_mem)
    except KeyError:
        return set()


def cmd_lockdown(_args: argparse.Namespace, config: Config) -> int:
    """Lock every child session. Never fails softly, never locks the parent."""
    child_users = _child_users(config)
    sessions = list_sessions()

    if not child_users:
        # The database is unreadable - which is one of the likeliest reasons
        # to be here at all (ARCHITECTURE.md section 9: "DB corrupt: fail
        # closed"). The accounts that log in by their picture are children by
        # construction (D-155): provisioning adds only password-less accounts
        # outside every administrative group, and nothing else adds anyone.
        # That is not a guess about who is a child.
        child_users = group_members(CHILD_GROUP)
        if child_users:
            log.error(
                "the child list could not be read from %s; locking the members of %s",
                config.paths.db_path,
                CHILD_GROUP,
            )
    if not child_users:
        log.error(
            "lockdown could not determine which users are children; "
            "no session was locked. Check %s",
            config.paths.db_path,
        )
        return 1

    # A child's `manager` and `background` sessions have no screen to lock, and
    # counting them would make "locked 1 of 3" look like a failure.
    targets = [
        row["session"]
        for row in sessions
        if row["user"] in child_users
        and not row.get("class", "").startswith(("manager", "background"))
    ]
    locked = [session_id for session_id in targets if lock_session(session_id)]
    if locked:
        # `loginctl lock-session` only asks the child's screen locker. One that
        # is not running leaves the child on screen; end that session instead.
        waiting = set(locked)
        deadline = time.monotonic() + LOCK_SETTLE_SECONDS
        while waiting:
            waiting = {sid for sid in waiting if _still_on_screen(sid)}
            if not waiting or time.monotonic() >= deadline:
                break
            time.sleep(LOCK_POLL_SECONDS)
        for session_id in sorted(waiting):
            log.error("session %s did not lock; ending it", session_id)
            if not terminate_session(session_id):
                locked.remove(session_id)

    log.warning(
        "lockdown: locked %s session(s) for %s", len(locked), ", ".join(sorted(child_users))
    )
    _audit_lockdown(config, locked, sorted(child_users))
    if len(locked) < len(targets):
        # Loud, so that the unit fails and the journal says so: a child session
        # this could not lock is the very case principle #3 is about.
        log.error(
            "lockdown: %s child session(s) could not be locked: %s",
            len(targets) - len(locked),
            ", ".join(s for s in targets if s not in locked),
        )
        return 1
    return 0


def _child_users(config: Config) -> set[str]:
    try:
        database = Database(config)
        with database.session() as session:
            users = {child.unix_user for child in repo.list_children(session)}
        database.dispose()
    except Exception:  # the database may be the thing that broke
        log.exception("could not read the child list")
        return set()
    return users


def _audit_lockdown(config: Config, locked: Sequence[str], users: Sequence[str]) -> None:
    """Best effort. A lockdown that could not be written down still happened."""
    try:
        database = Database(config)
        with database.session() as session:
            repo.audit(
                session,
                actor="system",
                action="lockdown",
                detail={"sessions": list(locked), "users": list(users)},
                severity="alert",
            )
        database.dispose()
    except Exception:
        log.exception("the lockdown could not be written to the audit log")


# ---------------------------------------------------------------- backup-db

BACKUP_DIR_NAME = "backups"
_BACKUP_NAME = re.compile(r"^biokids-\d{8}T\d{6}Z-[a-z0-9-]+\.db$")


def backup_database(config: Config, *, keep: int = 3, reason: str = "manual") -> Path | None:
    """Copy the database with SQLite's online backup; return the copy, or None
    when there is no database yet.

    Online backup is consistent while the daemon is writing, unlike a file copy
    of a WAL database. Copies are 0600 in a 0700 directory beside the database,
    because they hold exactly what it holds. Only the newest `keep` remain.
    """
    source = config.paths.db_path
    if not source.exists():
        return None
    directory = source.parent / BACKUP_DIR_NAME
    directory.mkdir(mode=0o700, exist_ok=True)
    tag = re.sub(r"[^a-z0-9-]+", "-", reason.lower()).strip("-") or "manual"
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"biokids-{stamp}-{tag}.db"

    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        # Created empty and 0600 before a byte of the family's data is in it.
        target.touch(mode=0o600)
        writer = sqlite3.connect(target)
        try:
            reader.backup(writer)
        finally:
            writer.close()
    finally:
        reader.close()

    copies = sorted(p for p in directory.iterdir() if _BACKUP_NAME.match(p.name))
    for old in copies[: max(0, len(copies) - max(keep, 1))]:
        old.unlink(missing_ok=True)
    return target


def cmd_backup_db(args: argparse.Namespace, config: Config) -> int:
    """Fails loudly: the postinst runs it before a migration, and migrating a
    database there is no copy of is the thing it exists to prevent."""
    try:
        copy = backup_database(config, keep=args.keep, reason=args.reason)
    except (OSError, sqlite3.Error) as exc:
        log.error("could not back up %s: %s", config.paths.db_path, exc)
        return 1
    if copy is None:
        log.info("no database at %s yet; nothing to back up", config.paths.db_path)
    else:
        log.info("database copied to %s", copy)
    return 0


# -------------------------------------------------------------------- status


def cmd_status(_args: argparse.Namespace, config: Config) -> int:
    client = DaemonClient(config.paths.daemon_socket)
    try:
        response = client.call(Op.GET_STATUS)
    except Exception as exc:
        print(f"the daemon is not answering on {config.paths.daemon_socket}: {exc}")
        return 1
    if not response.ok:
        print(f"the daemon refused: {response.error}")
        return 1

    children = response.data.get("children", [])
    if not children:
        print("no children are configured")
        return 0
    for child in children:
        remaining = int(child.get("remaining_seconds", 0)) // 60
        state = "online" if child.get("online") else "offline"
        app = child.get("app") or "-"
        print(
            f"{child.get('display_name', '?'):<12} {state:<8} {remaining:>4} min left   {app}"
        )
    return 0


# ----------------------------------------------------------------- reset-pin


def read_new_pin(from_stdin: bool) -> str:
    """Ask for the PIN twice, or read one line when a script is driving.

    `getpass` keeps it off the screen of a machine whose whole point is that a
    child is sometimes sitting at it.
    """
    if from_stdin:
        return sys.stdin.readline().strip()
    first = getpass.getpass("New PIN: ")
    second = getpass.getpass("Repeat it: ")
    if first != second:
        raise ValueError("wizard.parent.pin.mismatch")
    return first


def cmd_reset_pin(args: argparse.Namespace, config: Config) -> int:
    try:
        pin = security.validate_pin(read_new_pin(args.stdin))
    except ValueError as exc:
        print(translator("en")(str(exc)))
        return 2

    database = Database(config)
    try:
        with database.session() as session:
            parent = repo.get_parent(session)
            if parent is None:
                print("no parent account exists yet; run the setup wizard first")
                return 1
            parent.pin_hash = security.hash_secret(pin)
            parent.pin_failures = 0
            parent.pin_locked_until = None
            parent.pin_lockout_level = 0
            repo.audit(
                session,
                actor="cli",
                action="pin_changed",
                detail={"via": "biokids-admin reset-pin"},
                severity="warning",
            )
    finally:
        database.dispose()

    print("the PIN has been changed, and the change is in the audit log")
    return 0


# ---------------------------------------------------------- provision-child


def cmd_remote_setup(args: argparse.Namespace, config: Config) -> int:
    """What `biokids-remote-setup.service` runs when a parent asks for phone access.

    Prints each step for the journal, records the outcome in the audit log, and
    keeps the Serve approval page (D-157) where the panel looks for it.
    """
    from biokids_core import remote, tailscale_setup

    _ = args
    result = tailscale_setup.setup(config.panel.port)
    for step in result.steps:
        print(f"ran {step}")
    published = result.published
    approval = published.approval_url if published else ""
    if result.ok:
        print("the panel is published to the family's tailnet")
    elif approval:
        print(f"Tailscale asks its owner to allow Serve once: {approval}")
    else:
        print(f"problem: {result.error}", file=sys.stderr)

    database = Database(config)
    try:
        with database.session() as session:
            repo.set_setting(session, remote.SETTING_APPROVAL_URL, approval)
            repo.audit(
                session,
                actor="system",
                action="remote_setup",
                detail={
                    "installed": result.installed,
                    "logged_in": result.logged_in,
                    "ok": result.ok,
                    "approval": bool(approval),
                    "error": result.error[:300],
                },
                severity="info" if result.ok or approval else "warning",
            )
            session.commit()
    except Exception as exc:  # pragma: no cover - the outcome is in the journal anyway
        log.warning("could not record the outcome: %s", exc)
    finally:
        database.dispose()
    return 0 if result.ok or approval else 1


def cmd_refresh_greeter(args: argparse.Namespace, config: Config) -> int:
    """Give every child's account their name and picture on the login screen.

    Provisioning does this for a child added from now on (D-170); this is for
    the children added before, and runs from biokids-core's postinst. Never
    fails the upgrade: a machine without AccountsService keeps the user names.
    """
    _ = args
    database = Database(config)
    try:
        with database.session() as session:
            children = [
                (child.unix_user, child.display_name, child.avatar)
                for child in repo.list_children(session)
            ]
    except Exception as exc:  # pragma: no cover - no database yet is nothing to refresh
        log.warning("could not read the children: %s", exc)
        return 0
    finally:
        database.dispose()
    for unix_user, display_name, avatar in children:
        if not account_exists(unix_user):
            continue
        shown, why = set_greeter_identity(unix_user, display_name, avatar)
        print(f"{unix_user}: {'name and picture set' if shown else why}")
    return 0


def cmd_provision_child(args: argparse.Namespace, config: Config) -> int:
    """Give a child row an account and a session to log in to.

    The panel does this when a parent adds a child. This command exists for the
    case the panel could not: a machine provisioned before biokids-shell was
    installed, a home directory restored from a backup without its dotfiles, or
    a child added by editing the database. Running it again is harmless.
    """
    # The unit the panel starts knows only the user name (`%I`); the name and
    # the picture for the login screen are in the child's row (D-170).
    name, avatar = args.name, ""
    database = Database(config)
    try:
        with database.session() as session:
            child = repo.get_child_by_user(session, args.unix_user)
            if child is not None:
                name = name or child.display_name
                avatar = child.avatar
    except Exception as exc:  # pragma: no cover - no database is no reason not to provision
        log.warning("could not read %s's row: %s", args.unix_user, exc)
    finally:
        database.dispose()

    result = provision(
        args.unix_user,
        name,
        config,
        avatar=avatar,
        create=not args.no_create,
        overwrite=args.overwrite,
    )

    if result.account_created:
        print(f"created the account {result.unix_user}")
    elif result.account_existed:
        print(f"{result.unix_user} already exists; leaving the account alone")

    for written in result.files_written:
        print(f"wrote {written}")
    if not result.files_written and not result.problems:
        print("the session configuration was already in place")

    for problem in result.problems:
        print(f"problem: {problem}", file=sys.stderr)
    if not result.ok:
        return 1

    database = Database(config)
    try:
        with database.session() as session:
            child = repo.get_child_by_user(session, result.unix_user)
            repo.audit(
                session,
                actor="cli",
                action="child_provisioned",
                target=f"child:{child.id}" if child else "",
                detail=result.as_detail(),
            )
            if child is None:
                print(
                    f"note: no child in the database uses {result.unix_user}. "
                    "The account is ready, but nothing supervises it until a "
                    "parent adds the child in the panel.",
                    file=sys.stderr,
                )
    except Exception as exc:  # pragma: no cover - the database is optional here
        # The account and its files are already correct; a database that is not
        # set up yet must not turn a successful provision into a failure.
        log.warning("could not write the audit entry: %s", exc)
    finally:
        database.dispose()

    return 0


# ------------------------------------------------------------- sync-catalog


def cmd_sync_catalog(_args: argparse.Namespace, config: Config) -> int:
    database = Database(config)
    try:
        catalog = load_catalog(config=config)
        with database.session() as session:
            added, updated, gone = sync_catalog(session, catalog)
    finally:
        database.dispose()
    print(f"catalog: {added} added, {updated} updated, {gone} no longer available")
    return 0


def cmd_version(_args: argparse.Namespace, _config: Config) -> int:
    from biokids_core import __version__

    print(__version__)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
