"""D-172 — the fail-safe pieces that need no daemon around them.

The guarded clock, the watchdog notification, the DNS canary's wire format and
the lockdown's way of finding the children when the database is gone. The parts
that live in the supervisor loop are tested in test_daemon.py, next to the
fixtures that drive it.
"""

from __future__ import annotations

import datetime as dt
import struct
from typing import Any

import pytest

from biokids_core import admin
from biokids_core.clock import FakeClock, GuardedClock
from biokids_core.config import Config
from biokids_daemon import notify
from biokids_daemon.dnsfilter import FilterHealth
from biokids_daemon.dnsfilter import dns_query_blocked as real_dns_query_blocked
from biokids_daemon.sessions import SessionInfo, UserPresence

DAY = 24 * 3600


def guarded(trusted: bool = False) -> tuple[FakeClock, GuardedClock]:
    clock = FakeClock()
    return clock, GuardedClock(clock, threshold_seconds=600, trust_jump=lambda: trusted)


# ------------------------------------------------------------- the wall clock


def test_moving_the_date_forward_does_not_move_the_day() -> None:
    """Tomorrow's quota, today, by changing the date: the bug this closes."""
    clock, guard = guarded()
    before = guard.read().wall
    clock.advance(10)
    clock.jump_wall(DAY)

    after = guard.read().wall
    assert after.date() == before.date()
    assert after - before == dt.timedelta(seconds=10), "time still passes normally"
    [correction] = guard.take_corrections()
    assert not correction.trusted and correction.delta_seconds == pytest.approx(DAY)


def test_moving_it_back_does_not_reopen_a_finished_window_either() -> None:
    clock, guard = guarded()
    guard.read()
    clock.advance(10)
    clock.jump_wall(-3 * 3600)
    assert guard.read().wall == clock.wall + dt.timedelta(hours=3)


def test_a_jump_ntp_confirms_is_accepted() -> None:
    """A dead RTC battery and the first sync after boot: the new time is the right one."""
    clock, guard = guarded(trusted=True)
    guard.read()
    clock.advance(10)
    clock.jump_wall(DAY * 365)
    assert guard.read().wall == clock.wall
    assert guard.take_corrections()[0].trusted


def test_a_suspend_is_not_a_jump() -> None:
    """BOOTTIME runs through suspend, so the wall clock agrees with it."""
    clock, guard = guarded()
    guard.read()
    clock.suspend(8 * 3600)
    assert guard.read().wall == clock.wall
    assert guard.take_corrections() == []


def test_small_drift_is_left_alone() -> None:
    clock, guard = guarded()
    guard.read()
    clock.advance(10)
    clock.jump_wall(30)
    assert guard.read().wall == clock.wall


def test_a_new_boot_starts_from_the_clock_as_it_is() -> None:
    """A parent who sets the time on a machine without NTP gets it after a reboot."""
    clock, guard = guarded()
    guard.read()
    clock.reboot(off_for=DAY)
    assert guard.read().wall == clock.wall


# ------------------------------------------------------------------ watchdog


def test_notify_is_a_no_op_outside_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert notify.ready() is False
    assert notify.watchdog() is False


# --------------------------------------------------------------- filter state


@pytest.mark.parametrize(
    ("health", "is_open"),
    [
        (FilterHealth(redirect=True, filtering=True, answering=True), False),
        (FilterHealth(redirect=False, filtering=True, answering=True), True),
        (FilterHealth(redirect=True, filtering=False, answering=True), True),
        # Nothing answers: no DNS, so no internet. Closed, not open.
        (FilterHealth(redirect=True, filtering=False, answering=False), False),
    ],
)
def test_only_a_filter_that_lets_names_through_is_open(
    health: FilterHealth, is_open: bool
) -> None:
    assert health.open is is_open


def test_the_canary_reads_nxdomain_from_the_header(monkeypatch: pytest.MonkeyPatch) -> None:
    from biokids_daemon import dnsfilter

    sent: dict[str, Any] = {}

    class FakeSocket:
        def __init__(self, *_a: object) -> None:
            pass

        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_a: object) -> None:
            return None

        def settimeout(self, _t: float) -> None:
            pass

        def sendto(self, packet: bytes, address: tuple[str, int]) -> None:
            sent["packet"], sent["address"] = packet, address

        def recvfrom(self, _n: int) -> tuple[bytes, Any]:
            query_id = sent["packet"][:2]
            flags = struct.pack(">H", 0x8183)  # response, NXDOMAIN
            return query_id + flags + b"\0" * 8, None

    monkeypatch.setattr(dnsfilter.socket, "socket", FakeSocket)
    # The real function, imported before conftest replaced it for every test.
    assert real_dns_query_blocked("pornhub.com") is True
    assert sent["address"] == ("127.0.0.1", dnsfilter.DNS_PORT)
    assert b"\x07pornhub\x03com\x00" in sent["packet"]


# ------------------------------------------------------------ who is present


def a_session(**changes: Any) -> SessionInfo:
    values: dict[str, Any] = {
        "session_id": "3",
        "unix_user": "elif",
        "uid": 1001,
        "seat": "seat0",
        "active": False,
        "locked": False,
        "idle": False,
        "state": "online",
        "session_class": "user",
    }
    values.update(changes)
    return SessionInfo(**values)


def test_a_closing_session_is_nobody_at_the_machine() -> None:
    """KillUserProcesses=no leaves logged-out sessions lingering as `closing`."""
    presence = UserPresence("elif", (a_session(state="closing"),))
    assert not presence.present
    assert presence.lockable_session_ids == ()


def test_the_users_own_manager_is_not_a_login() -> None:
    presence = UserPresence("elif", (a_session(session_class="manager", seat=""),))
    assert not presence.present


# ------------------------------------------------------------------ lockdown


def test_lockdown_without_a_database_locks_the_picture_login_accounts(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARCHITECTURE.md section 9: a corrupt database fails closed."""
    locked: list[str] = []
    monkeypatch.setattr(admin, "_child_users", lambda _c: set())
    monkeypatch.setattr(admin, "group_members", lambda _g: {"elif"})
    monkeypatch.setattr(
        admin,
        "list_sessions",
        lambda: [
            {"session": "3", "user": "elif", "class": "user"},
            {"session": "2", "user": "anne", "class": "user"},
        ],
    )
    monkeypatch.setattr(admin, "lock_session", lambda sid: locked.append(sid) or True)
    monkeypatch.setattr(admin, "session_property", lambda _s, _n: "no")
    monkeypatch.setattr(admin, "_audit_lockdown", lambda *_a: None)

    assert admin.cmd_lockdown(None, config) == 0  # type: ignore[arg-type]
    assert locked == ["3"], "the parent's session is never locked"


def test_lockdown_ends_a_session_whose_screen_did_not_lock(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended: list[str] = []
    monkeypatch.setattr(admin, "_child_users", lambda _c: {"elif"})
    monkeypatch.setattr(
        admin, "list_sessions", lambda: [{"session": "3", "user": "elif", "class": "user"}]
    )
    monkeypatch.setattr(admin, "lock_session", lambda _sid: True)
    properties = {"Active": "yes", "LockedHint": "no"}
    monkeypatch.setattr(admin, "session_property", lambda _s, name: properties[name])
    monkeypatch.setattr(admin, "terminate_session", lambda sid: ended.append(sid) or True)
    monkeypatch.setattr(admin, "_audit_lockdown", lambda *_a: None)

    admin.cmd_lockdown(None, config)  # type: ignore[arg-type]
    assert ended == ["3"]


def test_lockdown_waits_for_a_slow_screen_locker(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """light-locker takes eight to ten seconds; the first version ended the
    session after three (found on the VM)."""
    ended: list[str] = []
    polls = {"n": 0}

    def active_until_third_look(_session: str, name: str) -> str:
        if name == "Active":
            polls["n"] += 1
            return "yes" if polls["n"] < 3 else "no"
        return "no"

    monkeypatch.setattr(admin, "LOCK_SETTLE_SECONDS", 30.0)
    monkeypatch.setattr(admin, "LOCK_POLL_SECONDS", 0.0)
    monkeypatch.setattr(admin, "_child_users", lambda _c: {"elif"})
    monkeypatch.setattr(
        admin, "list_sessions", lambda: [{"session": "3", "user": "elif", "class": "user"}]
    )
    monkeypatch.setattr(admin, "lock_session", lambda _sid: True)
    monkeypatch.setattr(admin, "session_property", active_until_third_look)
    monkeypatch.setattr(admin, "terminate_session", lambda sid: ended.append(sid) or True)
    monkeypatch.setattr(admin, "_audit_lockdown", lambda *_a: None)

    admin.cmd_lockdown(None, config)  # type: ignore[arg-type]
    assert ended == []
