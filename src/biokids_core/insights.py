"""What a parent can learn from a child's week, in plain words (D-206).

The owner asked for analysis in the parent panel. A parent who is not technical
(principle 6) does not want a dashboard; they want to know whether anything
changed, where the time went, and whether there is something to do about it.
So for one child and one period - this week, last week or the last thirty days -
this module works out:

* the numbers: screen time in total and per day, the change against the period
  before, the days the day's time ran out, the kinds of app and the apps, the
  minutes earned and granted, the requests, the assistant's alerts, the sites
  the browser reported and the hours of the day;
* at most five **highlights**, each one sentence a parent understands at a
  glance, the most important first, some with a gentle suggestion and the
  panel setting that acts on it.

Everything is computed when it is read, from the tables retention already
prunes (D-189): `ledger_day`, `usage_tick`, `usage_hour`, the `visit` events,
`access_request`, `ai_alert` and the `quota_exhausted` audit rows. Nothing is
stored, and nothing leaves the machine but the week in the parent's own Sunday
mail (D-177). There is no model: every highlight is a
rule written down below, so the same week always reads the same way.

Three rules shape the arithmetic:

* **Today is not over.** Totals include it; averages and comparisons do not, or
  every Monday morning would report a hundred percent drop. A comparison sets
  the finished days of the period against the same days one period earlier -
  this week's Monday to Wednesday against last week's.
* **Days before BioKids are not zeros.** A child added on Thursday did not
  spend Monday to Wednesday away from the computer; those days are left out of
  every average (`Facts.tracked_from`).
* **Small numbers are not news.** A comparison needs ten minutes a day on the
  other side, over at least three days the child was known (or as many as have
  finished on this side); a change is reported from ten percent and five
  minutes a day, and beyond tripling in words rather than as a number; a share
  is said from about a third of the time.

And one for the words: a highlight says what happened, never what it says
about the child. "Ekran süresinin çoğu oyunlarda geçti", never "çok oyun
oynadı". The parent may read it out; the child may be standing beside them.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biokids_core import mail
from biokids_core.config import Config
from biokids_core.db.models import (
    AccessRequest,
    AiAlert,
    AuditLog,
    CategoryLimit,
    Child,
    LedgerDay,
    Policy,
    UsageEvent,
    UsageHour,
    UsageTick,
    utcnow,
)
from biokids_core.i18n import Translator, duration_text
from biokids_core.repo import REQUEST_EXPIRY
from biokids_core.types import Category, Reason

MONTH_DAYS: Final = 30
"""The "last 30 days" period. Also how long visited sites are kept (D-189)."""

USED_SECONDS: Final = 60
"""A day with less than a minute of screen time was not a day the computer was used."""

COMPARE_MIN_SECONDS: Final = 10 * 60
"""A comparison needs this much screen time *a day*, on average, on the days it
compares against. Not in total: a child added last Friday and tried for six
minutes made any week after it "up 900%"."""

COMPARE_MIN_DAYS: Final = 3
"""...and this many of those days that BioKids knew the child, or as many as have
finished on this side if fewer: one evening of setting up is not a week."""

CHANGE_MIN_PERCENT: Final = 10
CHANGE_MIN_SECONDS: Final = 5 * 60
"""A change in the daily average is news from 10% *and* five minutes a day."""

CHANGE_WORDS_PERCENT: Final = 200
"""Beyond this the change is said in words, "more than double", and not as a
number: "up 900%" is a figure nobody pictures, and it says more about a quiet
week before than about this one."""

TREND_MIN_PERCENT: Final = 25
TREND_MIN_SECONDS: Final = 3 * 60
"""An app is "more" or "less" from 25% and three minutes a day."""

LATE_FROM: Final = 21
LATE_UNTIL: Final = 6
LATE_HOURS: Final = frozenset((*range(LATE_FROM, 24), *range(LATE_UNTIL)))
LATE_MIN_SECONDS: Final = 5 * 60
"""A night counts as late use with five minutes between 21:00 and 06:00. Children
of four to ten are mostly asleep by then; the sentence names the hours. The
hours after midnight belong to the evening before: 22:00 to 00:30 is one late
night, not two late days."""

PEAK_MIN_SECONDS: Final = 30 * 60
"""The busiest hours are worth saying from half an hour of use."""
PEAK_SECOND_HOUR: Final = 0.25
"""A second hour is named beside the busiest when it adds a quarter more."""

WEEKEND_RATIO: Final = 1.5
WEEKEND_MIN_SECONDS: Final = 30 * 60
WEEKEND_MIN_DAYS: Final = 2
"""Finished days on each side before weekdays and weekends are set side by side.
On a Sunday only Saturday is over, and on the VM a Saturday the machine was off
made "the weekend" an average of 0 minutes over that one day."""

SHARE_MIN_SECONDS: Final = 30 * 60
"""Shares of the time are said only once there is half an hour to share."""

EARNED_MIN_SECONDS: Final = 10 * 60
TIME_REQUESTS_MIN: Final = 3
UNANSWERED_MIN: Final = 2
RAN_OUT_MIN_DAYS: Final = 2
RAN_OUT_SUGGEST_DAYS: Final = 3

APPS_SHOWN: Final = 6
SITES_SHOWN: Final = 8
"""UX.md section 3.3: no list of sites longer than ten without a search box."""

MAX_HIGHLIGHTS: Final = 5

OTHER: Final = "other"
"""Screen time no app accounts for: the launcher's home screen, mostly."""

REQUEST_KINDS: Final = ("time", "site", "app", "search")

_CATEGORIES: Final = frozenset(category.value for category in Category)


# --------------------------------------------------------------- periods


class Period(StrEnum):
    """What the parent picks on the screen. This week unless they pick another."""

    WEEK = "week"
    LAST_WEEK = "last_week"
    MONTH = "30d"

    @classmethod
    def parse(cls, raw: str | None) -> Period:
        """A period from a query string; anything unknown is this week."""
        try:
            return cls(raw or cls.WEEK.value)
        except ValueError:
            return cls.WEEK


@dataclass(frozen=True, slots=True)
class Span:
    """A run of calendar days, both ends included, in the family's local time."""

    first: dt.date
    last: dt.date

    def __post_init__(self) -> None:
        if self.last < self.first:
            raise ValueError(f"a span cannot end ({self.last}) before it starts ({self.first})")

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1

    def dates(self) -> list[dt.date]:
        return [self.first + dt.timedelta(days=n) for n in range(self.days)]

    def shift(self, days: int) -> Span:
        step = dt.timedelta(days=days)
        return Span(self.first + step, self.last + step)

    def until(self, day: dt.date) -> Span | None:
        """The part of the span up to and including `day`; None if none is."""
        if day < self.first:
            return None
        return Span(self.first, min(self.last, day))


def span_for(period: Period, today: dt.date) -> Span:
    """Monday to Sunday for the weeks; the thirty days ending today for the month."""
    monday = today - dt.timedelta(days=today.weekday())
    if period is Period.LAST_WEEK:
        return Span(monday - dt.timedelta(days=7), monday - dt.timedelta(days=1))
    if period is Period.MONTH:
        return Span(today - dt.timedelta(days=MONTH_DAYS - 1), today)
    return Span(monday, monday + dt.timedelta(days=6))


# ----------------------------------------------------------------- facts


@dataclass(frozen=True)
class Facts:
    """What the database holds about one child, before any arithmetic.

    Plain values, so that `summarize` - where every rule lives - is tested
    without a database. Every day is a local calendar day: the rows stored in
    UTC have been converted already. The ledger and the apps cover the span
    and the one before it; the hours the days shown and the morning after the
    last of them; the rest only the days shown.
    """

    general: Mapping[dt.date, int] = field(default_factory=dict)
    earned: Mapping[dt.date, int] = field(default_factory=dict)
    granted: Mapping[dt.date, int] = field(default_factory=dict)
    apps: Mapping[tuple[dt.date, str, str], int] = field(default_factory=dict)
    """(day, category, app slug) -> seconds, as in `usage_tick`."""
    hours: Mapping[tuple[dt.date, int], int] = field(default_factory=dict)
    """(day, hour 0-23) -> seconds, as in `usage_hour`."""
    ran_out: frozenset[dt.date] = frozenset()
    """The days the day's time ran out, from the daemon's `quota_exhausted` entries."""
    visits: Sequence[tuple[dt.date, str]] = ()
    """(day, host), one per visit the extension reported (D-187)."""
    requests: Sequence[tuple[str, str]] = ()
    """(kind, status) of each request the child filed. One still `pending` after
    `REQUEST_EXPIRY` is `expired` here, as the daemon will mark it."""
    assistant_alerts: int = 0
    tracked_from: dt.date | None = None
    """The first day BioKids knew this child. Earlier days are not zeros."""


def utc_bounds(span: Span, tz: dt.tzinfo | None = None) -> tuple[dt.datetime, dt.datetime]:
    """[start, end) of a run of local days, as the naive UTC the rows store.

    The same conversion as the audit screen's (`_utc_bounds_of_local_day`): a
    row at 01:30 in Istanbul is 22:30 UTC the evening before, and belongs to
    the day the parent lived it. `tz` is the machine's zone unless given; the
    tests give one, so that they test the conversion on a machine set to UTC.
    """
    start = dt.datetime.combine(span.first, dt.time.min, tzinfo=tz).astimezone(dt.UTC)
    end = dt.datetime.combine(span.last + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
    return start.replace(tzinfo=None), end.astimezone(dt.UTC).replace(tzinfo=None)


def local_day(utc_naive: dt.datetime, tz: dt.tzinfo | None = None) -> dt.date:
    """The local calendar day of a naive UTC timestamp, in `tz` or the machine's zone."""
    return utc_naive.replace(tzinfo=dt.UTC).astimezone(tz).date()


def load_facts(
    session: Session,
    child_id: int,
    span: Span,
    today: dt.date,
    *,
    now: dt.datetime | None = None,
    tz: dt.tzinfo | None = None,
) -> Facts:
    """Read what `summarize` needs, with one indexed query per table.

    Every query is bounded by the child and by the days, which every table here
    has an index for; a month of a busy child is a few hundred rows. `now` (naive
    UTC) is what tells a request still waiting from one nobody answered.
    """
    expired_before = (now or utcnow()) - REQUEST_EXPIRY
    previous = span.shift(-span.days)
    general: dict[dt.date, int] = {}
    earned: dict[dt.date, int] = {}
    granted: dict[dt.date, int] = {}
    for row in session.scalars(
        select(LedgerDay).where(
            LedgerDay.child_id == child_id,
            LedgerDay.day >= previous.first,
            LedgerDay.day <= span.last,
        )
    ):
        general[row.day] = row.general_s
        earned[row.day] = row.earned_s
        granted[row.day] = row.granted_s
    apps = {
        (row.day, row.category, row.app_slug): row.seconds
        for row in session.scalars(
            select(UsageTick).where(
                UsageTick.child_id == child_id,
                UsageTick.day >= previous.first,
                UsageTick.day <= span.last,
            )
        )
    }
    tracked_from = _tracked_from(session, child_id, tz)
    shown = span.until(today)
    if shown is None:
        return Facts(general, earned, granted, apps, tracked_from=tracked_from)

    # The morning after the last day too: the small hours belong to the night
    # that began on the evening before (LATE_HOURS), and a night is counted in
    # the period its evening fell in.
    hours = {
        (row.day, row.hour): row.seconds
        for row in session.scalars(
            select(UsageHour).where(
                UsageHour.child_id == child_id,
                UsageHour.day >= shown.first,
                UsageHour.day <= shown.last + dt.timedelta(days=1),
            )
        )
    }
    since, until = utc_bounds(shown, tz)
    visits = [
        (local_day(event.ts_wall, tz), host)
        for event in session.scalars(
            select(UsageEvent).where(
                UsageEvent.child_id == child_id,
                UsageEvent.kind == "visit",
                UsageEvent.ts_wall >= since,
                UsageEvent.ts_wall < until,
            )
        )
        if (host := str(event.payload.get("host", "")).strip().lower().removeprefix("www."))
    ]
    ran_out = frozenset(
        local_day(entry.ts, tz)
        for entry in session.scalars(
            select(AuditLog).where(
                AuditLog.action == "quota_exhausted",
                AuditLog.target == f"child:{child_id}",
                AuditLog.ts >= since,
                AuditLog.ts < until,
            )
        )
        # The end of the day also comes when the last time window closes;
        # that is the clock, not the time running out.
        if entry.detail.get("reason", Reason.QUOTA_EXHAUSTED) == Reason.QUOTA_EXHAUSTED
    )
    requests = [
        (
            row.kind,
            "expired"
            if row.status == "pending" and row.created_at < expired_before
            else row.status,
        )
        for row in session.scalars(
            select(AccessRequest).where(
                AccessRequest.child_id == child_id,
                AccessRequest.created_at >= since,
                AccessRequest.created_at < until,
            )
        )
    ]
    alerts = session.scalar(
        select(func.count())
        .select_from(AiAlert)
        .where(
            AiAlert.child_id == child_id,
            AiAlert.app == "assistant",
            AiAlert.ts >= since,
            AiAlert.ts < until,
        )
    )
    return Facts(
        general=general,
        earned=earned,
        granted=granted,
        apps=apps,
        hours=hours,
        ran_out=ran_out,
        visits=visits,
        requests=requests,
        assistant_alerts=int(alerts or 0),
        tracked_from=tracked_from,
    )


def _tracked_from(session: Session, child_id: int, tz: dt.tzinfo | None) -> dt.date | None:
    """The day the child was added, or the first day on record if earlier."""
    child = session.get(Child, child_id)
    first_day = session.scalar(
        select(func.min(LedgerDay.day)).where(LedgerDay.child_id == child_id)
    )
    known = [day for day in (first_day,) if day is not None]
    if child is not None:
        known.append(local_day(child.created_at, tz))
    return min(known) if known else None


# ---------------------------------------------------------------- numbers


class Trend(StrEnum):
    """How an app compares with the period before, in the words a parent reads."""

    NEW = "new"
    UP = "up"
    DOWN = "down"
    SAME = "same"
    NONE = ""
    """Nothing to compare with."""


@dataclass(frozen=True, slots=True)
class DayBar:
    day: dt.date
    seconds: int
    ran_out: bool
    future: bool
    """After today: drawn empty, counted nowhere."""


@dataclass(frozen=True, slots=True)
class Share:
    category: str
    """A `Category` value, or OTHER."""
    seconds: int
    percent: int
    """Whole percent of the period's screen time; the shares add up to 100."""


@dataclass(frozen=True, slots=True)
class AppLine:
    slug: str
    category: str
    seconds: int
    trend: Trend


@dataclass(frozen=True, slots=True)
class RequestLine:
    kind: str
    asked: int
    approved: int
    denied: int
    unanswered: int
    """Expired: nobody said yes or no while it could still be answered."""
    waiting: int = 0
    """Still open, and still answerable: not "unanswered", only not yet."""


@dataclass(frozen=True, slots=True)
class SiteLine:
    host: str
    days: int
    """On how many days it was opened, which says more than how often."""
    visits: int


class Change(StrEnum):
    """What a comparison says, before it is put into words."""

    SAME = "same"
    LESS = "less"
    MORE = "more"
    MUCH_MORE = "much_more"
    """Up by more than CHANGE_WORDS_PERCENT: "more than double", not a number."""


@dataclass(frozen=True, slots=True)
class Comparison:
    """The finished days of the period against the same days one period back."""

    days: int
    """Finished days compared on this side."""
    average_s: int
    previous_average_s: int
    percent: int
    """Signed and rounded: -18 is "down 18%"."""
    partial: bool
    """The period is not over, so it is "the same days last week"."""

    @property
    def change(self) -> Change:
        """One rule for the highlight and the stat tile, so they never disagree."""
        difference = abs(self.average_s - self.previous_average_s)
        if abs(self.percent) < CHANGE_MIN_PERCENT or difference < CHANGE_MIN_SECONDS:
            return Change.SAME
        if self.percent < 0:
            return Change.LESS
        return Change.MUCH_MORE if self.percent > CHANGE_WORDS_PERCENT else Change.MORE


@dataclass(frozen=True)
class Insights:
    """Everything the insights screen shows for one child and one period."""

    period: Period
    span: Span
    today: dt.date
    days: tuple[DayBar, ...]
    total_s: int
    used_days: int
    counted_days: int
    """Days so far that BioKids knew the child, today included."""
    finished_days: int
    """The same, today excluded: what the averages are over."""
    average_s: int | None
    weekday_average_s: int | None
    weekend_average_s: int | None
    comparison: Comparison | None
    ran_out_days: int
    shares: tuple[Share, ...]
    apps: tuple[AppLine, ...]
    earned_s: int
    granted_s: int
    requests: tuple[RequestLine, ...]
    assistant_alerts: int
    sites: tuple[SiteLine, ...]
    more_sites: int
    hours: tuple[int, ...]
    """Seconds in each hour of the day, 0 to 23, over the days shown."""
    hours_total_s: int
    peak: tuple[int, int] | None
    """The busiest hour or two, (start, end), end exclusive and possibly 24."""
    late_nights: int
    """Evenings in the period with use between 21:00 and 06:00, the small hours
    counted with the evening before."""

    @property
    def includes_today(self) -> bool:
        return self.span.first <= self.today <= self.span.last

    @property
    def has_data(self) -> bool:
        """Anything at all to show. Without it the screen says so, and nothing else."""
        return (
            self.total_s >= USED_SECONDS
            or bool(self.requests)
            or self.assistant_alerts > 0
            or bool(self.sites)
        )

    @property
    def requests_total(self) -> int:
        return sum(line.asked for line in self.requests)


def summarize(facts: Facts, period: Period, span: Span, today: dt.date) -> Insights:
    """Turn the facts into the numbers. Pure: no database, no clock, no locale."""

    def tracked(day: dt.date) -> bool:
        return facts.tracked_from is None or day >= facts.tracked_from

    shown = [day for day in span.dates() if day <= today]
    shown_set = set(shown)
    finished = [day for day in shown if day < today and tracked(day)]

    def seconds_on(day: dt.date) -> int:
        return max(0, facts.general.get(day, 0))

    total = sum(seconds_on(day) for day in shown)
    ran_out_shown = facts.ran_out & shown_set
    days = tuple(
        DayBar(
            day=day,
            seconds=seconds_on(day) if day <= today else 0,
            ran_out=day in ran_out_shown,
            future=day > today,
        )
        for day in span.dates()
    )

    weekdays = [day for day in finished if day.weekday() < 5]
    weekend = [day for day in finished if day.weekday() >= 5]
    comparison, previous_days = _compare(facts, span, today, finished, tracked)
    sites, more_sites = _sites(facts.visits, shown_set)
    hours, peak, late_nights = _hours(facts.hours, shown_set)

    return Insights(
        period=period,
        span=span,
        today=today,
        days=days,
        total_s=total,
        used_days=sum(1 for day in shown if seconds_on(day) >= USED_SECONDS),
        counted_days=sum(1 for day in shown if tracked(day)),
        finished_days=len(finished),
        average_s=_average([seconds_on(day) for day in finished]),
        weekday_average_s=_average([seconds_on(day) for day in weekdays], WEEKEND_MIN_DAYS),
        weekend_average_s=_average([seconds_on(day) for day in weekend], WEEKEND_MIN_DAYS),
        comparison=comparison,
        ran_out_days=len(ran_out_shown),
        shares=_shares(facts, shown_set, total),
        apps=_apps(
            facts,
            shown_set,
            finished,
            previous_days if comparison else None,
            set(span.shift(-span.days).dates()),
        ),
        earned_s=sum(max(0, facts.earned.get(day, 0)) for day in shown),
        granted_s=sum(max(0, facts.granted.get(day, 0)) for day in shown),
        requests=_requests(facts.requests),
        assistant_alerts=facts.assistant_alerts,
        sites=sites,
        more_sites=more_sites,
        hours=hours,
        hours_total_s=sum(hours),
        peak=peak,
        late_nights=late_nights,
    )


def _compare(
    facts: Facts,
    span: Span,
    today: dt.date,
    finished: list[dt.date],
    tracked: Callable[[dt.date], bool],
) -> tuple[Comparison | None, list[dt.date]]:
    """This period's finished days against the same days one period back.

    Also returns those earlier days, which the apps' trends compare against.
    Nothing to compare with, unless the earlier side has enough days the child
    was known and enough time on each of them (COMPARE_MIN_DAYS,
    COMPARE_MIN_SECONDS): a few minutes of trying BioKids out is not a week to
    measure the next one by.
    """
    previous = [day - dt.timedelta(days=span.days) for day in finished]
    previous = [day for day in previous if tracked(day)]
    if not finished or len(previous) < min(COMPARE_MIN_DAYS, len(finished)):
        return None, previous
    before_average = sum(max(0, facts.general.get(day, 0)) for day in previous) / len(previous)
    if before_average < COMPARE_MIN_SECONDS:
        return None, previous
    now_average = sum(max(0, facts.general.get(day, 0)) for day in finished) / len(finished)
    return (
        Comparison(
            days=len(finished),
            average_s=round_half_up(now_average),
            previous_average_s=round_half_up(before_average),
            percent=round_half_up(100 * (now_average - before_average) / before_average),
            partial=span.last >= today,
        ),
        previous,
    )


def _average(values: list[int], at_least: int = 1) -> int | None:
    return round_half_up(sum(values) / len(values)) if len(values) >= max(1, at_least) else None


def _shares(facts: Facts, days: set[dt.date], total: int) -> tuple[Share, ...]:
    """The kinds of app, most time first, then what no app accounts for."""
    per_kind: dict[str, int] = {}
    for (day, category, _slug), seconds in facts.apps.items():
        if day in days and seconds > 0:
            kind = category if category in _CATEGORIES else Category.FREE.value
            per_kind[kind] = per_kind.get(kind, 0) + seconds
    counted = sum(per_kind.values())
    whole = max(total, counted)
    if whole <= 0:
        return ()
    kinds = sorted(per_kind.items(), key=lambda item: (-item[1], item[0]))
    if whole - counted >= USED_SECONDS:
        kinds.append((OTHER, whole - counted))
    percents = percentages([seconds for _kind, seconds in kinds])
    return tuple(
        Share(category=kind, seconds=seconds, percent=percent)
        for (kind, seconds), percent in zip(kinds, percents, strict=True)
    )


def _apps(
    facts: Facts,
    days: set[dt.date],
    finished: list[dt.date],
    previous: list[dt.date] | None,
    previous_span: set[dt.date],
) -> tuple[AppLine, ...]:
    """The most used apps, each with how it compares with the period before.

    "More" and "less" compare the same days as the screen time does; "new" is
    decided against the whole period before. A game played only at the weekend
    is not new every Thursday because last Monday to Wednesday went without it.
    """
    now_days, before_days = set(finished), set(previous or ())
    seconds: dict[str, int] = {}
    per_kind: dict[tuple[str, str], int] = {}
    now: dict[str, int] = {}
    before: dict[str, int] = {}
    earlier: dict[str, int] = {}
    for (day, category, slug), value in facts.apps.items():
        if day in days:
            seconds[slug] = seconds.get(slug, 0) + value
            per_kind[(slug, category)] = per_kind.get((slug, category), 0) + value
        if day in now_days:
            now[slug] = now.get(slug, 0) + value
        if day in before_days:
            before[slug] = before.get(slug, 0) + value
        if day in previous_span:
            earlier[slug] = earlier.get(slug, 0) + value
    ranked = sorted(
        (item for item in seconds.items() if item[1] >= USED_SECONDS),
        key=lambda item: (-item[1], item[0]),
    )[:APPS_SHOWN]
    # An app a parent moved to another kind mid-period is shown under the kind
    # it spent longest in.
    kind = {
        slug: category
        for (slug, category), _value in sorted(per_kind.items(), key=lambda item: item[1])
    }
    return tuple(
        AppLine(
            slug=slug,
            category=kind[slug],
            seconds=total,
            trend=_trend(
                now.get(slug, 0),
                len(now_days),
                before.get(slug, 0),
                len(before_days),
                used_before=earlier.get(slug, 0) >= USED_SECONDS,
            )
            if previous
            else Trend.NONE,
        )
        for slug, total in ranked
    )


def _trend(
    now_s: int, now_days: int, before_s: int, before_days: int, *, used_before: bool
) -> Trend:
    """`used_before`: the app had a minute or more anywhere in the period before."""
    if not now_days or not before_days or (now_s <= 0 and before_s <= 0):
        return Trend.NONE
    if before_s <= 0:
        # Not on the days compared. New only if not on the others either;
        # otherwise there is nothing on the same days to set it against.
        if used_before:
            return Trend.NONE
        return Trend.NEW if now_s >= USED_SECONDS else Trend.NONE
    per_day_now, per_day_before = now_s / now_days, before_s / before_days
    change = per_day_now - per_day_before
    if (
        abs(change) < TREND_MIN_SECONDS
        or 100 * abs(change) / per_day_before < TREND_MIN_PERCENT
    ):
        return Trend.SAME
    return Trend.UP if change > 0 else Trend.DOWN


def _requests(rows: Sequence[tuple[str, str]]) -> tuple[RequestLine, ...]:
    """One line per kind the child asked for, in a fixed order.

    A request still waiting is not "unanswered": the parent may be reading the
    "2 istek bekliyor" banner on the same screen. Only one that expired with
    nobody saying yes or no was left unanswered.
    """
    lines = []
    for wanted in (*REQUEST_KINDS, *sorted({k for k, _ in rows} - set(REQUEST_KINDS))):
        statuses = [status for kind, status in rows if kind == wanted]
        if statuses:
            lines.append(
                RequestLine(
                    kind=wanted,
                    asked=len(statuses),
                    approved=statuses.count("approved"),
                    denied=statuses.count("denied"),
                    unanswered=statuses.count("expired"),
                    waiting=statuses.count("pending"),
                )
            )
    return tuple(lines)


def _sites(
    visits: Sequence[tuple[dt.date, str]], days: set[dt.date]
) -> tuple[tuple[SiteLine, ...], int]:
    """The sites opened on the most days, and how many more there were."""
    seen: dict[str, set[dt.date]] = {}
    counts: dict[str, int] = {}
    for day, host in visits:
        if day in days:
            seen.setdefault(host, set()).add(day)
            counts[host] = counts.get(host, 0) + 1
    ranked = sorted(seen, key=lambda host: (-len(seen[host]), -counts[host], host))
    shown = tuple(
        SiteLine(host=host, days=len(seen[host]), visits=counts[host])
        for host in ranked[:SITES_SHOWN]
    )
    return shown, max(0, len(ranked) - SITES_SHOWN)


def _hours(
    hours: Mapping[tuple[dt.date, int], int], days: set[dt.date]
) -> tuple[tuple[int, ...], tuple[int, int] | None, int]:
    """Seconds per hour of the day, the busiest hours, and the late nights.

    The busiest hours are the busiest two in a row, unless the second adds
    less than a quarter to the first: a child who uses the computer from five
    to six is not shown "between four and six" because four came first.

    A night is named by its evening: 01:00 on Tuesday is Monday's night. So a
    night belongs to the period its evening is in - the small hours of the
    first day shown are the night before, which is not counted, and those of
    the morning after the last are, when `hours` has them.
    """
    per_hour = [0] * 24
    late: dict[dt.date, int] = {}
    for (day, hour), seconds in hours.items():
        if not 0 <= hour <= 23 or seconds <= 0:
            continue
        if day in days:
            per_hour[hour] += seconds
        if hour in LATE_HOURS:
            night = day - dt.timedelta(days=1) if hour < LATE_UNTIL else day
            if night in days:
                late[night] = late.get(night, 0) + seconds
    peak: tuple[int, int] | None = None
    if sum(per_hour) >= PEAK_MIN_SECONDS:
        busiest = max(range(24), key=lambda h: (per_hour[h], -h))
        start = max(range(23), key=lambda h: (per_hour[h] + per_hour[h + 1], -h))
        pair = per_hour[start] + per_hour[start + 1]
        alone = pair - per_hour[busiest] < PEAK_SECOND_HOUR * per_hour[busiest]
        peak = (busiest, busiest + 1) if alone else (start, start + 2)
    late_nights = sum(1 for seconds in late.values() if seconds >= LATE_MIN_SECONDS)
    return tuple(per_hour), peak, late_nights


def round_half_up(value: float) -> int:
    """2.5 -> 3 and -2.5 -> -3. Python's round() would say 2 and -2."""
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def percentages(values: Sequence[int]) -> list[int]:
    """Whole percentages of `values` that add up to exactly 100.

    Largest remainder: three equal thirds are 34, 33 and 33, not 33 three times,
    which a parent adding up a legend would notice.
    """
    total = sum(values)
    if total <= 0:
        return [0 for _ in values]
    exact = [100 * value / total for value in values]
    whole = [math.floor(share) for share in exact]
    order = sorted(range(len(values)), key=lambda i: (whole[i] - exact[i], i))
    for i in order[: 100 - sum(whole)]:
        whole[i] += 1
    return whole


# ------------------------------------------------------------- highlights


class Tone(StrEnum):
    GOOD = "good"
    INFO = "info"
    ATTENTION = "attention"
    """Something a parent may want to look at; never an alarm."""


@dataclass(frozen=True, slots=True)
class Duration:
    """A number of seconds, said as hours and minutes in the reader's language."""

    seconds: int


@dataclass(frozen=True, slots=True)
class Phrase:
    """A catalogue key, translated and put into the sentence."""

    key: str


Param = str | int | Duration | Phrase


@dataclass(frozen=True, slots=True)
class Highlight:
    """One sentence about the period, as keys and values, not words."""

    key: str
    weight: int
    """Importance: the screen shows the heaviest first, and at most five."""
    icon: str
    tone: Tone
    params: Mapping[str, Param] = field(default_factory=dict)
    suggestion: str = ""
    """A catalogue key: what the parent might do, said gently. Optional."""
    link: str = ""
    """A path in the panel where the suggestion can be acted on."""
    link_label: str = ""


@dataclass(frozen=True, slots=True)
class Context:
    """What the highlights need to know beyond the numbers: the child's settings."""

    child_id: int
    managed: bool = True
    """BioKids manages the child: there is a day's time that can run out."""
    games_limited: bool = False
    earning_on: bool = False
    windows_set: bool = False
    """Allowed hours are set, for every day or for some: the parent has already
    chosen when the computer may be used, so late use is not a cue to suggest it."""
    mail_alerts: bool = False
    """The parent gets the alert mail (D-177), which tells them of every request
    at once. A family with only the Sunday summary does not."""
    mail_weekly: bool = False
    """The Sunday mail goes out, and with it the week's highlights."""


def context_for(session: Session, child: Child, config: Config) -> Context:
    """The settings the suggestions depend on, read in a few small queries.

    The mail settings are decrypted to read two switches; nothing else of them
    (the server, the password) leaves this function.
    """
    policy = session.get(Policy, child.id)
    games = session.get(CategoryLimit, {"child_id": child.id, "category": Category.GAMES.value})
    settings = mail.load_settings(session, config)
    alerts = weekly = False
    if settings is not None and settings.usable:
        alerts, weekly = settings.alerts, settings.weekly
    return Context(
        child_id=child.id,
        managed=bool(child.enabled and policy is not None and policy.enabled),
        games_limited=games is not None,
        earning_on=bool(policy is not None and policy.earn_enabled),
        windows_set=policy is not None and _windows_set(policy),
        mail_alerts=alerts,
        mail_weekly=weekly,
    )


def _windows_set(policy: Policy) -> bool:
    """Any allowed hours: the weekday or weekend ones, or a day's own (D-176)."""
    if any(policy.windows.values()):
        return True
    return any(day.get("windows") for day in policy.schedule.values())


SHARE_WORDS: Final = (
    (0.85, "insights.share.all"),
    (0.58, "insights.share.most"),
    (0.42, "insights.share.half"),
    (0.29, "insights.share.third"),
    (0.2, "insights.share.quarter"),
)
"""A share in words, nearest first: "yaklaşık yarısı" reads faster than 47%, and
needs no Turkish suffix after a number."""


def share_phrase(fraction: float) -> str | None:
    for floor, key in SHARE_WORDS:
        if fraction >= floor:
            return key
    return None


def comparison_phrase(period: Period, partial: bool) -> str:
    """ "Geçen haftanın aynı günlerine göre", or whatever this period compares with."""
    if period is Period.WEEK:
        return "insights.vs.week_so_far" if partial else "insights.vs.week"
    if period is Period.LAST_WEEK:
        return "insights.vs.last_week"
    return "insights.vs.30d"


def highlights(ins: Insights, ctx: Context, limit: int = MAX_HIGHLIGHTS) -> list[Highlight]:
    """The rules, heaviest first. Each is a fact, and none is a verdict.

    The weights, so the order is written down somewhere:

    100 the assistant was asked something the family should know about
     90 a search the browser refused, and the child asked about it
     80 screen time changed against the period before
     70 the day's time ran out on several days
     65 used between 21:00 and 06:00, counted in nights
     60 requests that expired with nobody answering (not those still waiting)
     55 about a third or more of the time in learning apps
     50 half or more in games / 45 on the web / 35 in free apps
     45 minutes earned by learning
     40 weekends and weekdays differ a lot
     35 asked for more time three times or more
     30 screen time about the same as before
     20 the busiest hour or two
     15 the time never ran out
    """
    if not ins.has_data:
        return []
    child = ctx.child_id
    time_page = f"/children/{child}/time"
    found: list[Highlight] = []

    if ins.assistant_alerts:
        found.append(
            Highlight(
                "insights.h.assistant",
                100,
                "💬",
                Tone.ATTENTION,
                {"count": ins.assistant_alerts},
                suggestion="insights.s.assistant",
                link=f"/children/{child}/assistant",
                link_label="assistant.panel.title",
            )
        )

    searches = next((line.asked for line in ins.requests if line.kind == "search"), 0)
    if searches:
        found.append(
            Highlight(
                "insights.h.search",
                90,
                "🔍",
                Tone.ATTENTION,
                {"count": searches},
                suggestion="requests.search.help",
            )
        )

    if ins.comparison is not None:
        found.append(_change(ins, ins.comparison))

    if ctx.managed and ins.ran_out_days >= RAN_OUT_MIN_DAYS:
        often = ins.ran_out_days >= RAN_OUT_SUGGEST_DAYS
        found.append(
            Highlight(
                "insights.h.ran_out",
                70,
                "⏳",
                Tone.INFO,
                {"days": ins.ran_out_days},
                suggestion="insights.s.ran_out" if often else "",
                link=time_page if often else "",
                link_label="time.title" if often else "",
            )
        )
    elif (
        ctx.managed and ins.ran_out_days == 0 and ins.used_days >= 3 and ins.finished_days >= 3
    ):
        found.append(Highlight("insights.h.never_ran_out", 15, "✅", Tone.GOOD))

    if ins.late_nights:
        # With allowed hours already set, the parent has made this choice;
        # the sentence is then only the fact, said calmly.
        offer = not ctx.windows_set
        found.append(
            Highlight(
                "insights.h.late",
                65,
                "🌙",
                Tone.ATTENTION if offer else Tone.INFO,
                {"nights": ins.late_nights},
                suggestion="insights.s.late" if offer else "",
                link=f"{time_page}#windows" if offer else "",
                link_label="insights.l.windows" if offer else "",
            )
        )

    unanswered = sum(line.unanswered for line in ins.requests)
    if unanswered >= UNANSWERED_MIN:
        found.append(
            Highlight(
                "insights.h.unanswered",
                60,
                "⌛",
                Tone.ATTENTION,
                {"count": unanswered},
                suggestion="" if ctx.mail_alerts else "insights.s.mail",
                link="" if ctx.mail_alerts else "/settings#mail",
                link_label="" if ctx.mail_alerts else "mail.panel.title",
            )
        )

    found += _share_highlights(ins, ctx, time_page)

    if ins.earned_s >= EARNED_MIN_SECONDS:
        found.append(
            Highlight(
                "insights.h.earned", 45, "⭐", Tone.GOOD, {"duration": Duration(ins.earned_s)}
            )
        )

    weekday, weekend = ins.weekday_average_s, ins.weekend_average_s
    if weekday is not None and weekend is not None:
        low, high = sorted((weekday, weekend))
        if high >= WEEKEND_MIN_SECONDS and (low == 0 or high / low >= WEEKEND_RATIO):
            found.append(
                Highlight(
                    "insights.h.weekend",
                    40,
                    "📅",
                    Tone.INFO,
                    {"weekend": Duration(weekend), "weekday": Duration(weekday)},
                )
            )

    asked_time = next((line.asked for line in ins.requests if line.kind == "time"), 0)
    if asked_time >= TIME_REQUESTS_MIN:
        found.append(
            Highlight("insights.h.time_requests", 35, "🙋", Tone.INFO, {"count": asked_time})
        )

    if ins.peak is not None:
        start, end = ins.peak
        found.append(
            Highlight(
                "insights.h.peak",
                20,
                "🕓",
                Tone.INFO,
                {"start": f"{start:02d}:00", "end": f"{end % 24:02d}:00"},
            )
        )

    found.sort(key=lambda h: (-h.weight, h.key))
    return found[:limit]


def _change(ins: Insights, comparison: Comparison) -> Highlight:
    vs = Phrase(comparison_phrase(ins.period, comparison.partial))
    change = comparison.change
    if change is Change.SAME:
        return Highlight("insights.h.same", 30, "↔️", Tone.INFO, {"vs": vs})
    if change is Change.LESS:
        return Highlight(
            "insights.h.less", 80, "📉", Tone.INFO, {"vs": vs, "percent": -comparison.percent}
        )
    if change is Change.MUCH_MORE:
        return Highlight("insights.h.much_more", 80, "📈", Tone.INFO, {"vs": vs})
    return Highlight(
        "insights.h.more", 80, "📈", Tone.INFO, {"vs": vs, "percent": comparison.percent}
    )


def _share_highlights(ins: Insights, ctx: Context, time_page: str) -> list[Highlight]:
    """Where the time went, when one kind took a large part of it."""
    if ins.total_s < SHARE_MIN_SECONDS:
        return []
    whole = sum(share.seconds for share in ins.shares) or 1
    found: list[Highlight] = []
    for share in ins.shares:
        fraction = share.seconds / whole
        words = share_phrase(fraction)
        if words is None:
            continue
        params: dict[str, Param] = {"share": Phrase(words)}
        if share.category == Category.LEARNING and fraction >= 0.29:
            found.append(
                Highlight(
                    "insights.h.share.learning",
                    55,
                    "📚",
                    Tone.GOOD,
                    params,
                    suggestion="" if ctx.earning_on else "insights.s.earn",
                    link="" if ctx.earning_on else f"{time_page}?more=1#earn",
                    link_label="" if ctx.earning_on else "time.earn_enabled",
                )
            )
        elif share.category == Category.GAMES and fraction >= 0.42:
            found.append(
                Highlight(
                    "insights.h.share.games",
                    50,
                    "🎮",
                    Tone.INFO,
                    params,
                    suggestion="" if ctx.games_limited else "insights.s.games",
                    link="" if ctx.games_limited else f"{time_page}?more=1#limits",
                    link_label="" if ctx.games_limited else "time.categories.title",
                )
            )
        elif share.category == Category.WEB and fraction >= 0.42:
            found.append(
                Highlight(
                    "insights.h.share.web",
                    45,
                    "🌐",
                    Tone.INFO,
                    params,
                    suggestion="insights.s.web" if ins.sites else "",
                    link="#sites" if ins.sites else "",
                    link_label="insights.sites" if ins.sites else "",
                )
            )
        elif share.category == Category.FREE and fraction >= 0.42:
            found.append(Highlight("insights.h.share.free", 35, "🎨", Tone.INFO, params))
    return found


# --------------------------------------------------------------- in words


@dataclass(frozen=True, slots=True)
class Said:
    """A highlight in the reader's language, ready for a template or a mail."""

    text: str
    suggestion: str
    link: str
    link_label: str
    tone: str
    icon: str


def say(tr: Translator, key: str, params: Mapping[str, Param] | None = None) -> str:
    """Translate `key`, putting durations and phrases into words first."""
    values: dict[str, str | int] = {}
    for name, value in (params or {}).items():
        if isinstance(value, Duration):
            values[name] = duration_text(value.seconds, tr)
        elif isinstance(value, Phrase):
            values[name] = tr(value.key)
        else:
            values[name] = value
    return tr(key, **values)


def render(highlight: Highlight, tr: Translator) -> Said:
    return Said(
        text=say(tr, highlight.key, highlight.params),
        suggestion=say(tr, highlight.suggestion) if highlight.suggestion else "",
        link=highlight.link,
        link_label=tr(highlight.link_label) if highlight.link_label else "",
        tone=highlight.tone.value,
        icon=highlight.icon,
    )


# -------------------------------------------------------------- together


def insights_for(
    session: Session,
    child: Child,
    period: Period,
    today: dt.date,
    span: Span | None = None,
) -> Insights:
    """Load and summarise in one call: what the screens and the mail use."""
    span = span or span_for(period, today)
    return summarize(load_facts(session, child.id, span, today), period, span, today)


def week_highlights(
    session: Session, child: Child, tr: Translator, today: dt.date, limit: int, config: Config
) -> list[Said]:
    """This week's top highlights for one child, in words: the Today card's."""
    found = insights_for(session, child, Period.WEEK, today)
    chosen = highlights(found, context_for(session, child, config), limit)
    return [render(h, tr) for h in chosen]
