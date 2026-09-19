"""What "the child is using the computer" means.

`ARCHITECTURE.md` §3.2 lists five conditions a tick must satisfy, and four of
them are questions about a logind session. This is where those questions get
their answers, and where several sessions collapse into the one boolean the
ledger is given — which is what makes "counted once, not twice" a property of
the interface rather than something the ledger has to defend.

Every scenario here is one a real machine produces: a screen lock, a fast user
switch, a greeter session sitting on the same seat, an SSH login, a leftover
session that logind has not reaped.
"""

from __future__ import annotations

import pytest

from biokids_daemon.sessions import (
    FakeSessionSource,
    SessionInfo,
    SessionWatcher,
    UserPresence,
)


def a_session(**overrides: object) -> SessionInfo:
    base: dict[str, object] = {
        "session_id": "s1",
        "unix_user": "elif",
        "uid": 1000,
        "seat": "seat0",
        "active": True,
        "locked": False,
        "idle": False,
        "state": "active",
        "session_class": "user",
        "session_type": "x11",
    }
    base.update(overrides)
    return SessionInfo(**base)  # type: ignore[arg-type]


# --------------------------------------------------- what counts as one session


def test_an_active_unlocked_user_session_counts() -> None:
    assert a_session().counts_as_use is True


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"locked": True}, "the screen is locked"),
        ({"active": False, "state": "online"}, "switched away from on its seat"),
        ({"state": "closing"}, "logind is tearing it down"),
        ({"state": "online"}, "present but not in the foreground"),
        ({"session_class": "greeter"}, "the login screen, not the child"),
        ({"session_class": "lock-screen"}, "the lock screen's own session"),
    ],
)
def test_a_session_that_does_not_count(change: dict[str, object], why: str) -> None:
    assert a_session(**change).counts_as_use is False, why


def test_an_idle_session_still_counts() -> None:
    """The child is occupying the machine; idleness only stops *earning* (§3.2)."""
    assert a_session(idle=True).counts_as_use is True


# ------------------------------------------------------ collapsing to one child


def test_no_sessions_is_absent_not_active() -> None:
    presence = UserPresence(unix_user="elif")
    assert presence.present is False
    assert presence.active is False
    assert presence.idle is False


def test_two_sessions_where_one_counts_is_active_once() -> None:
    """A fast user switch, or an SSH login beside a desktop."""
    presence = UserPresence(
        unix_user="elif",
        sessions=(
            a_session(session_id="tty", active=False, state="online", session_type="tty"),
            a_session(session_id="x11"),
        ),
    )
    assert presence.present is True
    assert presence.active is True


def test_a_user_whose_every_session_is_locked_is_present_but_not_active() -> None:
    presence = UserPresence(
        unix_user="elif",
        sessions=(
            a_session(session_id="a", locked=True),
            a_session(session_id="b", locked=True),
        ),
    )
    assert presence.present is True
    assert presence.active is False


def test_idle_needs_every_counting_session_to_be_idle() -> None:
    """A video playing in one session and an idle terminal in another is not idle."""
    mixed = UserPresence(
        unix_user="elif",
        sessions=(a_session(session_id="a", idle=True), a_session(session_id="b", idle=False)),
    )
    assert mixed.idle is False

    both = UserPresence(
        unix_user="elif",
        sessions=(a_session(session_id="a", idle=True), a_session(session_id="b", idle=True)),
    )
    assert both.idle is True


def test_a_locked_idle_session_does_not_make_the_user_idle() -> None:
    """Idle is about the sessions that count; a locked one counts for nothing."""
    presence = UserPresence(unix_user="elif", sessions=(a_session(locked=True, idle=True),))
    assert presence.idle is False


def test_only_user_sessions_are_ever_locked() -> None:
    """Locking the greeter would put the login screen out of reach."""
    presence = UserPresence(
        unix_user="elif",
        sessions=(
            a_session(session_id="greeter", session_class="greeter"),
            a_session(session_id="real"),
        ),
    )
    assert presence.lockable_session_ids == ("real",)


def test_the_primary_session_is_the_one_being_used() -> None:
    presence = UserPresence(
        unix_user="elif",
        sessions=(
            a_session(session_id="old", active=False, state="online"),
            a_session(session_id="current"),
        ),
    )
    primary = presence.primary_session
    assert primary is not None and primary.session_id == "current"


def test_the_primary_session_falls_back_to_any_session_at_all() -> None:
    presence = UserPresence(unix_user="elif", sessions=(a_session(locked=True),))
    primary = presence.primary_session
    assert primary is not None and primary.locked


# ------------------------------------------------------------------ the watcher


async def test_the_watcher_groups_sessions_by_user() -> None:
    source = FakeSessionSource()
    source.log_in("elif")
    source.log_in("elif", session_id="elif-tty")
    source.log_in("kerem", uid=1001)
    watcher = SessionWatcher(source)
    await watcher.start()

    assert len(watcher.presence("elif").sessions) == 2
    assert len(watcher.presence("kerem").sessions) == 1
    assert watcher.presence("nobody").present is False


async def test_a_session_that_existed_before_the_daemon_started_is_seen() -> None:
    """The state is rebuilt from a full list, never accumulated from signals."""
    source = FakeSessionSource()
    source.log_in("elif")
    watcher = SessionWatcher(source)
    await watcher.start()
    assert watcher.presence("elif").active is True


async def test_logind_failing_keeps_the_previous_answer() -> None:
    """A D-Bus hiccup must not silently mark every child as absent."""
    source = FakeSessionSource()
    source.log_in("elif")
    watcher = SessionWatcher(source)
    await watcher.start()

    source.fail_snapshot = True
    await watcher.refresh()

    assert watcher.presence("elif").active is True


async def test_locking_a_user_locks_each_of_their_sessions() -> None:
    source = FakeSessionSource()
    source.log_in("elif")
    source.log_in("elif", session_id="elif-2")
    watcher = SessionWatcher(source)
    await watcher.start()

    locked = await watcher.lock_user("elif")

    assert locked == 2
    assert sorted(source.locked_calls) == ["elif-1", "elif-2"]
    await watcher.refresh()
    assert watcher.presence("elif").active is False


async def test_locking_a_user_who_is_not_here_does_nothing() -> None:
    source = FakeSessionSource()
    watcher = SessionWatcher(source)
    await watcher.start()
    assert await watcher.lock_user("elif") == 0
    assert source.locked_calls == []


async def test_suspending_is_a_flag_the_loop_reads_not_an_accounting_correction() -> None:
    """The ledger handles suspend from the clocks; this stops the *enforcer* acting."""
    watcher = SessionWatcher(FakeSessionSource())
    await watcher.start()
    assert watcher.suspending is False
    watcher.set_suspending(True)
    assert watcher.suspending is True
