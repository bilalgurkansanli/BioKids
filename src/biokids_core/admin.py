"""`biokids-admin` — the recovery and last-resort command line.

Two of its subcommands matter more than the rest.

`lockdown` is what `biokids-lockdown.service` runs when the daemon dies (D-106,
principle #3). It must work with the daemon gone, so it talks to `loginctl`
directly and touches the database only to find out which users are children and
to write one loud audit entry. If even that fails it still locks, because a
locked screen with no audit trail is better than an unsupervised machine.

`reset-pin` is one of the two documented ways back in when a parent forgets
their PIN (D-50). The other is the eight-word recovery code in the panel. There
is no email reset and no back door; a parental-control product with a vendor
override is not a parental-control product.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from biokids_core import repo, security
from biokids_core.catalog import load_catalog, sync_catalog
from biokids_core.config import Config, set_config
from biokids_core.db.session import Database
from biokids_core.i18n import translator
from biokids_core.ipc import DaemonClient, Op

log = logging.getLogger("biokids.admin")

LOGINCTL_TIMEOUT = 10


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    config = Config.load(args.config)
    set_config(config)

    handlers = {
        "lockdown": cmd_lockdown,
        "status": cmd_status,
        "reset-pin": cmd_reset_pin,
        "sync-catalog": cmd_sync_catalog,
        "version": cmd_version,
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects this first
        parser.print_help()
        return 2
    return handler(args, config)


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
    sub.add_parser("version", help="print the version")

    reset = sub.add_parser("reset-pin", help="set a new parent PIN (D-50)")
    reset.add_argument("pin", help="the new 6-digit PIN")

    return parser


# ------------------------------------------------------------------ lockdown


def list_sessions() -> list[dict[str, str]]:
    """Ask loginctl who is logged in, without needing the daemon or D-Bus bindings."""
    try:
        result = subprocess.run(
            ["loginctl", "list-sessions", "--output=json"],
            capture_output=True,
            text=True,
            timeout=LOGINCTL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.exception("loginctl is not answering")
        return []
    if result.returncode != 0:
        log.error("loginctl exited %s: %s", result.returncode, result.stderr.strip())
        return []
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        log.error("loginctl returned something that is not JSON")
        return []
    return [
        {"session": str(row.get("session", "")), "user": str(row.get("user", ""))}
        for row in rows
        if isinstance(row, dict)
    ]


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


def cmd_lockdown(_args: argparse.Namespace, config: Config) -> int:
    """Lock every child session. Never fails softly, never locks the parent."""
    child_users = _child_users(config)
    sessions = list_sessions()

    if not child_users:
        # The database is unreadable or empty. We will not guess which sessions
        # belong to children — locking a parent out of their own machine during
        # an incident is its own kind of failure.
        log.error(
            "lockdown could not determine which users are children; "
            "no session was locked. Check %s",
            config.paths.db_path,
        )
        return 1

    locked = [
        row["session"]
        for row in sessions
        if row["user"] in child_users and lock_session(row["session"])
    ]

    log.warning(
        "lockdown: locked %s session(s) for %s", len(locked), ", ".join(sorted(child_users))
    )
    _audit_lockdown(config, locked, sorted(child_users))
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


def cmd_reset_pin(args: argparse.Namespace, config: Config) -> int:
    try:
        pin = security.validate_pin(args.pin)
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
