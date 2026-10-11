"""D-206 — what a parent can learn from a week, in plain words.

The rules here decide what a parent is told about their child, so each is
pinned the way a parent would meet it: a Monday morning that must not read as
a hundred percent drop, a child added on Thursday whose Monday was not a zero,
a share that is "about half" and not 47%, and a sentence that states what
happened without a word about what kind of child did it.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from biokids_core import insights
from biokids_core.db import AccessRequest, AiAlert, AuditLog, Child, LedgerDay, UsageEvent
from biokids_core.db.models import CategoryLimit, Parent, Policy, UsageHour, UsageTick
from biokids_core.i18n import parse_po, translator
from biokids_core.insights import (
    Context,
    Duration,
    Facts,
    Period,
    Phrase,
    Span,
    Trend,
    highlights,
    percentages,
    round_half_up,
    say,
    span_for,
    summarize,
)

MONDAY = dt.date(2026, 10, 5)
THURSDAY = dt.date(2026, 10, 8)
SUNDAY = dt.date(2026, 10, 11)
LAST_MONDAY = MONDAY - dt.timedelta(days=7)
CTX = Context(child_id=1)
LOCALE_DIR = Path(__file__).resolve().parent.parent / "src" / "biokids_core" / "locale"


def week(today: dt.date = THURSDAY, **facts: object) -> insights.Insights:
    """This week as it looks on `today`."""
    return summarize(Facts(**facts), Period.WEEK, span_for(Period.WEEK, today), today)  # type: ignore[arg-type]


def days(first: dt.date, *seconds: int) -> dict[dt.date, int]:
    return {first + dt.timedelta(days=n): s for n, s in enumerate(seconds)}


def keys(found: list[insights.Highlight]) -> list[str]:
    return [h.key for h in found]


def utc(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    """Local wall-clock time on `day`, as the naive UTC the tables store."""
    local = dt.datetime.combine(day, dt.time(hour, minute)).astimezone()
    return local.astimezone(dt.UTC).replace(tzinfo=None)


# ------------------------------------------------------------------ periods


def test_this_week_runs_monday_to_sunday_whatever_the_day() -> None:
    for today in (MONDAY, THURSDAY, SUNDAY):
        assert span_for(Period.WEEK, today) == Span(MONDAY, SUNDAY)


def test_last_week_and_the_last_thirty_days() -> None:
    assert span_for(Period.LAST_WEEK, THURSDAY) == Span(
        LAST_MONDAY, MONDAY - dt.timedelta(days=1)
    )
    month = span_for(Period.MONTH, THURSDAY)
    assert month.last == THURSDAY and month.days == 30


def test_an_unknown_period_is_this_week() -> None:
    assert Period.parse("30d") is Period.MONTH
    assert Period.parse("forever") is Period.WEEK
    assert Period.parse(None) is Period.WEEK


def test_a_span_cannot_run_backwards_and_knows_its_past() -> None:
    with pytest.raises(ValueError):
        Span(THURSDAY, MONDAY)
    this_week = Span(MONDAY, SUNDAY)
    assert this_week.until(THURSDAY) == Span(MONDAY, THURSDAY)
    assert this_week.until(MONDAY - dt.timedelta(days=1)) is None
    assert this_week.shift(-7) == Span(LAST_MONDAY, MONDAY - dt.timedelta(days=1))


# ------------------------------------------------------- totals and averages


def test_nothing_recorded_is_nothing_to_say() -> None:
    found = week()
    assert not found.has_data
    assert found.total_s == 0 and found.comparison is None
    assert found.average_s == 0, "three finished days without the computer average nothing"
    assert highlights(found, CTX) == []


def test_today_counts_in_the_total_but_not_in_the_average() -> None:
    """At ten in the morning today is half a day; averaging it in drags every
    number down until the evening."""
    found = week(general=days(MONDAY, 3600, 3600, 3600, 600))
    assert found.total_s == 3 * 3600 + 600
    assert found.average_s == 3600
    assert found.finished_days == 3 and found.counted_days == 4
    assert found.includes_today


def test_days_after_today_are_drawn_empty_and_counted_nowhere() -> None:
    found = week(general=days(MONDAY, 600, 600, 600, 600, 9999, 9999))
    future = [bar for bar in found.days if bar.future]
    assert [bar.day.weekday() for bar in future] == [4, 5, 6]
    assert all(bar.seconds == 0 for bar in future)
    assert found.total_s == 4 * 600


def test_a_child_added_on_wednesday_did_not_spend_monday_away() -> None:
    wednesday = MONDAY + dt.timedelta(days=2)
    found = week(
        THURSDAY + dt.timedelta(days=1),
        general=days(wednesday, 3600, 1800),
        tracked_from=wednesday,
    )
    assert found.average_s == 2700, "Monday and Tuesday are not zeros"
    assert found.counted_days == 3
    assert found.comparison is None, "last week BioKids did not know the child"


def test_weekdays_and_weekends_are_averaged_apart() -> None:
    found = summarize(
        Facts(general=days(LAST_MONDAY, 1800, 1800, 1800, 1800, 1800, 7200, 5400)),
        Period.LAST_WEEK,
        span_for(Period.LAST_WEEK, THURSDAY),
        THURSDAY,
    )
    assert found.weekday_average_s == 1800
    assert found.weekend_average_s == 6300
    assert not found.includes_today


# ------------------------------------------------------------ comparisons


def test_this_week_so_far_is_compared_with_the_same_days_last_week() -> None:
    general = {
        **days(LAST_MONDAY, 4800, 4800, 4800, 9999, 9999, 9999, 9999),
        **days(MONDAY, 3600, 3600, 3600, 7200),
    }
    found = week(general=general)
    assert found.comparison is not None
    assert found.comparison.days == 3
    assert found.comparison.average_s == 3600
    assert found.comparison.previous_average_s == 4800
    assert found.comparison.percent == -25
    assert found.comparison.partial


def test_monday_morning_is_not_a_hundred_percent_drop() -> None:
    found = week(MONDAY, general={**days(LAST_MONDAY, 3600, 3600), MONDAY: 0})
    assert found.comparison is None
    assert found.average_s is None
    assert "insights.h.less" not in keys(highlights(found, CTX))


def test_a_few_minutes_last_week_are_not_something_to_compare_with() -> None:
    general = {**days(LAST_MONDAY, 120, 120, 120), **days(MONDAY, 3600, 3600, 3600)}
    assert week(general=general).comparison is None


def test_a_finished_week_is_compared_with_the_whole_week_before() -> None:
    before = LAST_MONDAY - dt.timedelta(days=7)
    found = summarize(
        Facts(general={**days(before, *[3600] * 7), **days(LAST_MONDAY, *[4500] * 7)}),
        Period.LAST_WEEK,
        span_for(Period.LAST_WEEK, THURSDAY),
        THURSDAY,
    )
    assert found.comparison is not None
    assert found.comparison.days == 7 and not found.comparison.partial
    assert found.comparison.percent == 25


def test_the_change_is_news_only_when_it_is_big_enough() -> None:
    said = {"insights.h.less", "insights.h.more", "insights.h.same"}

    def change(now: int, before: int) -> str:
        general = {**days(LAST_MONDAY, before, before, before), **days(MONDAY, now, now, now)}
        [found] = [k for k in keys(highlights(week(general=general), CTX)) if k in said]
        return found

    assert change(3000, 3600) == "insights.h.less"  # -17%, ten minutes a day
    assert change(4200, 3600) == "insights.h.more"
    assert change(3420, 3600) == "insights.h.same", "five percent is the same"
    assert change(500, 700) == "insights.h.same", "-29%, but three minutes a day"


# ------------------------------------------------------------------ rounding


def test_rounding_is_half_up_both_ways() -> None:
    assert round_half_up(2.5) == 3
    assert round_half_up(-2.5) == -3
    assert round_half_up(2.4999) == 2
    assert round_half_up(0) == 0


def test_percentages_always_add_up_to_a_hundred() -> None:
    assert percentages([1, 1, 1]) == [34, 33, 33]
    assert sum(percentages([7, 13, 29, 51, 3])) == 100
    assert percentages([0, 0]) == [0, 0]
    assert percentages([5]) == [100]


# ------------------------------------------------------------ where it went


def test_time_is_shared_by_kind_with_the_rest_last() -> None:
    apps = {
        (MONDAY, "learning", "gcompris"): 1800,
        (MONDAY, "games", "supertux"): 900,
        (MONDAY, "mystery", "x"): 300,
    }
    found = week(general={MONDAY: 3600}, apps=apps)
    assert [(s.category, s.seconds) for s in found.shares] == [
        ("learning", 1800),
        ("games", 900),
        ("free", 300),
        ("other", 600),
    ]
    assert sum(s.percent for s in found.shares) == 100
    assert found.shares[0].percent == 50


def test_apps_are_ranked_and_compared_with_the_period_before() -> None:
    apps: dict[tuple[dt.date, str, str], int] = {}
    for n in range(3):
        this, last = MONDAY + dt.timedelta(days=n), LAST_MONDAY + dt.timedelta(days=n)
        apps[(this, "learning", "gcompris")] = 1800  # 30 min a day, was 10
        apps[(last, "learning", "gcompris")] = 600
        apps[(this, "games", "supertux")] = 600  # 10, was 30
        apps[(last, "games", "supertux")] = 1800
        apps[(this, "free", "tuxpaint")] = 1200  # the same
        apps[(last, "free", "tuxpaint")] = 1260
        apps[(this, "web", "wiki")] = 300  # never before
    apps[(THURSDAY, "games", "today-only")] = 120
    general = {**days(LAST_MONDAY, 3600, 3600, 3600), **days(MONDAY, 3900, 3900, 3900, 120)}
    found = week(general=general, apps=apps)
    trends = {line.slug: line.trend for line in found.apps}
    assert found.apps[0].slug == "gcompris"
    assert trends == {
        "gcompris": Trend.UP,
        "supertux": Trend.DOWN,
        "tuxpaint": Trend.SAME,
        "wiki": Trend.NEW,
        "today-only": Trend.NONE,
    }


def test_an_app_moved_to_another_kind_shows_the_one_it_spent_longest_in() -> None:
    apps = {(MONDAY, "games", "x"): 600, (MONDAY + dt.timedelta(days=1), "learning", "x"): 1200}
    found = week(general=days(MONDAY, 600, 1200), apps=apps)
    assert found.apps[0].category == "learning"


def test_without_a_comparison_apps_have_no_trend() -> None:
    found = week(general={MONDAY: 3600}, apps={(MONDAY, "games", "x"): 3600})
    assert found.apps[0].trend is Trend.NONE


# ------------------------------------------------- requests, sites and hours


def test_requests_are_counted_by_kind_and_answer() -> None:
    rows = [
        ("time", "approved"),
        ("time", "denied"),
        ("time", "expired"),
        ("time", "pending"),
        ("search", "approved"),
        ("site", "approved"),
    ]
    found = week(requests=rows)
    assert [(line.kind, line.asked) for line in found.requests] == [
        ("time", 4),
        ("site", 1),
        ("search", 1),
    ]
    time = found.requests[0]
    assert (time.approved, time.denied, time.unanswered) == (1, 1, 2)
    assert found.has_data, "a request is something to show even without minutes"


def test_sites_are_ranked_by_the_days_they_were_opened() -> None:
    visits = [
        (MONDAY, "a.org"),
        (MONDAY, "a.org"),
        (MONDAY, "a.org"),
        (MONDAY, "b.org"),
        (MONDAY + dt.timedelta(days=1), "b.org"),
        (LAST_MONDAY, "old.org"),
        *[(MONDAY, f"site{n}.org") for n in range(10)],
    ]
    found = week(visits=visits)
    assert found.sites[0].host == "b.org" and found.sites[0].days == 2
    assert found.sites[1].host == "a.org" and found.sites[1].visits == 3
    assert len(found.sites) == insights.SITES_SHOWN
    assert found.more_sites == 12 - insights.SITES_SHOWN
    assert all(site.host != "old.org" for site in found.sites)


def test_the_busiest_two_hours_and_the_late_evenings() -> None:
    hours = {
        (MONDAY, 16): 1500,
        (MONDAY, 17): 1200,
        (MONDAY, 10): 1800,
        (MONDAY, 21): 600,  # five minutes or more after nine: a late day
        (MONDAY + dt.timedelta(days=1), 22): 120,  # two minutes: not
        (MONDAY + dt.timedelta(days=2), 5): 400,  # before six in the morning
    }
    found = week(hours=hours)
    assert found.hours[16] == 1500 and found.hours_total_s == sum(hours.values())
    assert found.peak == (16, 18)
    assert found.late_days == 2


def test_no_peak_from_a_few_minutes() -> None:
    assert week(hours={(MONDAY, 16): 600}).peak is None


def test_one_busy_hour_is_named_alone() -> None:
    """Five to six, not four to six because four came first."""
    assert week(hours={(MONDAY, 17): 2700, (MONDAY, 18): 300}).peak == (17, 18)
    assert week(hours={(MONDAY, 23): 2700}).peak == (23, 24)
    said = highlights(week(general={MONDAY: 2700}, hours={(MONDAY, 23): 2700}), CTX)
    peak = next(h for h in said if h.key == "insights.h.peak")
    assert peak.params == {"start": "23:00", "end": "00:00"}


def test_only_the_days_shown_count_for_the_time_running_out() -> None:
    found = week(ran_out=frozenset({MONDAY, LAST_MONDAY, THURSDAY + dt.timedelta(days=1)}))
    assert found.ran_out_days == 1
    assert [bar.ran_out for bar in found.days][:2] == [True, False]


# --------------------------------------------------------------- highlights


def test_the_most_important_come_first_and_there_are_five_at_most() -> None:
    general = {
        **days(LAST_MONDAY, 3600, 3600, 3600),
        **days(MONDAY, 5400, 5400, 5400, 600),
    }
    found = week(
        general=general,
        apps={(MONDAY, "games", "supertux"): 16200},
        ran_out=frozenset({MONDAY, MONDAY + dt.timedelta(days=1)}),
        hours={(MONDAY, 22): 900, (MONDAY, 16): 3600},
        requests=[("search", "approved"), ("time", "expired"), ("time", "expired")],
        assistant_alerts=2,
        earned=days(MONDAY, 1200),
    )
    chosen = highlights(found, CTX)
    assert len(chosen) == insights.MAX_HIGHLIGHTS
    assert keys(chosen) == [
        "insights.h.assistant",
        "insights.h.search",
        "insights.h.more",
        "insights.h.ran_out",
        "insights.h.late",
    ]
    assert [h.weight for h in chosen] == sorted((h.weight for h in chosen), reverse=True)


def test_the_time_running_out_often_comes_with_a_way_to_the_rules() -> None:
    twice = highlights(week(general={MONDAY: 3600}, ran_out=frozenset(days(MONDAY, 1, 1))), CTX)
    thrice = highlights(
        week(general={MONDAY: 3600}, ran_out=frozenset(days(MONDAY, 1, 1, 1))), CTX
    )
    once = highlights(week(general={MONDAY: 3600}, ran_out=frozenset({MONDAY})), CTX)
    ran_out = next(h for h in twice if h.key == "insights.h.ran_out")
    assert ran_out.params == {"days": 2} and not ran_out.link
    ran_out = next(h for h in thrice if h.key == "insights.h.ran_out")
    assert ran_out.link == "/children/1/time" and ran_out.suggestion
    assert "insights.h.ran_out" not in keys(once)


def test_a_day_that_never_ran_out_is_said_only_for_a_managed_child() -> None:
    general = days(MONDAY, 1800, 1800, 1800, 1800)
    assert "insights.h.never_ran_out" in keys(highlights(week(general=general), CTX))
    unmanaged = Context(child_id=1, managed=False)
    assert "insights.h.never_ran_out" not in keys(highlights(week(general=general), unmanaged))


def test_late_use_points_at_the_allowed_hours() -> None:
    found = highlights(week(general={MONDAY: 900}, hours={(MONDAY, 22): 900}), CTX)
    late = next(h for h in found if h.key == "insights.h.late")
    assert late.params == {"days": 1}
    assert late.link == "/children/1/time#windows"


def test_learning_time_is_good_news_and_earning_is_offered_once() -> None:
    found = week(general={MONDAY: 3600}, apps={(MONDAY, "learning", "gcompris"): 2400})
    learning = next(h for h in highlights(found, CTX) if h.key == "insights.h.share.learning")
    assert learning.tone is insights.Tone.GOOD
    assert learning.params == {"share": Phrase("insights.share.most")}
    assert learning.link == "/children/1/time?more=1#earn"
    earning = Context(child_id=1, earning_on=True)
    learning = next(
        h for h in highlights(found, earning) if h.key == "insights.h.share.learning"
    )
    assert not learning.link and not learning.suggestion


def test_mostly_games_offers_a_limit_unless_there_is_one() -> None:
    found = week(general={MONDAY: 3600}, apps={(MONDAY, "games", "supertux"): 2000})
    games = next(h for h in highlights(found, CTX) if h.key == "insights.h.share.games")
    assert games.params == {"share": Phrase("insights.share.half")}
    assert games.link == "/children/1/time?more=1#limits"
    limited = Context(child_id=1, games_limited=True)
    games = next(h for h in highlights(found, limited) if h.key == "insights.h.share.games")
    assert not games.link


def test_a_share_is_said_only_from_half_an_hour() -> None:
    found = week(general={MONDAY: 1200}, apps={(MONDAY, "games", "supertux"): 1200})
    assert not [k for k in keys(highlights(found, CTX)) if ".share." in k]


def test_unanswered_requests_suggest_mail_only_when_it_is_off() -> None:
    found = week(requests=[("site", "expired"), ("app", "pending")])
    unanswered = next(h for h in highlights(found, CTX) if h.key == "insights.h.unanswered")
    assert unanswered.link == "/settings#mail"
    mailed = Context(child_id=1, mail_on=True)
    unanswered = next(h for h in highlights(found, mailed) if h.key == "insights.h.unanswered")
    assert not unanswered.link


def test_weekends_and_weekdays_differ() -> None:
    general = days(LAST_MONDAY, 1200, 1200, 1200, 1200, 1200, 5400, 5400)
    found = summarize(
        Facts(general=general), Period.LAST_WEEK, span_for(Period.LAST_WEEK, THURSDAY), THURSDAY
    )
    weekend = next(h for h in highlights(found, CTX) if h.key == "insights.h.weekend")
    assert weekend.params == {"weekend": Duration(5400), "weekday": Duration(1200)}


# --------------------------------------------------------------- in words


def test_a_highlight_is_said_in_turkish_and_in_english() -> None:
    params = {"vs": Phrase("insights.vs.week_so_far"), "percent": 25}
    assert say(translator("tr"), "insights.h.less", params) == (
        "Geçen haftanın aynı günlerine göre günlük ortalama ekran süresi %25 azaldı."
    )
    assert say(translator("en"), "insights.h.less", params) == (
        "Average daily screen time is down 25% compared with the same days last week."
    )
    earned = {"duration": Duration(3900)}
    assert "1 sa 5 dk" in say(translator("tr"), "insights.h.earned", earned)
    assert "1 h 5 min" in say(translator("en"), "insights.h.earned", earned)


def test_every_highlight_can_be_said_in_both_languages() -> None:
    """Every key a rule can produce, rendered: no raw key, no unfilled {slot}."""
    general = {**days(LAST_MONDAY, 3600, 3600, 3600), **days(MONDAY, 5400, 5400, 5400, 60)}
    rich = week(
        general=general,
        apps={(MONDAY, "learning", "a"): 6000, (MONDAY, "games", "b"): 9000},
        ran_out=frozenset(days(MONDAY, 1, 1, 1)),
        hours={(MONDAY, 22): 900, (MONDAY, 16): 3600},
        requests=[("search", "denied"), ("time", "expired"), *[("time", "approved")] * 3],
        assistant_alerts=1,
        earned=days(MONDAY, 1200),
    )
    chosen = highlights(rich, CTX, limit=99)
    assert len(chosen) >= 10
    for locale in ("tr", "en"):
        tr = translator(locale)
        for highlight in chosen:
            said = insights.render(highlight, tr)
            for text in (said.text, said.suggestion, said.link_label):
                assert "insights." not in text and "{" not in text, (locale, highlight.key)


def test_no_sentence_passes_judgement_on_the_child() -> None:
    """Facts, never verdicts: a parent may read these aloud with the child there."""
    verdicts = re.compile(
        r"aşırı|bağımlı|kötü|tembel|yaramaz|ceza|yasak|endişe|tehlike|"
        r"addict|excessive|\bbad\b|lazy|naughty|punish|worr|danger",
        re.IGNORECASE,
    )
    for locale in ("tr", "en"):
        catalogue = parse_po(LOCALE_DIR / locale / "LC_MESSAGES" / "biokids.po")
        for key, text in catalogue.items():
            if key.startswith(("insights.h.", "insights.s.")):
                assert not verdicts.search(text), (locale, key, text)


# ------------------------------------------------------------ the database


def test_facts_are_read_by_the_local_day_they_happened(session: Session, child: Child) -> None:
    """The rows keep UTC; a parent's Monday runs from their own midnight."""
    late_monday, early_tuesday = utc(MONDAY, 23, 50), utc(MONDAY + dt.timedelta(days=1), 0, 20)
    session.add(LedgerDay(child_id=child.id, day=LAST_MONDAY, general_s=1800))
    session.add(LedgerDay(child_id=child.id, day=MONDAY, general_s=3600, earned_s=600))
    session.add(
        UsageTick(child_id=child.id, day=MONDAY, category="games", app_slug="x", seconds=60)
    )
    session.add(UsageHour(child_id=child.id, day=MONDAY, hour=23, seconds=600))
    for when, host in ((late_monday, "www.trt.net.tr"), (early_tuesday, "trt.net.tr")):
        session.add(
            UsageEvent(
                child_id=child.id,
                kind="visit",
                ts_wall=when,
                payload_json=f'{{"host": "{host}"}}',
            )
        )
    for reason in ("quota.exhausted", "window.closed"):
        session.add(
            AuditLog(
                actor="daemon",
                action="quota_exhausted",
                target=f"child:{child.id}",
                ts=late_monday,
                detail_json=f'{{"reason": "{reason}"}}',
            )
        )
    session.add(AccessRequest(child_id=child.id, url="", kind="time", created_at=early_tuesday))
    session.add(AiAlert(child_id=child.id, ts=late_monday, score=90, app="assistant"))
    session.add(AiAlert(child_id=child.id, ts=late_monday, score=90, app="other"))
    session.commit()

    facts = insights.load_facts(session, child.id, Span(MONDAY, SUNDAY), THURSDAY)

    assert facts.general == {LAST_MONDAY: 1800, MONDAY: 3600}, "the week before, for comparing"
    assert facts.earned[MONDAY] == 600
    assert facts.hours == {(MONDAY, 23): 600}
    assert sorted(facts.visits) == [
        (MONDAY, "trt.net.tr"),
        (MONDAY + dt.timedelta(days=1), "trt.net.tr"),
    ]
    assert facts.ran_out == frozenset({MONDAY}), "a closed window is not time running out"
    assert facts.requests == [("time", "pending")]
    assert facts.assistant_alerts == 1
    assert facts.tracked_from == LAST_MONDAY, "the first day on record, not the row's age"


def test_a_period_still_ahead_reads_only_the_ledger(session: Session, child: Child) -> None:
    facts = insights.load_facts(session, child.id, Span(SUNDAY, SUNDAY), THURSDAY)
    assert facts.visits == () and facts.assistant_alerts == 0


def test_the_settings_that_shape_a_suggestion(session: Session, child: Child) -> None:
    assert insights.context_for(session, child) == Context(child_id=child.id)
    policy = session.get(Policy, child.id)
    assert policy is not None
    policy.earn_enabled = True
    session.add(CategoryLimit(child_id=child.id, category="games", minutes=30))
    session.add(Parent(display_name="Anne", smtp_json_encrypted="sealed"))
    session.commit()
    assert insights.context_for(session, child) == Context(
        child_id=child.id, games_limited=True, earning_on=True, mail_on=True
    )
    policy.enabled = False
    session.commit()
    assert not insights.context_for(session, child).managed


def test_the_week_in_words_from_the_database(session: Session, child: Child) -> None:
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    for n in range(7):
        session.add(
            LedgerDay(child_id=child.id, day=monday - dt.timedelta(days=7 - n), general_s=3600)
        )
    for n in range(today.weekday()):
        session.add(
            LedgerDay(child_id=child.id, day=monday + dt.timedelta(days=n), general_s=7200)
        )
    session.commit()
    said = insights.week_highlights(session, child, translator("tr"), today, 3)
    if today.weekday():
        assert any("%100 arttı" in s.text for s in said), said
    assert len(said) <= 3
