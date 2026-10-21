"""D-215: a special day, and a holiday of several.

The policy engine is where these matter, so most of this is the engine with
special days in it: today only, a range on the weekend's rules, a quota of its
own, "no daily limit" with the allowed hours still in force, two that overlap,
one taken back, and the midnight on either side. Granted minutes, earned minutes
and the limits per kind of app keep working exactly as they did - a special day
changes the day's quota and hours and nothing else.

Then the rules about time: nothing is ever made special in the past, and a
holiday cancelled half-way keeps the days it was already enforced on. Then the
panel, which writes them, audits them and tells the daemon; and the daemon,
which lets a child whose day just got longer back in.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core import repo, retention, special_days
from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import AuditLog, Child, Database, DayOverride, Parent, Policy
from biokids_core.db.models import utcnow
from biokids_core.policy import evaluate, general_allowance_seconds
from biokids_core.types import (
    NO_DAILY_LIMIT_MINUTES,
    ActivityContext,
    Category,
    EndAction,
    Reason,
    SpecialDay,
    TimeWindow,
)
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.supervisor import Supervisor
from helpers import MONDAY, SATURDAY, make_policy, make_state
from test_daemon import LoginctlAwareRunner
from test_panel import PIN, FakeDaemon, login

TUESDAY = MONDAY + dt.timedelta(days=1)
TICK = 10


def today(day: dt.date = MONDAY, **fields: Any) -> SpecialDay:
    return SpecialDay(start=day, end=day, **fields)


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hour, minute))


# ------------------------------------------------------------- the engine


def test_an_ordinary_day_is_not_special() -> None:
    assert make_policy().special_on(MONDAY) is None
    assert make_policy().quota_minutes(MONDAY) == 60


def test_extra_minutes_today_and_only_today() -> None:
    policy = make_policy(special_days=(today(extra_minutes=60),))
    assert policy.quota_minutes(MONDAY) == 120
    assert policy.quota_minutes(TUESDAY) == 60, "tomorrow is an ordinary day"
    assert policy.quota_minutes(MONDAY - dt.timedelta(days=1)) == 60


def test_the_extra_hour_is_what_the_engine_counts_against() -> None:
    policy = make_policy(special_days=(today(extra_minutes=60),))
    state = make_state(general_used_s=90 * 60)
    decision = evaluate(policy, state, ActivityContext(), at(MONDAY, 16))
    assert decision.allow is True
    assert decision.remaining_seconds == 30 * 60


def test_no_daily_limit_keeps_the_allowed_hours() -> None:
    policy = make_policy(
        weekday_windows=(TimeWindow.parse("16:00-20:00"),),
        special_days=(today(no_limit=True),),
    )
    assert policy.quota_minutes(MONDAY) == NO_DAILY_LIMIT_MINUTES
    lots = make_state(general_used_s=5 * 3600)
    afternoon = evaluate(policy, lots, ActivityContext(), at(MONDAY, 19))
    assert afternoon.allow is True, "five hours used, and still no limit"
    assert afternoon.remaining_seconds == 3600, "the window still closes at eight"
    night = evaluate(policy, lots, ActivityContext(), at(MONDAY, 21))
    assert night.reason_code == Reason.WINDOW_CLOSED


def test_no_limit_cannot_run_out_before_the_day_does() -> None:
    """Every second of a calendar day still leaves time on a no-limit day."""
    policy = make_policy(special_days=(today(no_limit=True),))
    used_all_day = make_state(general_used_s=23 * 3600 + 59 * 60)
    decision = evaluate(policy, used_all_day, ActivityContext(), at(MONDAY, 23, 59))
    assert decision.allow is True


def test_a_holiday_follows_the_weekends_quota_and_hours() -> None:
    policy = make_policy(
        weekday_quota_min=60,
        weekend_quota_min=180,
        weekday_windows=(TimeWindow.parse("16:00-19:00"),),
        weekend_windows=(TimeWindow.parse("09:00-21:00"),),
        day_quota_min={0: 45},
        special_days=(
            SpecialDay(start=MONDAY, end=MONDAY + dt.timedelta(days=4), weekend_rules=True),
        ),
    )
    assert policy.quota_minutes(MONDAY) == 180, "the weekend's, over Monday's own 45"
    assert policy.windows(MONDAY) == (TimeWindow.parse("09:00-21:00"),)
    morning = evaluate(policy, make_state(), ActivityContext(), at(MONDAY, 10))
    assert morning.allow is True, "a holiday morning is open, as a Saturday's is"


def test_a_saturday_inside_a_holiday_is_still_that_saturday() -> None:
    """The weekend rule is for weekdays; Saturday keeps what the parent set for it."""
    policy = make_policy(
        weekend_quota_min=120,
        day_quota_min={5: 240},
        special_days=(
            SpecialDay(start=SATURDAY - dt.timedelta(days=2), end=SATURDAY, weekend_rules=True),
        ),
    )
    assert policy.quota_minutes(SATURDAY) == 240


def test_a_range_with_its_own_quota() -> None:
    policy = make_policy(
        weekday_windows=(TimeWindow.parse("16:00-19:00"),),
        special_days=(
            SpecialDay(start=MONDAY, end=MONDAY + dt.timedelta(days=2), quota_minutes=150),
        ),
    )
    assert policy.quota_minutes(MONDAY + dt.timedelta(days=2)) == 150
    assert policy.quota_minutes(MONDAY + dt.timedelta(days=3)) == 60
    assert policy.windows(MONDAY) == (TimeWindow.parse("16:00-19:00"),), "the usual hours"


def test_a_tap_inside_a_holiday_adds_to_it() -> None:
    policy = make_policy(
        weekend_quota_min=180,
        special_days=(
            SpecialDay(
                start=MONDAY, end=MONDAY + dt.timedelta(days=4), weekend_rules=True, order=1
            ),
            today(extra_minutes=60, order=2),
        ),
    )
    assert policy.quota_minutes(MONDAY) == 240
    assert policy.quota_minutes(TUESDAY) == 180


def test_of_two_quotas_of_their_own_the_later_one_wins() -> None:
    policy = make_policy(
        special_days=(
            SpecialDay(start=MONDAY, end=TUESDAY, quota_minutes=200, order=7),
            SpecialDay(start=MONDAY, end=MONDAY, quota_minutes=90, order=3),
        ),
    )
    assert policy.quota_minutes(MONDAY) == 200
    assert policy.quota_minutes(TUESDAY) == 200


def test_taking_a_special_day_back_is_the_usual_rules_again() -> None:
    special = make_policy(special_days=(today(extra_minutes=120),))
    taken_back = make_policy()
    state = make_state(general_used_s=70 * 60)
    assert evaluate(special, state, ActivityContext(), at(MONDAY, 17)).allow is True
    decision = evaluate(taken_back, state, ActivityContext(), at(MONDAY, 17))
    assert decision.reason_code == Reason.QUOTA_EXHAUSTED


def test_midnight_ends_a_special_day() -> None:
    policy = make_policy(
        weekday_windows=(TimeWindow.parse("16:00-23:00"),),
        special_days=(
            SpecialDay(start=MONDAY - dt.timedelta(days=2), end=MONDAY, weekend_rules=True),
        ),
        weekend_windows=(),
        weekend_quota_min=600,
    )
    last_minute = evaluate(
        policy,
        make_state(general_used_s=0),
        ActivityContext(),
        dt.datetime.combine(MONDAY, dt.time(23, 59)),
    )
    assert last_minute.allow is True, "the weekend's hours: the whole day"
    after = evaluate(
        policy,
        make_state(day=TUESDAY),
        ActivityContext(),
        dt.datetime.combine(TUESDAY, dt.time(0, 0)),
    )
    assert after.reason_code in {Reason.WINDOW_WAITING, Reason.WINDOW_CLOSED}
    assert policy.quota_minutes(TUESDAY) == 60


def test_granted_and_earned_minutes_still_add_on_top() -> None:
    policy = make_policy(
        earn_enabled=True,
        earn_spendable_general=True,
        special_days=(today(extra_minutes=30),),
    )
    state = make_state(granted_s=10 * 60, earned_s=5 * 60)
    assert general_allowance_seconds(policy, state) == (60 + 30 + 10 + 5) * 60


def test_the_limits_per_kind_of_app_still_apply_on_a_special_day() -> None:
    policy = make_policy(
        category_limits_min={Category.GAMES: 30}, special_days=(today(no_limit=True),)
    )
    state = make_state(general_used_s=2 * 3600, category_used_s={Category.GAMES: 30 * 60})
    games = evaluate(policy, state, ActivityContext(category=Category.GAMES), at(MONDAY, 17))
    assert games.reason_code == Reason.CATEGORY_EXHAUSTED
    learning = evaluate(
        policy, state, ActivityContext(category=Category.LEARNING), at(MONDAY, 17)
    )
    assert learning.allow is True


def test_a_negative_extra_from_a_hand_edited_row_takes_nothing_away() -> None:
    policy = make_policy(special_days=(today(extra_minutes=-60),))
    assert policy.quota_minutes(MONDAY) == 60


# ------------------------------------------------ never the past, kept as history


def test_a_tap_replaces_the_earlier_tap_and_leaves_a_holiday(
    session: Session, child: Child
) -> None:
    holiday = special_days.add_range(
        session, child.id, MONDAY, start=MONDAY, end=TUESDAY, weekend_rules=True
    )
    first, _ = special_days.set_today(session, child.id, MONDAY, extra_minutes=30)
    second, replaced = special_days.set_today(session, child.id, MONDAY, extra_minutes=60)
    session.commit()

    rows = special_days.current(session, child.id, MONDAY)
    assert {row.id for row in rows} == {holiday.id, second.id}
    assert replaced == [
        {
            "start": "2026-09-21",
            "end": "2026-09-21",
            "extra_minutes": 30,
            "quota_minutes": None,
            "no_limit": False,
            "weekend_rules": False,
            "label": "",
        }
    ]
    assert first.id not in {row.id for row in rows}


def test_a_tap_must_be_one_of_the_choices(session: Session, child: Child) -> None:
    with pytest.raises(special_days.SpecialDayError):
        special_days.set_today(session, child.id, MONDAY, extra_minutes=45)
    row, _ = special_days.set_today(session, child.id, MONDAY, no_limit=True, extra_minutes=999)
    assert row.no_limit and row.extra_minutes == 0


def test_a_holiday_entered_on_its_third_day_starts_today(
    session: Session, child: Child
) -> None:
    row = special_days.add_range(
        session,
        child.id,
        MONDAY,
        start=MONDAY - dt.timedelta(days=2),
        end=MONDAY + dt.timedelta(days=5),
        weekend_rules=True,
        label="  Ara   tatil  ",
    )
    assert row.start_day == MONDAY, "the days behind were enforced as they were"
    assert row.label == "Ara tatil"


@pytest.mark.parametrize(
    ("start", "end", "error"),
    [
        (MONDAY - dt.timedelta(days=5), MONDAY - dt.timedelta(days=1), "special.error.past"),
        (TUESDAY, MONDAY, "special.error.order"),
        (MONDAY, MONDAY + dt.timedelta(days=100), "special.error.too_long"),
    ],
)
def test_a_holiday_that_cannot_be_is_refused(
    session: Session, child: Child, start: dt.date, end: dt.date, error: str
) -> None:
    with pytest.raises(special_days.SpecialDayError, match=error):
        special_days.add_range(
            session, child.id, MONDAY, start=start, end=end, weekend_rules=True
        )


def test_a_holiday_has_exactly_one_rule(session: Session, child: Child) -> None:
    for weekend, quota in ((False, None), (True, 120)):
        with pytest.raises(special_days.SpecialDayError):
            special_days.add_range(
                session,
                child.id,
                MONDAY,
                start=MONDAY,
                end=TUESDAY,
                weekend_rules=weekend,
                quota_minutes=quota,
            )


def test_cancelling_half_way_keeps_the_days_already_over(
    session: Session, child: Child
) -> None:
    started = MONDAY - dt.timedelta(days=3)
    row = special_days.add_range(
        session, child.id, started, start=started, end=TUESDAY, weekend_rules=True
    )
    session.commit()

    before = special_days.cancel(session, child.id, row.id, MONDAY)
    session.commit()

    assert before is not None and before["end"] == TUESDAY.isoformat()
    kept = session.get(DayOverride, row.id)
    assert kept is not None
    assert (kept.start_day, kept.end_day) == (started, MONDAY - dt.timedelta(days=1))
    policy = repo.load_policy(session, child, today=MONDAY)
    assert policy is not None
    assert policy.special_on(MONDAY - dt.timedelta(days=1)) is not None, "history keeps it"
    assert policy.special_on(MONDAY) is None, "today is ordinary again"


def test_cancelling_one_that_has_not_started_removes_it(session: Session, child: Child) -> None:
    row = special_days.add_range(
        session, child.id, MONDAY, start=TUESDAY, end=TUESDAY, weekend_rules=True
    )
    assert special_days.cancel(session, child.id, row.id, MONDAY) is not None
    assert session.get(DayOverride, row.id) is None


def test_a_special_day_that_is_over_cannot_be_cancelled(session: Session, child: Child) -> None:
    yesterday = MONDAY - dt.timedelta(days=1)
    row, _ = special_days.set_today(session, child.id, yesterday, extra_minutes=60)
    assert special_days.cancel(session, child.id, row.id, MONDAY) is None
    assert session.get(DayOverride, row.id) is not None


def test_another_childs_special_day_cannot_be_cancelled(session: Session, child: Child) -> None:
    row, _ = special_days.set_today(session, child.id, MONDAY, extra_minutes=60)
    assert special_days.cancel(session, child.id + 1, row.id, MONDAY) is None


def test_the_policy_the_daemon_loads_carries_them(session: Session, child: Child) -> None:
    special_days.set_today(session, child.id, MONDAY, extra_minutes=120)
    session.commit()
    policy = repo.load_policy(session, child, today=MONDAY)
    assert policy is not None
    assert policy.quota_minutes(MONDAY) == policy.weekday_quota_min + 120


def test_they_are_kept_with_the_minutes_and_then_go(
    session: Session, child: Child, config: Config
) -> None:
    now = dt.datetime(2027, 6, 1, 12, 0)
    old = now.date() - dt.timedelta(days=config.retention.usage_days + 1)
    recent = now.date() - dt.timedelta(days=10)
    session.add_all(
        [
            DayOverride(child_id=child.id, start_day=old, end_day=old, extra_minutes=30),
            DayOverride(child_id=child.id, start_day=recent, end_day=recent, extra_minutes=30),
        ]
    )
    session.commit()
    deleted = retention.prune(session, config, now=now)
    session.commit()
    assert deleted["day_override"] == 1
    assert [row.start_day for row in session.scalars(select(DayOverride))] == [recent]


def test_they_go_with_the_child(session: Session, child: Child) -> None:
    special_days.set_today(session, child.id, MONDAY, extra_minutes=30)
    session.commit()
    session.delete(child)
    session.commit()
    assert session.scalars(select(DayOverride)).all() == []


# ------------------------------------------------------------------ the panel


@pytest.fixture
def daemon() -> FakeDaemon:
    fake = FakeDaemon()
    fake.status = {
        "children": [
            {
                "child_id": 1,
                "display_name": "Elif",
                "avatar": "fox",
                "managed": True,
                "online": True,
                "app": "",
                "remaining_seconds": 42 * 60,
                "remaining_minutes": 42,
                "quota_seconds": 60 * 60,
                "granted_seconds": 0,
                "used_seconds": 18 * 60,
                "allow": True,
                "reason_code": "allowed",
                "end_action": "poweroff",
            }
        ]
    }
    return fake


@pytest.fixture
def panel(config: Config, db: Database, daemon: FakeDaemon) -> TestClient:
    from biokids_core import security
    from biokids_panel.app import create_app

    with db.session() as s:
        family = repo.get_or_create_family(s)
        family.setup_completed_at = utcnow()
        s.add(Parent(display_name="Anne", pin_hash=security.hash_secret(PIN)))
        kid = Child(unix_user="elif", display_name="Elif", birth_year=2020)
        s.add(kid)
        s.flush()
        s.add(Policy(child_id=kid.id))
    app = create_app(config)
    app.state.ctx.client = daemon  # type: ignore[assignment]
    client = TestClient(app)
    login(client)
    return client


def token(client: TestClient, path: str = "/children/1/time") -> str:
    page = client.get(path).text
    marker = 'name="csrf_token" value="'
    start = page.index(marker) + len(marker)
    return page[start : page.index('"', start)]


def rows(db: Database) -> list[DayOverride]:
    with db.session() as s:
        found = list(s.scalars(select(DayOverride).order_by(DayOverride.id)))
        s.expunge_all()
        return found


def audit_actions(db: Database) -> list[str]:
    with db.session() as s:
        return [row.action for row in s.scalars(select(AuditLog).order_by(AuditLog.id))]


def test_one_tap_on_today_makes_today_special(
    panel: TestClient, db: Database, daemon: FakeDaemon
) -> None:
    response = panel.post(
        "/children/1/special/today",
        data={"choice": "60", "next": "/today", "csrf_token": token(panel)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/today?special=1"
    assert [(r.start_day, r.end_day, r.extra_minutes) for r in rows(db)] == [
        (dt.date.today(), dt.date.today(), 60)
    ]
    assert "special_day_set" in audit_actions(db)
    assert ("day_changed", {"child_id": 1}) in daemon.calls, "the daemon is told, by name"

    page = panel.get("/today?special=1").text
    assert "Bugün özel gün: +1 sa" in page
    assert "Elif için bugün özel gün." in page
    assert "/special/" in page and "Kaldır" in page


def test_today_shows_the_line_with_the_daemon_down(
    panel: TestClient, db: Database, daemon: FakeDaemon
) -> None:
    panel.post(
        "/children/1/special/today",
        data={"choice": "nolimit", "next": "/today", "csrf_token": token(panel)},
    )
    daemon.up = False
    page = panel.get("/today").text
    assert "süre sınırı yok" in page


def test_a_special_day_can_be_taken_back_from_today(
    panel: TestClient, db: Database, daemon: FakeDaemon
) -> None:
    csrf = token(panel)
    panel.post(
        "/children/1/special/today",
        data={"choice": "120", "next": "/today", "csrf_token": csrf},
    )
    row_id = rows(db)[0].id
    response = panel.post(
        f"/children/1/special/{row_id}/cancel",
        data={"next": "/today", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/today?special_removed=1"
    assert rows(db) == []
    assert "special_day_removed" in audit_actions(db)
    assert "Her zamanki kurallar" in panel.get("/today?special_removed=1").text


def test_a_tap_without_the_form_token_changes_nothing(panel: TestClient, db: Database) -> None:
    response = panel.post(
        "/children/1/special/today",
        data={"choice": "60", "next": "/today"},
        follow_redirects=False,
    )
    assert "error=csrf" in response.headers["location"]
    assert rows(db) == []


def test_a_choice_that_is_not_offered_is_refused(panel: TestClient, db: Database) -> None:
    response = panel.post(
        "/children/1/special/today",
        data={"choice": "600", "next": "/today", "csrf_token": token(panel)},
        follow_redirects=False,
    )
    assert "error=" in response.headers["location"]
    assert rows(db) == []


def test_a_tap_the_daemon_never_heard_is_not_reported_as_done(
    panel: TestClient, db: Database, daemon: FakeDaemon
) -> None:
    csrf = token(panel)
    daemon.up = False
    response = panel.post(
        "/children/1/special/today",
        data={"choice": "30", "next": "/today", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/today?error=daemon"


def test_the_time_screen_adds_a_holiday_and_lists_it(
    panel: TestClient, db: Database, daemon: FakeDaemon
) -> None:
    first = dt.date.today() + dt.timedelta(days=3)
    last = first + dt.timedelta(days=11)
    response = panel.post(
        "/children/1/special/range",
        data={
            "start": first.isoformat(),
            "end": last.isoformat(),
            "rule": "weekend",
            "label": "Tatil",
            "csrf_token": token(panel),
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/children/1/time?saved=1"
    saved = rows(db)
    assert [(r.start_day, r.end_day, r.weekend_rules, r.label) for r in saved] == [
        (first, last, True, "Tatil")
    ]
    page = panel.get("/children/1/time").text
    expected = f"Tatil ({first:%d.%m} – {last:%d.%m}): hafta sonu kuralları"
    assert expected in page
    assert ("day_changed", {"child_id": 1}) in daemon.calls


def test_a_holiday_with_its_own_minutes(panel: TestClient, db: Database) -> None:
    day = dt.date.today()
    panel.post(
        "/children/1/special/range",
        data={
            "start": day.isoformat(),
            "end": day.isoformat(),
            "rule": "quota",
            "quota_minutes": "180",
            "csrf_token": token(panel),
        },
    )
    assert [(r.quota_minutes, r.weekend_rules) for r in rows(db)] == [(180, False)]
    assert "Bugün: günde 3 sa" in panel.get("/children/1/time").text


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"start": "", "end": ""}, "special.error.dates"),
        ({"start": "2020-01-01", "end": "2020-01-05"}, "special.error.past"),
        ({"rule": "quota", "quota_minutes": "çok"}, "special.error.minutes"),
    ],
)
def test_a_holiday_that_cannot_be_says_why(
    panel: TestClient, db: Database, data: dict[str, str], error: str
) -> None:
    day = dt.date.today().isoformat()
    form = {"start": day, "end": day, "rule": "weekend", "csrf_token": token(panel)}
    form.update(data)
    response = panel.post("/children/1/special/range", data=form, follow_redirects=False)
    assert f"error={error}" in response.headers["location"]
    assert rows(db) == []
    page = panel.get(response.headers["location"]).text
    assert "special.error" not in page, "the key is translated, never shown"


def test_the_time_screen_offers_today_and_a_range_in_plain_words(panel: TestClient) -> None:
    page = panel.get("/children/1/time").text
    assert 'id="special"' in page
    assert "Bugün özel bir gün mü?" in page
    assert "Hafta sonu gibi" in page
    assert "Şu an özel gün yok." in page
    assert "/children/1/special/today" in page


# ------------------------------------------------------------------ the daemon


@pytest.fixture
def source() -> FakeSessionSource:
    return FakeSessionSource()


@pytest.fixture
def runner(source: FakeSessionSource) -> LoginctlAwareRunner:
    return LoginctlAwareRunner(source)


@pytest.fixture
def supervisor(
    db: Database,
    source: FakeSessionSource,
    runner: LoginctlAwareRunner,
    clock: FakeClock,
    config: Config,
    child: Child,
) -> Supervisor:
    return Supervisor(
        database=db,
        bus=EventBus(queue_size=4096, history_size=4096),
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, runner),
        clock=clock,
        config=config,
    )


async def run_for(supervisor: Supervisor, clock: FakeClock, seconds: int) -> None:
    for _ in range(seconds // TICK):
        clock.advance(TICK)
        await supervisor.tick()


def lock_at_end(session: Session, minutes: int) -> None:
    policy = session.get(Policy, 1)
    assert policy is not None
    policy.weekday_quota_min = minutes
    policy.weekend_quota_min = minutes
    policy.end_action = EndAction.LOCK.value
    session.commit()


async def test_a_special_day_after_the_time_ran_out_lets_the_child_back_in(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: LoginctlAwareRunner,
    session: Session,
) -> None:
    """The parent's tap on "+1 sa" at the lock screen: back in, as "+10 dk" does."""
    lock_at_end(session, 20)
    await supervisor.start()
    logged_in = source.log_in("elif")
    await run_for(supervisor, clock, 25 * 60)
    assert source.sessions[0].locked, "the child was never locked in the first place"
    runner.calls.clear()

    special_days.set_today(session, 1, clock.read().wall.date(), extra_minutes=60)
    session.commit()
    answer = await supervisor.day_changed(1)

    assert answer["remaining_seconds"] > 30 * 60
    assert runner.ran("loginctl", "unlock-session", logged_in.session_id)
    await run_for(supervisor, clock, 60)
    assert supervisor.status()["children"][0]["allow"] is True


async def test_a_special_day_does_not_lift_the_filters_lock(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: LoginctlAwareRunner,
    session: Session,
) -> None:
    """D-172: a child locked because the DNS filter is open stays locked."""
    lock_at_end(session, 20)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 25 * 60)
    supervisor.children[1].locked_for_filter = True
    runner.calls.clear()

    special_days.set_today(session, 1, clock.read().wall.date(), no_limit=True)
    session.commit()
    await supervisor.day_changed(1)

    assert not runner.ran("loginctl", "unlock-session")
    assert supervisor.children[1].locked_for_filter, "and a reload does not forget it"


async def test_a_special_day_on_an_open_screen_unlocks_nothing(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: LoginctlAwareRunner,
    session: Session,
) -> None:
    """Only a lock the end of the day put on is lifted: never a parent's own."""
    lock_at_end(session, 60)
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 60)
    runner.calls.clear()

    special_days.set_today(session, 1, clock.read().wall.date(), extra_minutes=30)
    session.commit()
    await supervisor.day_changed(1)
    assert not runner.ran("loginctl", "unlock-session")


async def test_taking_it_back_ends_a_day_that_is_over_with_the_usual_steps(
    supervisor: Supervisor,
    clock: FakeClock,
    source: FakeSessionSource,
    runner: LoginctlAwareRunner,
    session: Session,
) -> None:
    lock_at_end(session, 20)
    row, _ = special_days.set_today(session, 1, clock.read().wall.date(), extra_minutes=60)
    session.commit()
    await supervisor.start()
    logged_in = source.log_in("elif")
    await run_for(supervisor, clock, 30 * 60)
    assert not runner.ran("loginctl", "lock-session"), "thirty minutes of eighty"

    special_days.cancel(session, 1, row.id, clock.read().wall.date())
    session.commit()
    await supervisor.day_changed(1)
    await run_for(supervisor, clock, 20)
    assert runner.ran("loginctl", "lock-session", logged_in.session_id)
    status = supervisor.status()["children"][0]
    assert status["reason_code"] == Reason.QUOTA_EXHAUSTED


async def test_the_daemon_answers_day_changed_on_its_socket(
    supervisor: Supervisor, session: Session
) -> None:
    from biokids_core.ipc import Op, Request
    from biokids_daemon.server import DaemonServer

    await supervisor.start()
    server = DaemonServer(supervisor, supervisor.bus, supervisor.config)
    response, _ = await server._dispatch(
        Request(op=Op.DAY_CHANGED, id="1", args={"child_id": 1}).encode()
    )
    assert response.ok, response.error
    assert response.data["child_id"] == 1


async def test_a_no_limit_day_draws_its_ring_against_the_window(
    supervisor: Supervisor, clock: FakeClock, source: FakeSessionSource, session: Session
) -> None:
    """Not a red sliver of a whole day's minutes, for a child given the whole day."""
    policy = session.get(Policy, 1)
    assert policy is not None
    policy.windows = {"weekday": ["16:00-20:00"], "weekend": []}
    session.commit()
    special_days.set_today(session, 1, clock.read().wall.date(), no_limit=True)
    session.commit()
    await supervisor.start()
    source.log_in("elif")
    await run_for(supervisor, clock, 60)

    state = supervisor.shell_state(1)
    assert state["quota_seconds"] == 4 * 3600
    assert state["remaining_seconds"] <= state["quota_seconds"]
    today_card = supervisor.status()["children"][0]
    assert today_card["ring_seconds"] == 4 * 3600
