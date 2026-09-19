"""T2.5 — the time ledger, driven by a fully injected clock.

This is the suite the product stands on. Every scenario in `TASKS-PHASE0.md` T2.5
is here, and each one is written as the thing a child would actually try:

* sit at the machine for an hour
* move the system clock forward three hours
* move it backward an hour
* pull the power out and turn it back on
* close the lid for two hours
* lock the screen and walk away
* log in twice

A three-hour session takes milliseconds because `FakeClock` is the only clock in
the room.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.orm import Session

from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import Database, LedgerDay
from biokids_core.ledger import TickInput, TimeLedger
from biokids_core.policy import general_allowance_seconds
from biokids_core.types import Category
from helpers import make_policy

TICK = 10
"""The production tick. Every test drives time in these, like the daemon does."""


def run(
    ledger: TimeLedger,
    clock: FakeClock,
    child_id: int,
    seconds: int,
    *,
    active: bool = True,
    idle: bool = False,
    category: Category | None = None,
    app_slug: str = "",
    policy: object | None = None,
) -> int:
    """Advance the clock in ticks and return the seconds the ledger counted."""
    counted = 0
    for _ in range(seconds // TICK):
        clock.advance(TICK)
        results = ledger.tick(
            [
                TickInput(
                    child_id=child_id,
                    active=active,
                    idle=idle,
                    category=category,
                    app_slug=app_slug,
                    policy=policy,  # type: ignore[arg-type]
                )
            ]
        )
        counted += results[0].counted_seconds
    return counted


def prime(ledger: TimeLedger, child_id: int) -> None:
    """Take the first reading. The first tick establishes a baseline, not usage."""
    ledger.tick([TickInput(child_id=child_id, active=False)])


# ------------------------------------------------------ the seven scenarios


def test_one_hour_of_use_consumes_exactly_one_hour(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    prime(ledger, 1)
    counted = run(ledger, clock, 1, 3600)
    assert counted == 3600
    assert ledger.state(1).general_used_s == 3600


def test_moving_the_clock_forward_three_hours_changes_nothing(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """The whole point of `CLOCK_BOOTTIME` (D-41)."""
    prime(ledger, 1)
    run(ledger, clock, 1, 600)
    before = ledger.state(1).general_used_s

    clock.jump_wall(3 * 3600)
    ledger.tick([TickInput(child_id=1, active=True)])

    assert ledger.state(1).general_used_s == before
    run(ledger, clock, 1, 600)
    assert ledger.state(1).general_used_s == before + 600


def test_moving_the_clock_backward_an_hour_changes_nothing_and_is_logged(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    prime(ledger, 1)
    run(ledger, clock, 1, 600)
    before = ledger.state(1).general_used_s

    clock.jump_wall(-3600)
    ledger.tick([TickInput(child_id=1, active=True)])

    assert ledger.state(1).general_used_s == before
    anomalies = ledger.take_anomalies()
    assert len(anomalies) == 1
    assert anomalies[0].direction == "backward"
    assert abs(anomalies[0].delta_seconds + 3600) < 2
    assert ledger.take_anomalies() == [], "anomalies are handed over once"


def test_a_small_ntp_correction_is_not_reported_as_tampering(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """The threshold is ten minutes; a normal NTP step must not alarm a parent."""
    prime(ledger, 1)
    run(ledger, clock, 1, 60)
    clock.jump_wall(120)
    ledger.tick([TickInput(child_id=1, active=True)])
    assert ledger.take_anomalies() == []


def test_a_reboot_mid_session_carries_the_total_forward(
    db: Database, clock: FakeClock, config: Config, child: object
) -> None:
    """At most the persist interval is lost — 30 seconds (ARCHITECTURE.md §3.3)."""
    with db.session() as first_session:
        first = TimeLedger(first_session, clock, config)
        first.begin()
        prime(first, 1)
        run(first, clock, 1, 600)
        first.persist()
        used_before = first.state(1).general_used_s
    assert used_before == 600

    clock.reboot(off_for=120)

    with db.session() as second_session:
        second = TimeLedger(second_session, clock, config)
        second.begin()
        prime(second, 1)
        assert second.state(1).general_used_s == used_before
        run(second, clock, 1, 300)
        assert second.state(1).general_used_s == used_before + 300


def test_a_crash_loses_at_most_the_persist_interval(
    db: Database, clock: FakeClock, config: Config, child: object
) -> None:
    with db.session() as crashing:
        ledger = TimeLedger(crashing, clock, config)
        ledger.begin()
        prime(ledger, 1)
        run(ledger, clock, 1, 600)
        # Power cut: whatever was not committed is gone.
        crashing.rollback()

    clock.reboot(off_for=60)
    with db.session() as recovered:
        ledger = TimeLedger(recovered, clock, config)
        ledger.begin()
        used = ledger.state(1).general_used_s

    lost = 600 - used
    assert 0 <= lost <= config.daemon.persist_seconds, f"lost {lost} s across a crash"


def test_suspending_for_two_hours_consumes_nothing(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """`CLOCK_BOOTTIME` runs through suspend; `CLOCK_MONOTONIC` does not (T2.4)."""
    prime(ledger, 1)
    run(ledger, clock, 1, 600)
    before = ledger.state(1).general_used_s

    clock.suspend(2 * 3600)
    ledger.tick([TickInput(child_id=1, active=True)])

    assert ledger.state(1).general_used_s == before
    assert ledger.take_anomalies() == [], "a closed lid is not clock tampering"


def test_a_locked_session_for_twenty_minutes_consumes_nothing(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    prime(ledger, 1)
    run(ledger, clock, 1, 600)
    before = ledger.state(1).general_used_s

    counted = run(ledger, clock, 1, 20 * 60, active=False)

    assert counted == 0
    assert ledger.state(1).general_used_s == before


def test_two_sessions_for_one_child_are_counted_once(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """A fast user switch must not bill the same second twice."""
    prime(ledger, 1)
    for _ in range(6):
        clock.advance(TICK)
        results = ledger.tick(
            [
                TickInput(child_id=1, active=True),  # seat 0
                TickInput(child_id=1, active=True),  # a second logind session
            ]
        )
        assert results[1].counted_seconds == 0
    assert ledger.state(1).general_used_s == 60


# --------------------------------------------------------------- gaps and clamps


def test_an_unexplained_gap_is_clamped_and_reported(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """A stopped daemon must not bill the time it was not running."""
    prime(ledger, 1)
    clock.advance(4 * 3600)
    result = ledger.tick([TickInput(child_id=1, active=True)])[0]

    assert result.counted_seconds == ledger.clamp_seconds
    assert result.gap_seconds == 4 * 3600 - ledger.clamp_seconds
    assert ledger.state(1).general_used_s == ledger.clamp_seconds


def test_the_first_tick_of_a_process_bills_nothing(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    clock.advance(999)
    result = ledger.tick([TickInput(child_id=1, active=True)])[0]
    assert result.counted_seconds == 0


# ------------------------------------------------------------ penalty and break


def test_a_penalty_counts_down_in_real_time_and_blocks_accrual(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    prime(ledger, 1)
    ledger.start_penalty(1, 120)
    counted = run(ledger, clock, 1, 120)

    assert counted == 0, "a timeout does not consume quota (D-81)"
    assert ledger.state(1).penalty_remaining_s == 0
    assert ledger.state(1).general_used_s == 0


def test_a_penalty_runs_down_even_while_the_child_is_logged_out(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """A punishment you can pause by logging out is not a punishment."""
    prime(ledger, 1)
    ledger.start_penalty(1, 60)
    run(ledger, clock, 1, 60, active=False)
    assert ledger.state(1).penalty_remaining_s == 0


def test_a_forced_break_starts_on_time_and_does_not_consume_quota(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    policy = make_policy(break_enabled=True, break_work_min=1, break_rest_min=1)
    prime(ledger, 1)

    run(ledger, clock, 1, 60, policy=policy)
    assert ledger.state(1).break_remaining_s == 60

    used_before_break = ledger.state(1).general_used_s
    run(ledger, clock, 1, 60, policy=policy)

    assert ledger.state(1).break_remaining_s == 0
    assert ledger.state(1).general_used_s == used_before_break, "the break is free (D-46)"
    assert ledger.state(1).seconds_since_break_s == 0


# ---------------------------------------------------------------- earned time


def test_a_short_learning_session_earns_nothing(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """Opening and closing an educational app twenty times must pay nothing (§3.4)."""
    policy = make_policy(earn_enabled=True, earn_min_session_min=10, earn_ratio=1.0)
    prime(ledger, 1)
    for _ in range(5):
        run(ledger, clock, 1, 5 * 60, category=Category.LEARNING, policy=policy)
        run(ledger, clock, 1, 30, category=Category.GAMES, policy=policy)
    assert ledger.state(1).earned_s == 0


def test_a_long_learning_session_earns_at_the_configured_ratio(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    policy = make_policy(earn_enabled=True, earn_min_session_min=10, earn_ratio=1.0)
    prime(ledger, 1)
    run(ledger, clock, 1, 30 * 60, category=Category.LEARNING, policy=policy)
    assert ledger.state(1).earned_s == 30 * 60


def test_earned_time_honours_the_daily_cap(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    policy = make_policy(
        earn_enabled=True, earn_min_session_min=10, earn_ratio=1.0, earn_cap_min=15
    )
    prime(ledger, 1)
    run(ledger, clock, 1, 60 * 60, category=Category.LEARNING, policy=policy)
    assert ledger.state(1).earned_s == 15 * 60


def test_idling_inside_a_learning_app_pauses_the_accrual(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    policy = make_policy(earn_enabled=True, earn_min_session_min=1, earn_ratio=1.0)
    prime(ledger, 1)
    run(ledger, clock, 1, 120, category=Category.LEARNING, policy=policy)
    earned_before = ledger.state(1).earned_s
    assert earned_before == 120

    run(ledger, clock, 1, 600, category=Category.LEARNING, idle=True, policy=policy)
    assert ledger.state(1).earned_s == earned_before
    assert ledger.state(1).general_used_s == 720, "idle still uses the general quota"


def test_earning_is_off_unless_the_parent_turned_it_on(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    policy = make_policy(earn_enabled=False)
    prime(ledger, 1)
    run(ledger, clock, 1, 30 * 60, category=Category.LEARNING, policy=policy)
    assert ledger.state(1).earned_s == 0


# ---------------------------------------------------------- category accounting


def test_category_seconds_are_attributed_per_app(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    prime(ledger, 1)
    run(ledger, clock, 1, 120, category=Category.LEARNING, app_slug="gcompris")
    run(ledger, clock, 1, 60, category=Category.GAMES, app_slug="supertux")
    ledger.persist()

    state = ledger.state(1)
    assert state.category_used(Category.LEARNING) == 120
    assert state.category_used(Category.GAMES) == 60
    assert state.general_used_s == 180


# ------------------------------------------------------------- parent actions


def test_granting_ten_minutes_raises_the_allowance_not_the_usage(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """A grant is extra allowance. Usage is a record of what happened and stands."""
    policy = make_policy(weekday_quota_min=60, weekend_quota_min=60)
    prime(ledger, 1)
    run(ledger, clock, 1, 20 * 60)

    ledger.grant(1, 600)
    state = ledger.state(1)

    assert state.general_used_s == 20 * 60
    assert state.granted_s == 600
    assert general_allowance_seconds(policy, state) == 70 * 60


def test_a_grant_is_worth_its_full_value_however_little_was_used(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    """The case that breaks a "subtract from usage" implementation."""
    policy = make_policy(weekday_quota_min=120, weekend_quota_min=120)
    prime(ledger, 1)
    run(ledger, clock, 1, 60)

    ledger.grant(1, 600)
    state = ledger.state(1)

    assert (
        general_allowance_seconds(policy, state) - state.general_used_s == 120 * 60 + 600 - 60
    )


def test_grants_accumulate_across_a_day(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    prime(ledger, 1)
    ledger.grant(1, 600)
    ledger.grant(1, 600)
    assert ledger.state(1).granted_s == 1200


def test_warnings_are_remembered_across_a_restart(
    db: Database, clock: FakeClock, config: Config, child: object
) -> None:
    """A daemon crash at minute nine must not replay the ten-minute warning."""
    day = clock.wall.date()
    with db.session() as first:
        ledger = TimeLedger(first, clock, config)
        ledger.begin()
        ledger.mark_warned(1, day, 10)
        ledger.persist()

    with db.session() as second:
        ledger = TimeLedger(second, clock, config)
        ledger.begin()
        assert ledger.already_warned(1, day, 10)
        assert not ledger.already_warned(1, day, 5)


# ---------------------------------------------------------------- day rollover


def test_usage_lands_on_the_calendar_day_it_happened(
    ledger: TimeLedger, clock: FakeClock, session: Session, child: object
) -> None:
    clock.wall = dt.datetime(2026, 9, 21, 23, 59, 30)
    prime(ledger, 1)
    run(ledger, clock, 1, 30)  # finishes at 00:00:00
    run(ledger, clock, 1, 60)  # the new day
    ledger.persist()

    monday = session.get(LedgerDay, {"child_id": 1, "day": dt.date(2026, 9, 21)})
    tuesday = session.get(LedgerDay, {"child_id": 1, "day": dt.date(2026, 9, 22)})
    assert monday is not None and tuesday is not None
    assert monday.general_s + tuesday.general_s == 90
    assert tuesday.general_s >= 60, "the new day starts its own count"


def test_a_new_day_starts_from_zero(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    prime(ledger, 1)
    run(ledger, clock, 1, 600)
    clock.wall += dt.timedelta(days=1)
    assert ledger.state(1).general_used_s == 0


def test_resetting_a_day_clears_every_counter(
    ledger: TimeLedger, clock: FakeClock, child: object
) -> None:
    prime(ledger, 1)
    run(ledger, clock, 1, 600)
    ledger.start_penalty(1, 300)
    ledger.mark_warned(1, clock.wall.date(), 10)

    ledger.reset_day(1)

    state = ledger.state(1)
    assert state.general_used_s == 0
    assert state.penalty_remaining_s == 0
    assert not ledger.already_warned(1, clock.wall.date(), 10)


# ---------------------------------------------------------------- boot epochs


def test_each_boot_gets_its_own_epoch_row(
    db: Database, clock: FakeClock, config: Config, child: object
) -> None:
    from biokids_core.db import BootEpoch

    with db.session() as s:
        TimeLedger(s, clock, config).begin()
        clock.reboot(off_for=60)
        TimeLedger(s, clock, config).begin()
        assert s.query(BootEpoch).count() == 2


def test_the_same_boot_seen_twice_reuses_its_epoch(
    db: Database, clock: FakeClock, config: Config, child: object
) -> None:
    from biokids_core.db import BootEpoch

    with db.session() as s:
        TimeLedger(s, clock, config).begin()
        clock.advance(600)
        TimeLedger(s, clock, config).begin()
        assert s.query(BootEpoch).count() == 1
        epoch = s.query(BootEpoch).one()
        assert epoch.last_boot_offset == pytest.approx(600, abs=1)
