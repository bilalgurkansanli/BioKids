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

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import AuditLog, Child, Database, Policy
from biokids_core.ipc import EventKind, Op, Request, Response
from biokids_core.types import EndAction
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer, RecordingRunner
from biokids_daemon.server import DaemonServer
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.supervisor import Supervisor

TICK = 10


@pytest.fixture
def source() -> FakeSessionSource:
    return FakeSessionSource()


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


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

    supervisor.grant_time(1, 600)
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

    supervisor.grant_time(1, 10 * 60)
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
    supervisor.grant_time(1, 600)
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
    supervisor.grant_time(1, 600)
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
