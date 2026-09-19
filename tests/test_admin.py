"""`biokids-admin`, and the path that runs when everything else has failed.

`lockdown` is what `biokids-lockdown.service` invokes when the daemon dies
(D-106). It is the least-exercised code in the product and the most consequential:
by the time it runs, the thing that was supposed to be supervising a child is
gone, and this is the only thing standing between that and an unsupervised
machine.

So the behaviour asserted here is deliberately blunt. It locks the children's
sessions and nobody else's. It does not guess. If it cannot tell which users are
children it locks nothing and says so loudly, because locking a parent out of
their own machine during an incident is its own kind of failure.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from typing import Any
from unittest import mock

import pytest

from biokids_core import admin, repo, security
from biokids_core.config import Config
from biokids_core.db import AuditLog, Child, Database, Family, Parent, Policy


@pytest.fixture
def family_with_children(db: Database) -> None:
    with db.session() as session:
        session.add(Family())
        session.add(Parent(display_name="Anne", pin_hash=security.hash_secret("284913")))
        for name, user, year in (("Elif", "elif", 2020), ("Kerem", "kerem", 2017)):
            child = Child(unix_user=user, display_name=name, birth_year=year)
            session.add(child)
            session.flush()
            session.add(Policy(child_id=child.id))


class FakeLoginctl:
    """Stands in for loginctl. Records what was locked."""

    def __init__(self, sessions: list[dict[str, str]] | None = None) -> None:
        self.sessions = sessions if sessions is not None else []
        self.locked: list[str] = []
        self.fail_list = False
        self.fail_lock = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(admin, "list_sessions", self._list)
        monkeypatch.setattr(admin, "lock_session", self._lock)

    def _list(self) -> list[dict[str, str]]:
        return [] if self.fail_list else list(self.sessions)

    def _lock(self, session_id: str) -> bool:
        if self.fail_lock:
            return False
        self.locked.append(session_id)
        return True


def run_lockdown(config: Config) -> int:
    return admin.cmd_lockdown(argparse.Namespace(), config)


# ------------------------------------------------------------------ lockdown


def test_lockdown_locks_the_children_and_nobody_else(
    config: Config, db: Database, family_with_children: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    loginctl = FakeLoginctl(
        [
            {"session": "c1", "user": "elif"},
            {"session": "c2", "user": "kerem"},
            {"session": "p1", "user": "anne"},
            {"session": "g1", "user": "lightdm"},
        ]
    )
    loginctl.install(monkeypatch)

    assert run_lockdown(config) == 0

    assert sorted(loginctl.locked) == ["c1", "c2"]
    assert "p1" not in loginctl.locked, "the parent keeps their session"
    assert "g1" not in loginctl.locked, "so does the greeter"


def test_lockdown_writes_a_loud_audit_entry(
    config: Config, db: Database, family_with_children: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeLoginctl([{"session": "c1", "user": "elif"}]).install(monkeypatch)
    run_lockdown(config)

    with db.session() as session:
        entries = session.query(AuditLog).filter(AuditLog.action == "lockdown").all()
    assert len(entries) == 1
    assert entries[0].severity == "alert"
    assert entries[0].detail["sessions"] == ["c1"]


def test_lockdown_refuses_to_guess_when_it_cannot_read_the_child_list(
    config: Config, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable database is not a licence to lock whatever is logged in."""
    loginctl = FakeLoginctl([{"session": "p1", "user": "anne"}])
    loginctl.install(monkeypatch)
    monkeypatch.setattr(admin, "_child_users", lambda _config: set())

    assert run_lockdown(config) == 1
    assert loginctl.locked == []
    assert "could not determine which users are children" in caplog.text


def test_lockdown_with_nobody_logged_in_is_a_success_not_an_error(
    config: Config, db: Database, family_with_children: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    FakeLoginctl([]).install(monkeypatch)
    assert run_lockdown(config) == 0


def test_lockdown_still_reports_when_loginctl_refuses(
    config: Config, db: Database, family_with_children: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that failed must still be written down; someone has to know."""
    loginctl = FakeLoginctl([{"session": "c1", "user": "elif"}])
    loginctl.fail_lock = True
    loginctl.install(monkeypatch)

    assert run_lockdown(config) == 0
    with db.session() as session:
        entry = session.query(AuditLog).filter(AuditLog.action == "lockdown").one()
    assert entry.detail["sessions"] == []
    assert entry.detail["users"] == ["elif", "kerem"]


def test_lockdown_survives_an_audit_write_that_fails(
    config: Config, db: Database, family_with_children: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lockdown that could not be written down still happened.

    The database is the most likely thing to have caused the failure that
    brought the daemon down in the first place, so an unwritable audit log must
    not stop the screens locking. `Database` is broken here rather than
    `_audit_lockdown`, so this exercises the real error handling instead of a
    mock of it.
    """
    loginctl = FakeLoginctl([{"session": "c1", "user": "elif"}])
    loginctl.install(monkeypatch)

    real_database = admin.Database
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:  # the first call reads the child list; the second audits
            raise OSError("no space left on device")
        return real_database(*args, **kwargs)

    monkeypatch.setattr(admin, "Database", flaky)

    assert run_lockdown(config) == 0
    assert loginctl.locked == ["c1"], "the screens locked even though nothing was recorded"


# ------------------------------------------------------- reading from loginctl


def test_sessions_are_parsed_from_loginctl_json(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps(
        [
            {"session": "3", "uid": 1000, "user": "elif", "seat": "seat0"},
            {"session": "4", "uid": 1001, "user": "kerem", "seat": "seat0"},
        ]
    )
    monkeypatch.setattr(
        admin.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, payload, ""),
    )
    assert admin.list_sessions() == [
        {"session": "3", "user": "elif"},
        {"session": "4", "user": "kerem"},
    ]


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(["loginctl"], 1, "", "no such command"),
        subprocess.CompletedProcess(["loginctl"], 0, "not json at all", ""),
        subprocess.CompletedProcess(["loginctl"], 0, "", ""),
    ],
)
def test_a_broken_loginctl_yields_no_sessions_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, outcome: Any
) -> None:
    monkeypatch.setattr(admin.subprocess, "run", lambda *a, **k: outcome)
    assert admin.list_sessions() == []


def test_loginctl_being_absent_yields_no_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_a: Any, **_k: Any) -> Any:
        raise FileNotFoundError("loginctl")

    monkeypatch.setattr(admin.subprocess, "run", explode)
    assert admin.list_sessions() == []


# ----------------------------------------------------------------- reset-pin


def _reset(config: Config, pin: str) -> int:
    """Run reset-pin with the PIN arriving on standard input.

    A real pipe rather than a stubbed `read_new_pin`, so the reading half is
    covered too: it stopped being a positional argument because on a family
    machine `ps` and the shell history are readable by the child the PIN exists
    to stop, and a test that patches the reader would not notice it regressing.
    """
    with mock.patch.object(sys, "stdin", io.StringIO(f"{pin}\n")):
        return admin.cmd_reset_pin(argparse.Namespace(stdin=True), config)


def test_reset_pin_sets_a_new_pin_and_clears_the_lockout(
    config: Config, db: Database, family_with_children: None
) -> None:
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        parent.pin_failures = 4
        parent.pin_lockout_level = 2

    assert _reset(config, "551234") == 0

    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        assert security.verify_secret(parent.pin_hash, "551234")
        assert parent.pin_failures == 0
        assert parent.pin_locked_until is None
        assert parent.pin_lockout_level == 0
        actions = [row.action for row in session.query(AuditLog).all()]
    assert "pin_changed" in actions


def test_reset_pin_refuses_a_guessable_pin(
    config: Config, db: Database, family_with_children: None
) -> None:
    assert _reset(config, "111111") == 2
    with db.session() as session:
        parent = repo.get_parent(session)
        assert parent is not None
        assert security.verify_secret(parent.pin_hash, "284913"), "the old PIN still works"


def test_reset_pin_before_setup_says_so_rather_than_creating_a_parent(
    config: Config, db: Database
) -> None:
    assert _reset(config, "551234") == 1
    with db.session() as session:
        assert repo.get_parent(session) is None


def test_reset_pin_does_not_take_the_pin_on_the_command_line(
    config: Config, db: Database, family_with_children: None
) -> None:
    """`reset-pin 551234` must not be a thing a parent can type.

    Every process table on the machine shows a command line, including the
    child's session, and the shell writes it to history. The parser rejecting it
    is the whole fix, so the parser is what is tested.
    """
    parser = admin.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["reset-pin", "551234"])

    args = parser.parse_args(["reset-pin", "--stdin"])
    assert args.stdin is True
    assert not hasattr(args, "pin"), "no argv path for the PIN survives"


def test_reset_pin_asks_twice_and_refuses_a_typo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typing it once is how a parent locks themselves out of their own machine."""
    typed = iter(["551234", "551243"])
    monkeypatch.setattr(admin.getpass, "getpass", lambda _prompt: next(typed))
    with pytest.raises(ValueError, match="mismatch"):
        admin.read_new_pin(False)


def test_reset_pin_prompt_is_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    """It goes through getpass, not input(): a child may well be watching."""
    monkeypatch.setattr(admin.getpass, "getpass", lambda _prompt: "551234")
    monkeypatch.setattr(
        "builtins.input", lambda *_a: pytest.fail("the PIN was echoed to the screen")
    )
    assert admin.read_new_pin(False) == "551234"


# ------------------------------------------------------------ provision-child


def test_provision_child_reports_a_missing_account_rather_than_pretending(
    config: Config, db: Database, family_with_children: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-create on a machine with no such user is a failure, and says which.

    The development host has no `adduser` and no password database, which is
    exactly the shape of the failure this has to report honestly: the command
    must not print "done" over a child who still cannot log in.
    """
    code = admin.cmd_provision_child(
        argparse.Namespace(unix_user="nobody-here", name="", no_create=True, overwrite=False),
        config,
    )
    assert code == 1
    captured = capsys.readouterr()
    assert "nobody-here" in captured.err
    assert "does not exist" in captured.err


# --------------------------------------------------------------- sync-catalog


def test_sync_catalog_loads_the_applications(
    config: Config, db: Database, family_with_children: None
) -> None:
    assert admin.cmd_sync_catalog(argparse.Namespace(), config) == 0
    with db.session() as session:
        apps = repo.list_apps(session)
    assert any(app.slug == "tuxpaint" for app in apps)
    assert any(app.slug == "eba" for app in apps)


def test_sync_catalog_is_idempotent(
    config: Config, db: Database, family_with_children: None
) -> None:
    admin.cmd_sync_catalog(argparse.Namespace(), config)
    with db.session() as session:
        first = len(repo.list_apps(session))
    admin.cmd_sync_catalog(argparse.Namespace(), config)
    with db.session() as session:
        assert len(repo.list_apps(session)) == first


# ----------------------------------------------------------------- the parser


def test_every_subcommand_is_reachable() -> None:
    parser = admin.build_parser()
    for command in ("lockdown", "status", "reset-pin", "sync-catalog", "version"):
        args = parser.parse_args([command, "--stdin"] if command == "reset-pin" else [command])
        assert args.command == command


def test_a_subcommand_is_required() -> None:
    """`biokids-admin` alone must not silently do something."""
    with pytest.raises(SystemExit):
        admin.build_parser().parse_args([])
