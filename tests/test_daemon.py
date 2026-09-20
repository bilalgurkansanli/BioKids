"""T3 — the daemon loop, the enforcer and the end-of-time sequence.

The interesting assertions here are the ones about *sequence*: that the ten,
five and one minute warnings happen once each and in order, that the lock screen
precedes the shutdown by the configured countdown and not less, and that a
parent pressing "+10 dk" during that countdown takes the machine back off the
ledge and re-arms the warnings that are now ahead of the clock again.

No logind, no systemd, no sound card: the session source and the command runner
are both injected, so the whole sequence runs in milliseconds on any machine.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from collections.abc import Sequence
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import AuditLog, Child, Database, Policy
from biokids_core.ipc import EventKind, Op, Request, Response
from biokids_core.ledger import TickInput
from biokids_core.types import EndAction
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer, RecordingRunner
from biokids_daemon.server import DaemonServer
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.supervisor import ACTIVITY_TTL_SECONDS, Supervisor, run_forever

TICK = 10


@pytest.fixture
def source() -> FakeSessionSource:
    return FakeSessionSource()


class LoginctlAwareRunner(RecordingRunner):
    """A recording runner that mirrors what loginctl actually does.

    `loginctl lock-session` makes logind set `LockedHint` on that session, which
    the watcher then reads back. Without modelling that, a test cannot tell the
    difference between "the daemon locks a session once" and "the daemon locks
    an already-locked session ten times a minute" — and one of those is a bug.
    """

    def __init__(self, source: FakeSessionSource) -> None:
        super().__init__()
        self.source = source

    async def __call__(self, argv: Sequence[str]) -> Any:
        result = await super().__call__(argv)
        if result.ok and list(argv[:2]) == ["loginctl", "lock-session"] and len(argv) > 2:
            self.source.set(argv[2], locked=True)
        if result.ok and list(argv[:2]) == ["loginctl", "terminate-user"] and len(argv) > 2:
            self.source.sessions = [s for s in self.source.sessions if s.unix_user != argv[2]]
        return result


@pytest.fixture
def runner(source: FakeSessionSource) -> RecordingRunner:
    return LoginctlAwareRunner(source)


@pytest.fixture
def bus() -> EventBus:
    """A bus that remembers everything.

    The production bus keeps a short replay buffer and a bounded per-subscriber
    queue on purpose — a stalled browser tab must not be able to stall the
    daemon. A test asserting on the order of an hour of warnings needs the whole
    stream, so it asks for it explicitly rather than the production defaults
    quietly deciding what the test can see.
    """
    return EventBus(queue_size=4096, history_size=4096)


@pytest.fixture
def supervisor(
    db: Database,
    bus: EventBus,
    source: FakeSessionSource,
    runner: RecordingRunner,
    clock: FakeClock,
    config: Config,
    child: Child,
) -> Supervisor:
    return Supervisor(
        database=db,
        bus=bus,
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, runner),
        clock=clock,
        config=config,
    )


async def run_for(supervisor: Supervisor, clock: FakeClock, seconds: int) -> None:
    for _ in range(seconds // TICK):
        clock.advance(TICK)
        await supervisor.tick()


async def run_learning(
    supervisor: Supervisor, clock: FakeClock, seconds: int, child_id: int = 1
) -> None:
    """Run the loop with an educational app in focus the whole time.

    The shell re-sends the focus report; a report is only trusted for
    ACTIVITY_TTL_SECONDS. Reporting once and then running for ten minutes models
    a shell that crashed, and the learning streak resets every ninety seconds -
    which is correct behaviour, and not what these tests are about.
    """
    for _ in range(seconds // TICK):
        clock.advance(TICK)
        supervisor.report_activity(child_id, "gcompris", "GCompris", "learning")
        await supervisor.tick()


def set_quota(session: Session, child_id: int, minutes: int, **extra: object) -> None:
    policy = session.get(Policy, child_id)
    assert policy is not None
    policy.weekday_quota_min = minutes
    policy.weekend_quota_min = minutes
    for key, value in extra.items():
        setattr(policy, key, value)
    session.commit()


def events_of(bus: EventBus, kind: EventKind) -> list[dict[str, object]]:
    return [e.payload for e in bus.history if e.kind == str(kind)]


# -------------------------------------------------------------- session facts


async def test_a_logged_out_child_consumes_nothing(
    supervisor: Supervisor, clock: FakeClock
) -> None:
    await supervisor.start()
    await run_for(supervisor, clock, 300)
    assert supervisor.ledger.state(1).general_used_s == 0


async def test_an_active_session_consumes_time(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource
) -> None:
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 120)
    assert supervisor.ledger.state(1).general_used_s == 110, "the first tick sets a baseline"


async def test_locking_the_screen_stops_the_clock(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource
) -> None:
    await supervisor.start()
    session = source.log_in("elif")
    await run_for(supervisor, clock, 120)
    used = supervisor.ledger.state(1).general_used_s

    source.set(session.session_id, locked=True)
    await run_for(supervisor, clock, 600)

    assert supervisor.ledger.state(1).general_used_s == used


async def test_a_session_on_another_seat_does_not_consume_time(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource
) -> None:
    """`Active` is false for a session that has been switched away from."""
    await supervisor.start()
    session = source.log_in("elif")
    await run_for(supervisor, clock, 60)
    used = supervisor.ledger.state(1).general_used_s

    source.set(session.session_id, active=False, state="online")
    await run_for(supervisor, clock, 600)
    assert supervisor.ledger.state(1).general_used_s == used


async def test_logind_going_away_keeps_the_previous_snapshot(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource
) -> None:
    """A D-Bus hiccup must not silently mark every child as absent."""
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 60)

    source.fail_snapshot = True
    await run_for(supervisor, clock, 60)
    assert supervisor.watcher.presence("elif").active is True


# ------------------------------------------------------------------ warnings


async def test_each_warning_is_spoken_exactly_once_and_in_order(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    bus: EventBus,
) -> None:
    set_quota(session, 1, 12)
    await supervisor.start()
    source.log_in("elif")

    await run_for(supervisor, clock, 12 * 60)

    spoken = [str(p.get("event", "")) for p in events_of(bus, EventKind.SPEAK)]
    warnings = [e for e in spoken if e.startswith("warn_")]
    assert warnings == ["warn_10", "warn_5", "warn_1"]


async def test_a_warning_survives_a_daemon_restart_without_repeating(
    db: Database,
    bus: EventBus,
    source: FakeSessionSource,
    runner: RecordingRunner,
    clock: FakeClock,
    config: Config,
    session: Session,
    child: Child,
) -> None:
    set_quota(session, 1, 12)

    first = Supervisor(
        database=db,
        bus=bus,
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, runner),
        clock=clock,
        config=config,
    )
    await first.start()
    source.log_in("elif")
    await run_for(first, clock, 3 * 60)
    await first.stop()
    assert "warn_10" in [str(p.get("event")) for p in events_of(bus, EventKind.SPEAK)]

    second_bus = EventBus()
    second = Supervisor(
        database=db,
        bus=second_bus,
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, runner),
        clock=clock,
        config=config,
    )
    await second.start()
    await run_for(second, clock, 60)

    replayed = [str(p.get("event")) for p in events_of(second_bus, EventKind.SPEAK)]
    assert "warn_10" not in replayed


# ------------------------------------------------------- end-of-time sequence


async def test_the_end_sequence_locks_then_powers_off_after_the_countdown(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    set_quota(session, 1, 1, end_action=EndAction.POWEROFF.value, lock_countdown_seconds=120)
    await supervisor.start()
    logged_in = source.log_in("elif")

    await run_for(supervisor, clock, 70)
    assert runner.ran("lock-session", logged_in.session_id), "the screen locks at zero"
    assert not runner.ran("systemctl", "poweroff"), "and nothing else happens yet"

    await run_for(supervisor, clock, 60)
    assert not runner.ran("systemctl", "poweroff"), "still inside the two minutes"

    await run_for(supervisor, clock, 70)
    assert runner.ran("systemctl", "poweroff")


async def test_applications_are_stopped_before_the_machine_goes_down(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    set_quota(session, 1, 1, lock_countdown_seconds=20)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 120)

    calls = [" ".join(c) for c in runner.calls]
    stop_at = next(i for i, c in enumerate(calls) if "stop" in c)
    power_at = next(i for i, c in enumerate(calls) if "poweroff" in c)
    assert stop_at < power_at, "a child's unsaved drawing gets its SIGTERM first"


async def test_the_end_action_fires_once_not_every_tick(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    set_quota(session, 1, 1, lock_countdown_seconds=20)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 300)

    powered = [c for c in runner.calls if "poweroff" in c]
    assert len(powered) == 1


async def test_lock_only_is_honoured_as_an_end_action(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    set_quota(session, 1, 1, end_action=EndAction.LOCK.value, lock_countdown_seconds=20)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 180)

    assert not runner.ran("systemctl", "poweroff")
    assert runner.ran("loginctl", "lock-session")


async def test_a_break_locks_but_never_powers_the_machine_off(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    """The machine does not shut down because a child owes five minutes of rest."""
    set_quota(session, 1, 120, break_enabled=True, break_work_min=1, break_rest_min=1)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 200)

    assert not runner.ran("systemctl", "poweroff")


async def test_granting_time_during_the_countdown_cancels_it(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    set_quota(session, 1, 1, lock_countdown_seconds=120)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 70)
    assert runner.ran("loginctl", "lock-session")

    await supervisor.grant_time(1, 600)
    await run_for(supervisor, clock, 200)

    assert not runner.ran("systemctl", "poweroff")


async def test_granted_time_re_arms_a_warning_that_is_ahead_again(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    bus: EventBus,
) -> None:
    """Otherwise the extra ten minutes arrive with no warning at the end of them."""
    set_quota(session, 1, 12)
    await supervisor.start()
    source.log_in("elif")

    await run_for(supervisor, clock, 3 * 60)
    first = [str(p.get("event")) for p in events_of(bus, EventKind.SPEAK)]
    assert first.count("warn_10") == 1

    await supervisor.grant_time(1, 10 * 60)
    await run_for(supervisor, clock, 11 * 60)

    again = [str(p.get("event")) for p in events_of(bus, EventKind.SPEAK)]
    assert again.count("warn_10") == 2


# --------------------------------------------------------------- clock jumps


async def test_a_clock_jump_is_audited_and_changes_nothing(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
) -> None:
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 60)
    used = supervisor.ledger.state(1).general_used_s

    clock.jump_wall(3 * 3600)
    await supervisor.tick()

    assert supervisor.ledger.state(1).general_used_s == used
    supervisor.session.commit()
    entries = session.query(AuditLog).filter(AuditLog.action == "clock_jump").all()
    assert len(entries) == 1
    assert entries[0].severity == "warning"


# ------------------------------------------------------------- parent actions


async def test_granting_ten_minutes_changes_what_the_status_reports(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource, session: Session
) -> None:
    set_quota(session, 1, 30)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 600)

    before = supervisor.status()["children"][0]["remaining_seconds"]
    await supervisor.grant_time(1, 600)
    after = supervisor.status()["children"][0]["remaining_seconds"]
    assert after - before == 600


async def test_a_grant_is_worth_its_full_value_even_if_barely_any_time_was_used(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource, session: Session
) -> None:
    """Ten minutes granted is ten minutes, not "up to ten minutes of usage undone"."""
    set_quota(session, 1, 120)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 60)

    before = supervisor.status()["children"][0]["remaining_seconds"]
    await supervisor.grant_time(1, 600)
    after = supervisor.status()["children"][0]["remaining_seconds"]
    assert after - before == 600


async def test_lock_now_locks_only_this_child(
    supervisor: Supervisor, source: FakeSessionSource, runner: RecordingRunner
) -> None:
    await supervisor.start()
    child_session = source.log_in("elif")
    parent_session = source.log_in("anne", uid=1001)
    await supervisor.watcher.refresh()

    await supervisor.lock_now(1)

    assert runner.ran("lock-session", child_session.session_id)
    assert not runner.ran("lock-session", parent_session.session_id)


async def test_an_unmanaged_account_is_never_touched(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    """A child the parent disabled is a normal user again, not a locked-out one."""
    row = session.get(Child, 1)
    assert row is not None
    row.enabled = False
    session.commit()

    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 3600)

    assert runner.calls == []
    assert supervisor.status()["children"][0]["managed"] is False


async def test_reload_picks_up_a_policy_change_without_a_restart(
    supervisor: Supervisor, session: Session
) -> None:
    await supervisor.start()
    assert supervisor.children[1].policy is not None
    assert supervisor.children[1].policy.weekday_quota_min == 60

    set_quota(session, 1, 15)
    supervisor.reload()

    assert supervisor.children[1].policy is not None
    assert supervisor.children[1].policy.weekday_quota_min == 15


# ------------------------------------------------------------------ the bus


def test_a_slow_subscriber_is_dropped_not_waited_for() -> None:
    bus = EventBus(queue_size=2)
    subscriber = bus.subscribe(label="a stalled browser tab")
    for index in range(10):
        bus.publish(EventKind.TICK, child_id=1, n=index)

    assert subscriber.queue.qsize() == 2
    assert subscriber.dropped == 8
    newest = subscriber.queue.get_nowait()
    assert newest.payload["n"] == 8, "the oldest is dropped, the newest is kept"


def test_a_child_filtered_subscriber_still_gets_daemon_events() -> None:
    bus = EventBus()
    subscriber = bus.subscribe(child_id=2)
    bus.publish(EventKind.TICK, child_id=1)
    bus.publish(EventKind.TICK, child_id=2)
    bus.publish(EventKind.DAEMON_STATE, state="started")

    kinds = []
    while not subscriber.queue.empty():
        kinds.append(subscriber.queue.get_nowait())
    assert [e.child_id for e in kinds] == [2, None]


# ---------------------------------------------------------------- the server


async def test_the_server_answers_a_ping(supervisor: Supervisor, bus: EventBus) -> None:
    await supervisor.start()
    server = DaemonServer(supervisor, bus, supervisor.config)
    response, subscribe = await server._dispatch(Request(op=Op.PING, id="1").encode())
    assert response.ok
    assert subscribe is None


async def test_the_server_refuses_an_unknown_op(supervisor: Supervisor, bus: EventBus) -> None:
    await supervisor.start()
    server = DaemonServer(supervisor, bus, supervisor.config)
    response, _ = await server._dispatch(Request(op="drop_tables", id="1").encode())
    assert response.ok is False
    assert "unknown op" in response.error


async def test_the_server_survives_a_malformed_frame(
    supervisor: Supervisor, bus: EventBus
) -> None:
    await supervisor.start()
    server = DaemonServer(supervisor, bus, supervisor.config)
    response, _ = await server._dispatch(b"{not json at all\n")
    assert response.ok is False
    assert response.error == "protocol.malformed"


async def test_the_server_reports_status_for_every_child(
    supervisor: Supervisor, bus: EventBus, source: FakeSessionSource
) -> None:
    await supervisor.start()
    source.log_in("elif")
    await supervisor.watcher.refresh()
    server = DaemonServer(supervisor, bus, supervisor.config)

    response, _ = await server._dispatch(Request(op=Op.GET_STATUS, id="9").encode())

    assert response.ok
    assert response.data["children"][0]["display_name"] == "Elif"
    assert response.data["children"][0]["online"] is True


async def test_a_launch_request_is_refused_for_an_app_the_parent_did_not_enable(
    supervisor: Supervisor, bus: EventBus
) -> None:
    await supervisor.start()
    server = DaemonServer(supervisor, bus, supervisor.config)
    response, _ = await server._dispatch(
        Request(op=Op.REQUEST_LAUNCH, id="3", args={"child_id": 1, "app_slug": "doom"}).encode()
    )
    assert response.ok
    assert response.data["allow"] is False
    assert response.data["reason_code"] == "app.not_allowed"


def test_the_protocol_round_trips_every_frame_type() -> None:
    request = Request(op="grant_time", id="7", args={"child_id": 1, "seconds": 600})
    assert Request.decode(request.encode()) == request

    response = Response(id="7", ok=True, data={"remaining_seconds": 600})
    assert Response.decode(response.encode()) == response

    from biokids_core.ipc import Event

    event = Event(kind="tick", child_id=1, payload={"remaining_seconds": 42}, ts="now")
    assert Event.decode(event.encode()) == event


def test_a_frame_carries_turkish_without_escaping_it() -> None:
    """`ensure_ascii=False`: a child called Gökçe should not arrive as \\u00f6."""
    response = Response(id="1", ok=True, data={"name": "Gökçe"})
    assert "Gökçe".encode() in response.encode()


# --------------------------------------------------------------- the enforcer


async def test_the_enforcer_never_builds_a_shell_string(config: Config) -> None:
    """A display name is user input and ends up in some of these arguments."""
    runner = RecordingRunner()
    enforcer = Enforcer(config, runner)
    await enforcer.logout_user("elif; rm -rf /")
    assert runner.calls == [("loginctl", "terminate-user", "elif; rm -rf /")]


async def test_apply_end_action_dispatches_correctly(config: Config) -> None:
    runner = RecordingRunner()
    enforcer = Enforcer(config, runner)

    await enforcer.apply_end_action(EndAction.LOCK, "elif", ["s1"])
    await enforcer.apply_end_action(EndAction.LOGOUT, "elif", ["s1"])
    await enforcer.apply_end_action(EndAction.POWEROFF, "elif", ["s1"])

    assert runner.ran("loginctl", "lock-session", "s1")
    assert runner.ran("loginctl", "terminate-user", "elif")
    assert runner.ran("systemctl", "poweroff")


async def test_locking_no_sessions_is_a_no_op_not_an_error(config: Config) -> None:
    runner = RecordingRunner()
    assert await Enforcer(config, runner).lock_sessions([]) is False
    assert runner.calls == []


# ------------------------------------------------------------------ day roll


async def test_a_new_day_gives_the_child_their_quota_back(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource, session: Session
) -> None:
    set_quota(session, 1, 20)
    await supervisor.start()
    source.log_in("elif")
    # One extra tick: the first tick for a child establishes a baseline rather
    # than billing an interval, so twenty minutes of wall time is one tick short.
    await run_for(supervisor, clock, 20 * 60 + 2 * TICK)
    assert supervisor.status()["children"][0]["remaining_seconds"] == 0

    clock.wall += dt.timedelta(days=1)
    await supervisor.tick()

    assert supervisor.status()["children"][0]["remaining_seconds"] > 0


# ------------------------------------------- the end sequence, adversarially
#
# Four ways a child got an unsupervised afternoon, each found by reading the
# code adversarially rather than by a failing test. They are grouped here
# because they share one cause: the sequence used to be something that happened
# once, and "once" is not a property a child cooperates with.


async def test_rebooting_does_not_buy_hours_of_unsupervised_use(
    db: Database,
    bus: EventBus,
    source: FakeSessionSource,
    runner: RecordingRunner,
    clock: FakeClock,
    config: Config,
    session: Session,
    child: Child,
) -> None:
    """The countdown is a BOOTTIME offset, so it has to carry its boot with it.

    Without one: the machine has been up for hours, the quota runs out, the
    child pulls the power. BOOTTIME restarts at zero, the stored offset now
    reads as "the countdown has not started", and because it is not None the
    branch that locks the screen is skipped too. The child gets the machine back
    unlocked, and keeps it until uptime passes the old offset — typically hours.
    """
    set_quota(session, 1, 5, end_action=EndAction.POWEROFF.value, lock_countdown_seconds=60)
    clock.advance(8 * 3600)  # a machine that booted this morning

    first = Supervisor(
        database=db,
        bus=bus,
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, runner),
        clock=clock,
        config=config,
    )
    await first.start()
    source.log_in("elif")
    await run_for(first, clock, 6 * 60)
    assert runner.ran("loginctl", "lock-session"), "the day ended and the screen locked"
    await first.stop()

    # The child reboots instead of waiting for the shutdown.
    clock.reboot(off_for=60)
    runner.calls.clear()
    source.sessions.clear()

    second = Supervisor(
        database=db,
        bus=EventBus(queue_size=4096, history_size=4096),
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, runner),
        clock=clock,
        config=config,
    )
    await second.start()
    source.log_in("elif", session_id="after-reboot")
    await run_for(second, clock, 30)

    assert runner.ran("loginctl", "lock-session", "after-reboot"), (
        "the new session must be locked within a tick, not in eight hours"
    )

    await run_for(second, clock, 90)
    assert runner.ran("systemctl", "poweroff"), "and the countdown must run again"


async def test_logging_back_in_after_a_logout_is_enforced_again(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    """lightdm hands the child a new session; the day's quota is still spent."""
    set_quota(session, 1, 5, end_action=EndAction.LOGOUT.value, lock_countdown_seconds=30)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 6 * 60)
    assert runner.ran("loginctl", "terminate-user", "elif")

    runner.calls.clear()
    source.sessions.clear()
    source.log_in("elif", session_id="second-go")
    await run_for(supervisor, clock, 60)

    assert runner.ran("loginctl", "terminate-user", "elif"), (
        "a fresh session id is something the end action has not been applied to"
    )


async def test_a_dismissed_lock_comes_back_within_one_tick(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    set_quota(session, 1, 5, end_action=EndAction.LOCK.value, lock_countdown_seconds=600)
    await supervisor.start()
    logged_in = source.log_in("elif")
    await run_for(supervisor, clock, 6 * 60)
    assert runner.ran("loginctl", "lock-session", logged_in.session_id)

    source.set(logged_in.session_id, locked=False)
    runner.calls.clear()
    await run_for(supervisor, clock, 20)

    assert runner.ran("loginctl", "lock-session", logged_in.session_id)


async def test_an_already_locked_session_is_not_locked_every_ten_seconds(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    set_quota(session, 1, 5, end_action=EndAction.LOCK.value, lock_countdown_seconds=600)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 6 * 60)

    runner.calls.clear()
    await run_for(supervisor, clock, 120)

    assert not runner.ran("loginctl", "lock-session"), "logind already says it is locked"


async def test_a_break_never_opens_the_end_of_day_countdown(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
    bus: EventBus,
) -> None:
    """The bug this guards: a break wrote the countdown slot, so the real
    exhaustion later found a timestamp already past the countdown and powered
    the machine off in the same tick — no lock screen, no two minutes, no
    "time's up"."""
    set_quota(
        session,
        1,
        10,
        break_enabled=True,
        break_work_min=5,
        break_rest_min=1,
        end_action=EndAction.POWEROFF.value,
        lock_countdown_seconds=120,
    )
    await supervisor.start()
    source.log_in("elif")

    await run_for(supervisor, clock, 7 * 60)  # through the break and out again
    assert not runner.ran("systemctl", "poweroff")

    await run_for(supervisor, clock, 5 * 60)  # now the quota really runs out
    spoken = [str(p.get("event", "")) for p in events_of(bus, EventKind.SPEAK)]
    assert "time_up" in spoken, "the child is told the day is over"
    assert not runner.ran("systemctl", "poweroff"), "the two minutes have not passed yet"

    countdowns = [
        int(p.get("countdown_seconds", 0))
        for p in events_of(bus, EventKind.END_SEQUENCE)
        if "countdown_seconds" in p
    ]
    assert max(countdowns) > 100, "the countdown starts at the top, not at zero"

    await run_for(supervisor, clock, 130)
    assert runner.ran("systemctl", "poweroff")


async def test_a_break_pauses_the_child_without_locking_them_out(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
) -> None:
    """Nothing in BioKids can unlock a session, so a break must not lock one.

    A five-year-old left at a password prompt for the rest of the afternoon is
    a worse outcome than the one the break was for. UX.md §2.5 asks for a calm
    full-screen timer, which the launcher draws.
    """
    set_quota(session, 1, 120, break_enabled=True, break_work_min=1, break_rest_min=1)
    await supervisor.start()
    source.log_in("elif")

    await run_for(supervisor, clock, 70)

    assert not runner.ran("loginctl", "lock-session"), "a break is not a lock"
    assert runner.ran("stop"), "but the running application is stopped, once"

    stops = [call for call in runner.calls if "stop" in call]
    runner.calls.clear()
    await run_for(supervisor, clock, 30)
    assert not runner.ran("stop"), "and not again on every tick"
    assert len(stops) == 1

    await run_for(supervisor, clock, 60)
    assert supervisor.shell_state(1)["allow"] is True, "the child is back on the grid"


async def test_a_timeout_pauses_rather_than_locking_too(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: RecordingRunner,
) -> None:
    await supervisor.start()
    source.log_in("elif")
    supervisor.start_timeout(1, 1)
    await run_for(supervisor, clock, 30)

    assert not runner.ran("loginctl", "lock-session")
    assert runner.ran("stop")


async def test_an_end_action_that_keeps_failing_escalates_to_lockdown(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    session: Session,
    runner: RecordingRunner,
    db: Database,
) -> None:
    """Audited once and then forgotten is `continue unprotected`."""
    set_quota(session, 1, 5, end_action=EndAction.POWEROFF.value, lock_countdown_seconds=10)
    await supervisor.start()
    source.log_in("elif")
    runner.returncode = 1  # everything the enforcer runs now fails

    await run_for(supervisor, clock, 10 * 60)

    supervisor.session.commit()
    with db.session() as fresh:
        entries = [row for row in fresh.query(AuditLog).all() if row.action == "lockdown"]
    assert entries, "three failures in a row take the D-106 path"
    assert entries[0].severity == "alert"


# ------------------------------------------------- earned time, and saying so


async def test_the_earning_celebration_never_says_zero_minutes(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    bus: EventBus,
    session: Session,
) -> None:
    """A tick's payout is ten seconds, and `10 // 60` is zero.

    The first payout clears the streak threshold and is worth ten minutes; every
    tick after it credits one tick's worth. Announcing each of those meant the
    child was congratulated for earning zero minutes, every ten seconds, for as
    long as they kept working.
    """
    set_quota(
        session,
        1,
        60,
        earn_enabled=True,
        earn_ratio=1.0,
        earn_cap_min=60,
        earn_min_session_min=1,
    )
    await supervisor.start()
    source.log_in("elif")
    await run_learning(supervisor, clock, 600)

    spoken = [e for e in events_of(bus, EventKind.SPEAK) if e.get("event") == "earned"]
    assert spoken, "ten minutes of learning earned something worth saying"
    minutes = [int(e["vars"]["minutes"]) for e in spoken]  # type: ignore[index]
    assert all(m >= 1 for m in minutes), f"congratulated for earning nothing: {minutes}"

    # shell.js draws a banner from the EARNED event, so it carried the same
    # zero across the child's screen every ten seconds, with no voice setting
    # to turn it off. The banner and the spoken line are one celebration now.
    banners = [int(e["minutes"]) for e in events_of(bus, EventKind.EARNED)]  # type: ignore[arg-type]
    assert banners, "the child sees the minutes appear (D-45)"
    assert all(m >= 1 for m in banners), f"a banner announcing nothing: {banners}"
    assert len(banners) == len(spoken), "the banner and the voice are one celebration"


async def test_the_earning_celebration_does_not_interrupt_every_ten_seconds(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    bus: EventBus,
    session: Session,
) -> None:
    """Praise on every tick is not a reward, it is a nuisance."""
    set_quota(
        session,
        1,
        120,
        earn_enabled=True,
        earn_ratio=1.0,
        earn_cap_min=60,
        earn_min_session_min=1,
    )
    await supervisor.start()
    source.log_in("elif")
    await run_learning(supervisor, clock, 1800)

    spoken = [e for e in events_of(bus, EventKind.SPEAK) if e.get("event") == "earned"]
    assert len(spoken) <= 1800 // 300 + 1, f"spoke {len(spoken)} times in half an hour"


async def test_a_parent_who_turned_the_voice_off_is_obeyed_by_the_celebration_too(
    db: Database,
    bus: EventBus,
    source: FakeSessionSource,
    runner: RecordingRunner,
    clock: FakeClock,
    config: Config,
    child: Child,
    session: Session,
) -> None:
    """It published SPEAK for itself, so it was the one line that ignored the setting."""
    set_quota(
        session,
        1,
        60,
        earn_enabled=True,
        earn_ratio=1.0,
        earn_cap_min=60,
        earn_min_session_min=1,
    )
    quiet = dataclasses.replace(config, voice=dataclasses.replace(config.voice, enabled=False))
    supervisor = Supervisor(
        database=db,
        bus=bus,
        watcher=SessionWatcher(source),
        enforcer=Enforcer(quiet, runner),
        clock=clock,
        config=quiet,
    )
    await supervisor.start()
    source.log_in("elif")
    await run_learning(supervisor, clock, 900)

    assert events_of(bus, EventKind.SPEAK) == [], "the daemon spoke with the voice off"
    assert events_of(bus, EventKind.EARNED), "the visual counter is not the voice"


async def test_earned_minutes_actually_extend_the_day(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    bus: EventBus,
    session: Session,
) -> None:
    """The whole point, asserted on the number the child and the parent both see."""
    set_quota(
        session,
        1,
        20,
        earn_enabled=True,
        earn_ratio=1.0,
        earn_cap_min=60,
        earn_min_session_min=1,
    )
    await supervisor.start()
    source.log_in("elif")
    await run_learning(supervisor, clock, 600)

    ticks = events_of(bus, EventKind.TICK)
    remaining = int(ticks[-1]["remaining_seconds"])  # type: ignore[arg-type]
    used = supervisor.ledger.state(1).general_used_s
    assert used >= 600 - TICK, f"ten minutes were spent, less the baseline tick: {used}"
    assert remaining > 20 * 60 - used, (
        f"ten earned minutes bought nothing: {remaining}s left after {used}s of a 20 min day"
    )


# ------------------------------------------------------- stale activity


async def test_a_category_the_child_left_stops_capping_their_time(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    bus: EventBus,
) -> None:
    """The ledger applied the freshness rule and the policy engine did not.

    So the ledger stopped charging a category the child had left while the
    engine kept applying its limit, and `remaining_seconds` - the number on the
    parent's Today card - stayed pinned to an app closed twenty minutes ago.
    """
    await supervisor.start()
    source.log_in("elif")
    supervisor.report_activity(1, "supertux", "SuperTux", "games")

    await run_for(supervisor, clock, TICK)
    runtime = supervisor.children[1]
    assert runtime.category is not None

    await run_for(supervisor, clock, ACTIVITY_TTL_SECONDS * 2)

    last = events_of(bus, EventKind.TICK)[-1]
    assert last["app"] in (None, "", "SuperTux"), "the runtime may still remember it"
    decision = runtime.last_decision
    assert decision is not None
    # What matters is what the engine was told, not what the runtime remembers.
    assert supervisor.children[1].activity_fresh(clock.read().boot) is False


# ------------------------------------------------- re-arming the warnings


async def test_raising_the_quota_lets_the_warnings_fire_again(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    bus: EventBus,
    session: Session,
) -> None:
    """A parent editing the quota is not a grant, and used not to re-arm anything.

    Before the fix the child heard 10, 5 and 1 against the old 20-minute day;
    the parent then set 60 minutes on the settings screen; and the remaining
    forty minutes ran out in complete silence, ending in a shutdown with no
    warning at all. Only `+10 dk` re-armed them, because only `+10 dk` went
    through `grant`.
    """
    set_quota(session, 1, 20)
    await supervisor.start()
    source.log_in("elif")

    await run_for(supervisor, clock, 20 * 60 - 60)
    heard = [e["event"] for e in events_of(bus, EventKind.SPEAK)]
    assert "warn_10" in heard and "warn_5" in heard, f"the first day's warnings: {heard}"

    before = len(events_of(bus, EventKind.SPEAK))
    set_quota(session, 1, 60)
    supervisor.reload()
    await run_for(supervisor, clock, 41 * 60)

    after = [e["event"] for e in events_of(bus, EventKind.SPEAK)][before:]
    assert "warn_10" in after, f"the extra forty minutes ran out in silence: {after}"
    assert "warn_5" in after
    assert "warn_1" in after


async def test_saving_a_setting_does_not_restart_the_earning_celebration(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    bus: EventBus,
    session: Session,
) -> None:
    """`reload()` carries eleven fields across a policy change, on purpose.

    The two the celebration needs were not among them, so a parent pressing Save
    reset the throttle and dropped the seconds not yet announced. Save twice
    while a child is doing their homework and they are congratulated twice in a
    row for the same minutes.
    """
    set_quota(
        session,
        1,
        120,
        earn_enabled=True,
        earn_ratio=1.0,
        earn_cap_min=60,
        earn_min_session_min=1,
    )
    await supervisor.start()
    source.log_in("elif")

    await run_learning(supervisor, clock, 120)
    before = len(events_of(bus, EventKind.EARNED))
    assert before >= 1, "the first payout was celebrated"

    pending = supervisor.children[1].earned_pending_s
    supervisor.reload()

    assert supervisor.children[1].earned_pending_s == pending, "un-announced seconds lost"
    assert supervisor.children[1].earned_announced_at > 0, "the throttle was reset"

    await run_learning(supervisor, clock, 60)
    after = len(events_of(bus, EventKind.EARNED))
    assert after == before, f"reload made it celebrate again: {before} -> {after}"


# ------------------------------------- whose machine is it, at the end of the day


async def test_the_day_does_not_end_for_a_child_who_is_not_here(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: RecordingRunner,
    session: Session,
) -> None:
    """The mother logs in that evening and the machine shuts down under her.

    A logged-out child still evaluates to QUOTA_EXHAUSTED every tick - the
    engine never consults presence on that branch - and the end sequence had no
    presence test. `_lock` no-ops on an empty session list, so control reached
    `apply_end_action(POWEROFF)`, which ignores session ids entirely. Poweroff
    is the column default and the wizard pre-checks it, so this was what a
    family got without changing a setting.
    """
    set_quota(session, 1, 20, end_action=EndAction.POWEROFF.value)
    await supervisor.start()

    # Elif spends her twenty minutes and logs out.
    source.log_in("elif")
    await run_for(supervisor, clock, 21 * 60)
    source.log_out("elif")
    runner.calls.clear()

    # Her mother sits down at her own account.
    source.log_in("anne")
    await run_for(supervisor, clock, 10 * 60)

    assert not any("poweroff" in " ".join(call) for call in runner.calls), (
        f"the machine was powered off under the parent: {runner.calls}"
    )


async def test_ending_one_childs_day_does_not_shut_down_a_parent_who_is_working(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: RecordingRunner,
    session: Session,
) -> None:
    """Poweroff and logout reach past the child; locking is the part about them.

    The child still loses the machine. The parent, sitting at the same computer
    on their own account, does not lose their work to it.
    """
    set_quota(session, 1, 20, end_action=EndAction.POWEROFF.value)
    await supervisor.start()
    source.log_in("elif")
    source.log_in("anne")

    await run_for(supervisor, clock, 25 * 60)

    assert not any("poweroff" in " ".join(call) for call in runner.calls), (
        f"the parent's session was taken down with the child's: {runner.calls}"
    )
    assert any("lock-session" in " ".join(call) for call in runner.calls), (
        "the child was not stopped either; narrowing must not mean doing nothing"
    )


async def test_a_child_alone_at_the_machine_is_still_powered_off(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: RecordingRunner,
    session: Session,
) -> None:
    """The narrowing must not become a way out of the end action."""
    set_quota(session, 1, 20, end_action=EndAction.POWEROFF.value)
    await supervisor.start()
    source.log_in("elif")

    await run_for(supervisor, clock, 25 * 60)

    assert any("poweroff" in " ".join(call) for call in runner.calls), (
        f"the day never ended for a child sitting alone at the machine: {runner.calls}"
    )


# ------------------------------------- the loop that ran for days doing nothing


async def test_a_wedged_database_does_not_leave_the_child_unsupervised(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: RecordingRunner,
    session: Session,
) -> None:
    """Three failed ticks in a row must stop looking like supervision.

    One transient SQLite error used to be permanent: `persist()` had no
    rollback, so the daemon's long-lived Session stayed in the needs-rollback
    state and every later tick died before accounting for a second. The loop
    caught that, logged it, and went round again - for days. systemd saw
    `active (running)`, so `Restart=` and `OnFailure=` never fired, and the
    panel kept rendering the last numbers committed.
    """
    set_quota(session, 1, 60)
    await supervisor.start()
    source.log_in("elif")

    broken = {"count": 0}
    real_tick = supervisor.tick

    async def failing() -> None:
        broken["count"] += 1
        raise OperationalError("UPDATE ledger_day", {}, Exception("disk I/O error"))

    supervisor.tick = failing  # type: ignore[method-assign]
    stop = asyncio.Event()

    with pytest.raises(OperationalError):
        await run_forever(supervisor, stop)

    supervisor.tick = real_tick  # type: ignore[method-assign]
    assert broken["count"] == 3, f"gave up after {broken['count']} ticks, not 3"
    assert any("lock-session" in " ".join(call) for call in runner.calls), (
        "the loop gave up without locking the child's screen"
    )


async def test_one_bad_tick_is_not_a_reason_to_lock_a_child_out(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource, runner: RecordingRunner
) -> None:
    """A single D-Bus hiccup must not become a locked screen a parent has to explain."""
    await supervisor.start()
    source.log_in("elif")

    calls = {"n": 0}
    real_tick = supervisor.tick

    async def flaky() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("a transient hiccup")
        await real_tick()
        if calls["n"] >= 3:
            stop.set()

    supervisor.tick = flaky  # type: ignore[method-assign]
    stop = asyncio.Event()
    await run_forever(supervisor, stop)
    supervisor.tick = real_tick  # type: ignore[method-assign]

    assert calls["n"] >= 3, "the loop stopped on the first failure"
    assert not any("lock-session" in " ".join(call) for call in runner.calls), (
        "one bad tick locked the child out"
    )


def test_a_failed_persist_leaves_the_ledger_usable(
    config: Config, db: Database, child: Child, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback is what makes the next tick possible at all.

    Without it the Session stays in the needs-rollback state and every later
    call raises PendingRollbackError. The test fixtures hid this because
    `Database.session()` is the only place in src/ that rolls back, and the
    daemon deliberately does not use it.
    """
    from biokids_core.ledger import TimeLedger

    clock = FakeClock()
    with db.session() as own:
        ledger = TimeLedger(own, clock, config)
        ledger.begin()

        calls = {"n": 0}
        real_commit = own.commit

        def once_broken() -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("commit", {}, Exception("disk I/O error"))
            real_commit()

        monkeypatch.setattr(own, "commit", once_broken)
        with pytest.raises(OperationalError):
            ledger.persist()
        monkeypatch.undo()

        # The session has to be usable again, or the daemon is finished. Two
        # ticks, because the first one after begin() only sets the baseline.
        for _ in range(2):
            clock.advance(10)
            ledger.tick([TickInput(child_id=child.id, active=True, idle=False)])
        ledger.persist()
        assert ledger.state(child.id).general_used_s >= 10, "accounting never resumed"


async def test_granting_time_lets_a_locked_out_child_back_in(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: RecordingRunner,
    session: Session,
) -> None:
    """The child is at the password prompt of an account that has no password.

    The quota runs out, `loginctl lock-session` locks the screen, the child asks
    for more time and the parent presses "+10 dk". The panel said "Elif için 10
    dakika eklendi", the card showed 10:00 - and nothing unlocked the session.
    `Enforcer.unlock_sessions` existed and was called from nowhere in the whole
    product, so on a deliberately passwordless account (D-99) there was no way
    back in at all.
    """
    set_quota(session, 1, 20, end_action=EndAction.LOCK.value)
    await supervisor.start()
    logged_in = source.log_in("elif")

    await run_for(supervisor, clock, 25 * 60)
    assert source.sessions[0].locked, "the child was never locked in the first place"
    runner.calls.clear()

    await supervisor.grant_time(1, 10 * 60)

    assert any(
        call[:2] == ("loginctl", "unlock-session") and call[2] == logged_in.session_id
        for call in runner.calls
    ), f"the child was left at the lock screen: {runner.calls}"


async def test_a_grant_that_does_not_open_the_window_does_not_open_the_screen(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: RecordingRunner,
    session: Session,
) -> None:
    """Bedtime is not negotiable by granting quota.

    "+10 dk" outside the allowed hours must not unlock a screen the window is
    there to keep shut - the extra minutes are real, and they are for tomorrow.
    """
    set_quota(session, 1, 60, end_action=EndAction.LOCK.value)
    with session.no_autoflush:
        policy = session.get(Policy, 1)
        assert policy is not None
        policy.windows = {"weekday": ["06:00-07:00"], "weekend": ["06:00-07:00"]}
        session.commit()

    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 5 * 60)
    runner.calls.clear()

    await supervisor.grant_time(1, 10 * 60)

    assert not any("unlock-session" in " ".join(call) for call in runner.calls), (
        f"a grant opened a screen the window had closed: {runner.calls}"
    )


async def test_the_machine_does_not_power_off_between_two_windows(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: RecordingRunner,
    session: Session,
) -> None:
    """Before school and after school is one day with two windows, not two days.

    Every closed window used to produce WINDOW_CLOSED, which TERMINAL_LOCK
    routes to the end action - poweroff by default. So the computer shut itself
    down a couple of minutes after the morning window, while the child was told
    "see you tomorrow" and still had their whole quota and a window at four.
    """
    with session.no_autoflush:
        policy = session.get(Policy, 1)
        assert policy is not None
        policy.windows = {
            "weekday": ["07:30-08:15", "16:00-19:00"],
            "weekend": ["07:30-08:15", "16:00-19:00"],
        }
        policy.end_action = EndAction.POWEROFF.value
        session.commit()

    clock.wall = dt.datetime(2026, 9, 21, 8, 20, 0)
    await supervisor.start()
    source.log_in("elif")

    await run_for(supervisor, clock, 10 * 60)

    assert not any("poweroff" in " ".join(call) for call in runner.calls), (
        f"the day ended at 08:20 for a child whose next window opens at 16:00: {runner.calls}"
    )
    decision = supervisor.children[1].last_decision
    assert decision is not None and decision.reason_code == "window.waiting"


# ------------------------------------------------- the machine belongs to everyone


async def test_the_shutdown_tile_refuses_while_a_parent_is_logged_in(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource, runner: RecordingRunner
) -> None:
    """Mum switches user so her son can play; he taps "Kapat" when he is done.

    The check iterated `self.children`, so it refused when the other user was
    another managed child and allowed it when the other user was a parent - and
    the machine went down with her session and her unsaved work still open. The
    code already knew this harm was worth refusing; it was looking in the wrong
    list.
    """
    await supervisor.start()
    source.log_in("elif")
    source.log_in("anne")

    allowed = await supervisor.child_requested_shutdown(1)

    assert allowed is False
    assert not any("poweroff" in " ".join(call) for call in runner.calls)


async def test_a_child_alone_can_still_shut_the_machine_down(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource, runner: RecordingRunner
) -> None:
    """D-100 gives the child that tile on purpose; refusing always is not the fix."""
    await supervisor.start()
    source.log_in("elif")

    allowed = await supervisor.child_requested_shutdown(1)

    assert allowed is True
    assert any("poweroff" in " ".join(call) for call in runner.calls)


# ------------------------------------------------------- the ring the child reads


async def test_earned_minutes_make_the_ring_move(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource, session: Session
) -> None:
    """The shell draws remaining/quota and clamps to 1.

    Sending the bare quota while `remaining_seconds` included the bonus meant a
    child who earned thirty minutes watched a full circle that did not move for
    half an hour - the one thing a four-year-old is meant to be able to read.
    """
    set_quota(
        session,
        1,
        60,
        earn_enabled=True,
        earn_ratio=1.0,
        earn_cap_min=60,
        earn_min_session_min=1,
    )
    await supervisor.start()
    source.log_in("elif")
    await run_learning(supervisor, clock, 600)

    state = supervisor.shell_state(1)

    assert state["quota_seconds"] >= state["remaining_seconds"], (
        f"remaining {state['remaining_seconds']} exceeds quota {state['quota_seconds']}: "
        "the ring cannot move"
    )
    assert state["quota_seconds"] > 60 * 60, "the earned minutes are missing from the total"


# ---------------------------------------------------- a punishment outlives a day


def test_a_timeout_is_not_cancelled_by_midnight(
    config: Config, db: Database, child: Child
) -> None:
    """The counters live on the calendar-day row, and `_apply` counts down on it.

    So at 00:00 the ledger moved to a fresh row whose penalty is zero: a parent
    who gave a fifteen-minute timeout at 23:55 had it end five minutes later, in
    silence, and the child went straight back to what they were doing.
    """
    from biokids_core.ledger import TimeLedger

    clock = FakeClock(wall=dt.datetime(2026, 9, 21, 23, 55, 0))
    with db.session() as own:
        ledger = TimeLedger(own, clock, config)
        ledger.begin()
        ledger.start_penalty(child.id, 15 * 60)
        assert ledger.state(child.id).penalty_remaining_s == 15 * 60

        # Over midnight, one tick at a time.
        for _ in range(7):
            clock.advance(60)
            ledger.tick([TickInput(child_id=child.id, active=True, idle=False)])

        left = ledger.state(child.id).penalty_remaining_s

    assert left > 0, "the timeout evaporated at midnight"
    assert left <= 10 * 60, f"the timeout stopped counting down instead: {left}s left"
