"""T1.5 — the policy engine.

Two things are being tested. First, that every rule in the precedence list of
`ARCHITECTURE.md` §4 wins against everything below it: a test per rule, each one
setting up a state where *two* rules would fire and asserting the higher one
does. Second, the boundaries — a quota of exactly zero, a window at the exact
second it closes, midnight, and a child with no policy at all.
"""

from __future__ import annotations

import datetime as dt

import pytest

from biokids_core.policy import (
    _category_remaining,
    evaluate,
    finish_up_offer,
    general_allowance_seconds,
    learn_first_left,
    remaining_seconds,
)
from biokids_core.types import (
    Action,
    ActivityContext,
    AgeBand,
    Category,
    ChildPolicy,
    LedgerState,
    Reason,
    TimeWindow,
)
from helpers import AFTERNOON, MONDAY, SATURDAY, make_policy


def state(**overrides: object) -> LedgerState:
    base: dict[str, object] = {"child_id": 1, "day": MONDAY}
    base.update(overrides)
    return LedgerState(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------------ precedence


def test_penalty_beats_everything() -> None:
    """A timeout outranks an open window and a full quota alike."""
    policy = make_policy(weekday_windows=(TimeWindow.parse("16:00-20:00"),))
    decision = evaluate(
        policy,
        state(penalty_remaining_s=600, general_used_s=0),
        ActivityContext(),
        AFTERNOON,
    )
    assert decision.allow is False
    assert decision.action is Action.TIMEOUT
    assert decision.reason_code == Reason.PENALTY_ACTIVE
    assert decision.remaining_seconds == 0


def test_closed_window_beats_available_quota() -> None:
    """Hours of quota left does not buy a minute past bedtime."""
    policy = make_policy(weekday_windows=(TimeWindow.parse("16:00-20:00"),))
    at_2100 = dt.datetime(2026, 9, 21, 21, 0, 0)
    decision = evaluate(policy, state(general_used_s=0), ActivityContext(), at_2100)
    assert decision.action is Action.LOCK
    assert decision.reason_code == Reason.WINDOW_CLOSED
    assert decision.remaining_seconds == 0


def test_exhausted_quota_beats_a_due_break() -> None:
    """When both would lock, the child is told the day is over, not that it is break time."""
    policy = make_policy(break_enabled=True, break_work_min=40)
    decision = evaluate(
        policy,
        state(general_used_s=60 * 60, seconds_since_break_s=50 * 60),
        ActivityContext(),
        AFTERNOON,
    )
    assert decision.reason_code == Reason.QUOTA_EXHAUSTED


def test_running_break_beats_a_newly_due_break() -> None:
    policy = make_policy(break_enabled=True, break_work_min=40)
    decision = evaluate(
        policy,
        state(general_used_s=600, break_remaining_s=120, seconds_since_break_s=50 * 60),
        ActivityContext(),
        AFTERNOON,
    )
    assert decision.reason_code == Reason.BREAK_ACTIVE


def test_break_due_beats_category_limit() -> None:
    policy = make_policy(
        break_enabled=True,
        break_work_min=40,
        category_limits_min={Category.GAMES: 10},
    )
    decision = evaluate(
        policy,
        state(
            general_used_s=45 * 60,
            seconds_since_break_s=45 * 60,
            category_used_s={Category.GAMES: 20 * 60},
        ),
        ActivityContext(category=Category.GAMES),
        AFTERNOON,
    )
    assert decision.reason_code == Reason.BREAK_DUE
    assert decision.action is Action.LOCK


def test_category_limit_beats_a_disallowed_app() -> None:
    policy = make_policy(category_limits_min={Category.GAMES: 10})
    decision = evaluate(
        policy,
        state(category_used_s={Category.GAMES: 11 * 60}),
        ActivityContext(category=Category.GAMES, app_allowed=False),
        AFTERNOON,
    )
    assert decision.reason_code == Reason.CATEGORY_EXHAUSTED
    assert decision.action is Action.BLOCK


def test_disallowed_app_beats_a_blocked_url() -> None:
    decision = evaluate(
        make_policy(),
        state(),
        ActivityContext(app_allowed=False, url_blocked=True),
        AFTERNOON,
    )
    assert decision.reason_code == Reason.APP_NOT_ALLOWED


def test_everything_clear_allows() -> None:
    decision = evaluate(make_policy(), state(general_used_s=60), ActivityContext(), AFTERNOON)
    assert decision.allow is True
    assert decision.action is Action.NONE
    assert decision.reason_code == Reason.ALLOWED
    assert decision.remaining_seconds == 59 * 60


# --------------------------------------------------------------------- edges


def test_quota_of_exactly_zero_used_is_exhaustion_not_a_warning() -> None:
    """60 of 60 minutes used is over, not "0 minutes left"."""
    decision = evaluate(
        make_policy(), state(general_used_s=60 * 60), ActivityContext(), AFTERNOON
    )
    assert decision.allow is False
    assert decision.reason_code == Reason.QUOTA_EXHAUSTED
    assert decision.remaining_seconds == 0


def test_a_quota_of_zero_minutes_locks_immediately() -> None:
    """A parent who sets today's quota to nothing means nothing, not unlimited."""
    policy = make_policy(weekday_quota_min=0, weekend_quota_min=0)
    decision = evaluate(policy, state(general_used_s=0), ActivityContext(), AFTERNOON)
    assert decision.allow is False
    assert decision.reason_code == Reason.QUOTA_EXHAUSTED


def test_one_second_left_still_allows() -> None:
    decision = evaluate(
        make_policy(), state(general_used_s=60 * 60 - 1), ActivityContext(), AFTERNOON
    )
    assert decision.allow is True
    assert decision.remaining_seconds == 1
    assert decision.remaining_minutes == 1


@pytest.mark.parametrize(
    ("moment", "expect_open"),
    [
        (dt.datetime(2026, 9, 21, 15, 59, 59), False),
        (dt.datetime(2026, 9, 21, 16, 0, 0), True),  # inclusive start
        (dt.datetime(2026, 9, 21, 19, 59, 59), True),
        (dt.datetime(2026, 9, 21, 20, 0, 0), False),  # exclusive end
    ],
)
def test_window_boundaries_are_exact_to_the_second(
    moment: dt.datetime, expect_open: bool
) -> None:
    policy = make_policy(weekday_windows=(TimeWindow.parse("16:00-20:00"),))
    decision = evaluate(policy, state(), ActivityContext(), moment)
    assert decision.allow is expect_open


def test_a_window_that_wraps_midnight_is_open_on_both_sides() -> None:
    policy = make_policy(weekday_windows=(TimeWindow.parse("20:00-06:00"),))
    assert policy.window_open(dt.datetime(2026, 9, 21, 23, 0))
    assert policy.window_open(dt.datetime(2026, 9, 21, 2, 0))
    assert not policy.window_open(dt.datetime(2026, 9, 21, 12, 0))


def test_remaining_time_is_capped_by_the_window_not_just_the_quota() -> None:
    """Two hours of quota and twenty minutes of window means twenty minutes."""
    policy = make_policy(
        weekday_quota_min=120, weekday_windows=(TimeWindow.parse("16:00-20:00"),)
    )
    at_1940 = dt.datetime(2026, 9, 21, 19, 40, 0)
    decision = evaluate(policy, state(general_used_s=0), ActivityContext(), at_1940)
    assert decision.remaining_seconds == 20 * 60


def test_penalty_overlapping_an_open_window_reports_no_time_left() -> None:
    policy = make_policy(weekday_windows=(TimeWindow.parse("16:00-20:00"),))
    assert (
        remaining_seconds(policy, state(penalty_remaining_s=300), ActivityContext(), AFTERNOON)
        == 0
    )


def test_weekend_quota_applies_on_saturday() -> None:
    policy = make_policy(weekday_quota_min=30, weekend_quota_min=120)
    saturday_state = LedgerState(child_id=1, day=SATURDAY)
    saturday_afternoon = dt.datetime(2026, 9, 19, 17, 0)
    decision = evaluate(policy, saturday_state, ActivityContext(), saturday_afternoon)
    assert decision.remaining_seconds == 120 * 60


def test_midnight_rollover_uses_the_new_day_quota_and_a_fresh_ledger() -> None:
    """Saturday 23:59 and Sunday 00:00 are different days with different quotas."""
    policy = make_policy(weekday_quota_min=30, weekend_quota_min=120)
    saturday_night = dt.datetime(2026, 9, 19, 23, 59, 30)
    sunday_morning = dt.datetime(2026, 9, 20, 0, 0, 30)

    before = evaluate(
        policy,
        LedgerState(child_id=1, day=SATURDAY, general_used_s=119 * 60),
        ActivityContext(),
        saturday_night,
    )
    after = evaluate(
        policy,
        LedgerState(child_id=1, day=dt.date(2026, 9, 20), general_used_s=0),
        ActivityContext(),
        sunday_morning,
    )
    assert before.remaining_seconds == 60
    assert after.remaining_seconds == 120 * 60


def test_a_child_with_no_policy_is_never_locked_out() -> None:
    """An unmanaged account — the parent's own, or a child never configured."""
    decision = evaluate(None, state(), ActivityContext(), AFTERNOON)
    assert decision.allow is True
    assert decision.action is Action.NONE
    assert decision.reason_code == Reason.NO_POLICY


def test_a_disabled_policy_is_the_same_as_no_policy() -> None:
    decision = evaluate(
        make_policy(enabled=False), state(general_used_s=10**6), ActivityContext(), AFTERNOON
    )
    assert decision.allow is True
    assert decision.reason_code == Reason.NO_POLICY


# ------------------------------------------------------------------ warnings


@pytest.mark.parametrize(
    ("used_minutes", "expected_event"),
    [
        (49, None),  # 11 minutes left, nothing crossed
        (50, "warn_10"),  # exactly 10
        (51, "warn_10"),
        (55, "warn_5"),  # exactly 5
        (56, "warn_5"),
        (59, "warn_1"),  # exactly 1
    ],
)
def test_warning_picks_the_nearest_threshold_crossed(
    used_minutes: int, expected_event: str | None
) -> None:
    decision = evaluate(
        make_policy(), state(general_used_s=used_minutes * 60), ActivityContext(), AFTERNOON
    )
    if expected_event is None:
        assert decision.action is Action.NONE
        assert decision.speak is None
    else:
        assert decision.action is Action.WARN
        assert decision.speak is not None
        assert decision.speak.event == expected_event
        assert decision.allow is True, "a warning never stops the child"


def test_the_last_warning_is_high_priority_so_it_is_never_dropped() -> None:
    decision = evaluate(
        make_policy(), state(general_used_s=59 * 60 + 30), ActivityContext(), AFTERNOON
    )
    assert decision.speak is not None
    assert decision.speak.priority == "high"


def test_no_warning_when_the_child_is_not_at_the_machine() -> None:
    decision = evaluate(
        make_policy(),
        state(general_used_s=55 * 60),
        ActivityContext(session_active=False),
        AFTERNOON,
    )
    assert decision.speak is None


def test_a_warning_fires_on_the_window_closing_too() -> None:
    """The ten-minute warning must work when it is the window, not the quota, ending."""
    policy = make_policy(
        weekday_quota_min=600, weekday_windows=(TimeWindow.parse("16:00-20:00"),)
    )
    at_1955 = dt.datetime(2026, 9, 21, 19, 55, 0)
    decision = evaluate(policy, state(), ActivityContext(), at_1955)
    assert decision.speak is not None
    # The last window closing at eight is a night ending: the night's words (D-214).
    assert decision.speak.event == "bedtime_5"

    at_1355 = dt.datetime(2026, 9, 21, 13, 55, 0)
    daytime = make_policy(
        weekday_quota_min=600, weekday_windows=(TimeWindow.parse("10:00-14:00"),)
    )
    decision = evaluate(daytime, state(), ActivityContext(), at_1355)
    assert decision.speak is not None
    assert decision.speak.event == "warn_5"


# -------------------------------------------------------------- earned time


def test_earned_minutes_are_spendable_somewhere() -> None:
    """The bug this replaces: earned time bought nothing, in every configuration.

    D-45 puts earned minutes into Oyun, and the old rule read that literally -
    `earn_spendable_general` false meant "raise the Games ceiling". But nothing
    in the product ever writes a Games limit (`CategoryLimit` has no writer, and
    per-category limits are D-44, not Phase 0), so there was no ceiling to
    raise; and nothing ever writes `earn_spendable_general` either, so the other
    branch was unreachable too.

    The result, in every installation a parent could actually produce: the child
    does their homework, the daemon congratulates them, the panel shows the
    minutes, and their remaining time does not move by one second.
    """
    policy = make_policy(earn_enabled=True, earn_spendable_general=False)
    assert policy.category_limits_min == {}, "the configuration every family has"

    allowance = general_allowance_seconds(policy, state(earned_s=30 * 60))

    assert allowance == 90 * 60, "thirty earned minutes have to be worth thirty minutes"


def test_earned_minutes_go_to_games_when_there_is_a_games_limit_to_raise() -> None:
    """With a limit set, D-45 applies as written and the general quota is untouched."""
    policy = make_policy(
        earn_enabled=True,
        earn_spendable_general=False,
        category_limits_min={Category.GAMES: 20},
    )
    assert general_allowance_seconds(policy, state(earned_s=30 * 60)) == 60 * 60


def test_earned_minutes_are_never_counted_in_both_places() -> None:
    """Thirty earned minutes are thirty minutes, not sixty.

    The two branches are written as one condition and its negation so this
    cannot drift, and this is the test that says why that matters.
    """
    limited = make_policy(
        earn_enabled=True,
        earn_spendable_general=True,
        category_limits_min={Category.GAMES: 20},
    )
    earned = state(earned_s=30 * 60)

    assert general_allowance_seconds(limited, earned) == 90 * 60
    games_left = _category_remaining(limited, earned, Category.GAMES)
    assert games_left == 20 * 60, "the same minutes were added to Games as well"


def test_earning_switched_off_adds_nothing_anywhere() -> None:
    policy = make_policy(earn_enabled=False, category_limits_min={Category.GAMES: 20})
    earned = state(earned_s=30 * 60)
    assert general_allowance_seconds(policy, earned) == 60 * 60
    assert _category_remaining(policy, earned, Category.GAMES) == 20 * 60


def test_earned_minutes_extend_the_general_quota_when_the_parent_says_so() -> None:
    policy = make_policy(earn_enabled=True, earn_spendable_general=True)
    assert general_allowance_seconds(policy, state(earned_s=30 * 60)) == 90 * 60


def test_earned_minutes_are_capped_even_if_the_ledger_holds_more() -> None:
    policy = make_policy(earn_enabled=True, earn_spendable_general=True, earn_cap_min=15)
    assert general_allowance_seconds(policy, state(earned_s=99 * 60)) == 75 * 60


def test_earned_minutes_raise_the_games_ceiling() -> None:
    policy = make_policy(
        earn_enabled=True,
        earn_spendable_general=False,
        category_limits_min={Category.GAMES: 20},
    )
    ctx = ActivityContext(category=Category.GAMES)
    used = state(earned_s=15 * 60, category_used_s={Category.GAMES: 25 * 60})
    decision = evaluate(policy, used, ctx, AFTERNOON)
    assert decision.allow is True, "20 minutes of limit plus 15 earned is 35"


# ---------------------------------------------------------- types behaviour


def test_time_window_rejects_an_empty_range() -> None:
    with pytest.raises(ValueError, match="may not be empty"):
        TimeWindow.parse("16:00-16:00")


@pytest.mark.parametrize("text", ["1600-2000", "16:00", "25:00-26:00", "16:60-17:00", ""])
def test_time_window_rejects_malformed_text(text: str) -> None:
    with pytest.raises(ValueError):
        TimeWindow.parse(text)


def test_time_window_round_trips_through_text() -> None:
    assert str(TimeWindow.parse(" 16:00 - 20:00 ")) == "16:00-20:00"


def test_age_band_boundary_is_seven_to_eight() -> None:
    assert AgeBand.for_age(7) is AgeBand.YOUNG
    assert AgeBand.for_age(8) is AgeBand.OLDER


def test_no_windows_means_the_whole_day_is_allowed() -> None:
    policy = make_policy()
    assert policy.window_open(dt.datetime(2026, 9, 21, 3, 0))
    assert policy.seconds_until_window_closes(dt.datetime(2026, 9, 21, 3, 0)) is None


def test_several_windows_in_one_day_are_all_honoured() -> None:
    policy = make_policy(
        weekday_windows=(TimeWindow.parse("08:00-09:00"), TimeWindow.parse("16:00-20:00"))
    )
    assert policy.window_open(dt.datetime(2026, 9, 21, 8, 30))
    assert not policy.window_open(dt.datetime(2026, 9, 21, 12, 0))
    assert policy.window_open(dt.datetime(2026, 9, 21, 19, 0))


def test_decision_rounds_remaining_minutes_up() -> None:
    decision = evaluate(
        make_policy(), state(general_used_s=59 * 60 - 1), ActivityContext(), AFTERNOON
    )
    assert decision.remaining_seconds == 61
    assert decision.remaining_minutes == 2


def test_reason_codes_are_keys_not_sentences() -> None:
    """The engine must never build user-facing text (ARCHITECTURE.md §4)."""
    policy = ChildPolicy(child_id=1)
    cases = [
        (state(penalty_remaining_s=1), ActivityContext()),
        (state(general_used_s=10**6), ActivityContext()),
        (state(), ActivityContext(app_allowed=False)),
        (state(), ActivityContext(url_blocked=True)),
    ]
    for ledger_state, ctx in cases:
        code = evaluate(policy, ledger_state, ctx, AFTERNOON).reason_code
        assert " " not in code
        assert code.islower()


# ---------------------------------------------- a pause between windows is not bedtime


def test_a_gap_between_two_windows_is_not_the_end_of_the_day() -> None:
    """ "07:30-08:15 before school, 16:00-19:00 after school" is how windows get used.

    Every closed window produced WINDOW_CLOSED, which the daemon routes to the
    end action - poweroff by default. So the machine shut itself down at 08:17
    while the child was told "see you tomorrow", with their whole daily quota
    intact and a window opening at four.
    """
    policy = make_policy(
        weekday_windows=(TimeWindow.parse("07:30-08:15"), TimeWindow.parse("16:00-19:00"))
    )
    at_0830 = dt.datetime(2026, 9, 21, 8, 30, 0)

    decision = evaluate(policy, state(), ActivityContext(), at_0830)

    assert decision.allow is False, "the child may not use it between windows"
    assert decision.reason_code == Reason.WINDOW_WAITING
    assert decision.reason_code != Reason.WINDOW_CLOSED, "this would power the machine off"
    assert decision.speak is not None and decision.speak.event == "window_waiting"


def test_after_the_last_window_the_day_really_is_over() -> None:
    policy = make_policy(
        weekday_windows=(TimeWindow.parse("07:30-08:15"), TimeWindow.parse("16:00-19:00"))
    )
    at_2100 = dt.datetime(2026, 9, 21, 21, 0, 0)

    decision = evaluate(policy, state(), ActivityContext(), at_2100)

    assert decision.reason_code == Reason.WINDOW_CLOSED
    # Nine in the evening: good night rather than "that is all for today" (D-214).
    assert decision.speak is not None and decision.speak.event == "bedtime"


def test_before_the_only_window_of_the_day_the_child_is_waiting_not_finished() -> None:
    """Morning, before the window opens. Nothing about that is the end of a day."""
    policy = make_policy(weekday_windows=(TimeWindow.parse("16:00-19:00"),))
    at_0900 = dt.datetime(2026, 9, 21, 9, 0, 0)

    decision = evaluate(policy, state(), ActivityContext(), at_0900)

    assert decision.reason_code == Reason.WINDOW_WAITING


def test_seconds_until_the_next_window_opens() -> None:
    policy = make_policy(
        weekday_windows=(TimeWindow.parse("07:30-08:15"), TimeWindow.parse("16:00-19:00"))
    )
    at_0830 = dt.datetime(2026, 9, 21, 8, 30)
    assert policy.seconds_until_window_opens(at_0830) == 7 * 3600 + 30 * 60
    assert policy.seconds_until_window_opens(dt.datetime(2026, 9, 21, 21, 0)) is None
    assert make_policy().seconds_until_window_opens(dt.datetime(2026, 9, 21, 9, 0)) is None


# ------------------------------------------------------ D-176 a schedule per day


def test_a_day_of_its_own_wins_over_weekday_and_weekend() -> None:
    from biokids_core.types import ChildPolicy, TimeWindow

    policy = ChildPolicy(
        child_id=1,
        weekday_quota_min=60,
        weekend_quota_min=120,
        weekday_windows=(TimeWindow.parse("16:00-20:00"),),
        day_quota_min={2: 30},  # Wednesday: swimming
        day_windows={2: (TimeWindow.parse("18:00-19:00"),)},
    )
    wednesday = dt.date(2026, 10, 7)
    thursday = dt.date(2026, 10, 8)
    assert policy.quota_minutes(wednesday) == 30
    assert policy.quota_minutes(thursday) == 60
    assert not policy.window_open(dt.datetime(2026, 10, 7, 17, 0))
    assert policy.window_open(dt.datetime(2026, 10, 8, 17, 0))


def test_a_day_that_names_only_its_minutes_keeps_the_usual_hours() -> None:
    from biokids_core.types import ChildPolicy, TimeWindow

    policy = ChildPolicy(
        child_id=1,
        weekend_windows=(TimeWindow.parse("10:00-20:00"),),
        day_quota_min={6: 30},
    )
    sunday = dt.datetime(2026, 10, 11, 21, 0)
    assert policy.quota_minutes(sunday.date()) == 30
    assert not policy.window_open(sunday)


# ------------------------------------------------- the last round (D-230)


def test_the_last_round_is_offered_in_the_days_last_two_minutes() -> None:
    """A one-hour day with 90 seconds left: three minutes are on offer."""
    policy = make_policy(finish_up_min=3)
    assert (
        finish_up_offer(
            policy, state(general_used_s=60 * 60 - 90), ActivityContext(), AFTERNOON
        )
        == 180
    )


@pytest.mark.parametrize(
    ("used", "why"),
    [
        (60 * 60 - 5 * 60, "five minutes left is not the end yet"),
        (60 * 60, "the time is already up: the screen is locked, nobody can tap"),
    ],
)
def test_the_last_round_waits_for_the_end(used: int, why: str) -> None:
    offer = finish_up_offer(
        make_policy(), state(general_used_s=used), ActivityContext(), AFTERNOON
    )
    assert offer == 0, why


def test_the_last_round_is_once_a_day() -> None:
    near_end = state(general_used_s=60 * 60 - 60, finish_up_s=180)
    assert finish_up_offer(make_policy(), near_end, ActivityContext(), AFTERNOON) == 0


def test_a_parent_can_turn_the_last_round_off() -> None:
    near_end = state(general_used_s=60 * 60 - 60)
    assert (
        finish_up_offer(make_policy(finish_up_min=0), near_end, ActivityContext(), AFTERNOON)
        == 0
    )


def test_the_last_round_is_not_offered_when_minutes_cannot_help() -> None:
    """Allowance moves the quota, not a closing window, a used-up kind of app or
    a penalty; offering it there would be a button that does nothing."""
    near_end = state(general_used_s=60 * 60 - 60)
    closing = make_policy(weekday_windows=(TimeWindow.parse("16:00-17:00"),))
    at_five_to = dt.datetime(2026, 9, 21, 16, 59, 30)
    assert finish_up_offer(closing, near_end, ActivityContext(), at_five_to) == 0

    games = make_policy(category_limits_min={Category.GAMES: 20})
    games_out = state(general_used_s=60 * 60 - 60, category_used_s={Category.GAMES: 20 * 60})
    assert (
        finish_up_offer(games, games_out, ActivityContext(category=Category.GAMES), AFTERNOON)
        == 0
    )

    penalised = state(general_used_s=60 * 60 - 60, penalty_remaining_s=300)
    assert finish_up_offer(make_policy(), penalised, ActivityContext(), AFTERNOON) == 0


def test_the_last_round_is_allowance_like_a_grant() -> None:
    policy = make_policy()
    taken = state(general_used_s=60 * 60, finish_up_s=180)
    assert general_allowance_seconds(policy, taken) == 63 * 60
    decision = evaluate(policy, taken, ActivityContext(), AFTERNOON)
    assert decision.allow is True
    assert decision.remaining_seconds == 180


# ------------------------------------------------ games after learning (D-232)


def test_games_wait_for_the_days_learning() -> None:
    policy = make_policy(learn_first_min=15)
    early = state(category_used_s={Category.LEARNING: 10 * 60})
    decision = evaluate(policy, early, ActivityContext(category=Category.GAMES), AFTERNOON)
    assert decision.allow is False
    assert decision.action is Action.BLOCK
    assert decision.reason_code == Reason.LEARN_FIRST
    assert learn_first_left(policy, early) == 5 * 60


def test_learning_and_everything_else_are_open_meanwhile() -> None:
    policy = make_policy(learn_first_min=15)
    for category in (Category.LEARNING, Category.WEB, Category.FREE, None):
        decision = evaluate(policy, state(), ActivityContext(category=category), AFTERNOON)
        assert decision.allow is True, category


def test_games_open_once_the_learning_is_done() -> None:
    policy = make_policy(learn_first_min=15)
    done = state(category_used_s={Category.LEARNING: 15 * 60})
    assert learn_first_left(policy, done) == 0
    decision = evaluate(policy, done, ActivityContext(category=Category.GAMES), AFTERNOON)
    assert decision.allow is True


def test_used_up_games_say_so_before_learn_first() -> None:
    """Out of game time, a child is not told to go and learn first."""
    policy = make_policy(learn_first_min=15, category_limits_min={Category.GAMES: 10})
    over = state(category_used_s={Category.GAMES: 10 * 60})
    decision = evaluate(policy, over, ActivityContext(category=Category.GAMES), AFTERNOON)
    assert decision.reason_code == Reason.CATEGORY_EXHAUSTED


def test_no_rule_no_wait() -> None:
    assert learn_first_left(make_policy(), state()) == 0
    decision = evaluate(
        make_policy(), state(), ActivityContext(category=Category.GAMES), AFTERNOON
    )
    assert decision.allow is True


def test_learning_first_never_asks_for_more_than_its_limit() -> None:
    """D-250: learn first 20 with learning limited to 15 kept games shut all day."""
    policy = make_policy(learn_first_min=20, category_limits_min={Category.LEARNING: 15})
    done = state(category_used_s={Category.LEARNING: 15 * 60})
    assert learn_first_left(policy, done) == 0
    decision = evaluate(policy, done, ActivityContext(category=Category.GAMES), AFTERNOON)
    assert decision.allow is True
