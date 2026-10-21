"""Special days: "today is different", and holidays (D-215).

A parent says it from Today with one tap - an hour more today, or no daily
limit with the allowed hours still in force - or from the child's time screen
for a range of dates, "Tatil: 20-31 Ocak", which follows the weekend's rules or
a quota of its own. Rows of `day_override`; the policy engine reads them where
it picks a day's quota and hours (`ChildPolicy.special_on`).

Three rules hold whatever the panel sends:

* **Never the past.** A day that is over has been enforced, and the history
  screen shows what it was given. A range is saved from today on, and a
  range cancelled half-way keeps its past days, cut to yesterday.
* **One tap is one choice.** "+60 today" after "+30 today" is an hour more, not
  an hour and a half: the day's tap is replaced, never stacked. A range that
  covers today - Bayram, set on the time screen - is left alone, and the tap
  adds to it.
* **Sanity bounds.** A typed year does not turn the whole year into a weekend.

Nothing here commits, audits or tells the daemon: the panel does those, as it
does for every other change to a child's rules.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core.db.models import DayOverride
from biokids_core.types import SpecialDay

TODAY_EXTRA_MINUTES: Final = (30, 60, 120)
"""The taps on Today. Anything else is refused rather than rounded."""

MAX_RANGE_DAYS: Final = 100
"""A summer holiday fits; a mistyped year does not."""

LABEL_MAX: Final = 40


class SpecialDayError(ValueError):
    """A special day that will not be saved. The message is a catalogue key."""


def load(session: Session, child_id: int) -> tuple[SpecialDay, ...]:
    """Every special day of one child, as the engine reads them.

    Past ones included: the history and insights screens draw each day's bar
    against the quota it had, and retention bounds how many there are.
    """
    rows = session.scalars(
        select(DayOverride).where(DayOverride.child_id == child_id).order_by(DayOverride.id)
    ).all()
    return tuple(as_special(row) for row in rows)


def as_special(row: DayOverride) -> SpecialDay:
    return SpecialDay(
        start=row.start_day,
        end=row.end_day,
        extra_minutes=max(0, row.extra_minutes or 0),
        quota_minutes=row.quota_minutes,
        no_limit=bool(row.no_limit),
        weekend_rules=bool(row.weekend_rules),
        order=row.id,
    )


def current(session: Session, child_id: int, today: dt.date) -> list[DayOverride]:
    """Today's and the ones still ahead, soonest first: what can be cancelled."""
    return list(
        session.scalars(
            select(DayOverride)
            .where(DayOverride.child_id == child_id, DayOverride.end_day >= today)
            .order_by(DayOverride.start_day, DayOverride.id)
        ).all()
    )


def is_tap(row: DayOverride) -> bool:
    """A choice made with one tap on Today, as opposed to a range or a day's rules."""
    return row.start_day == row.end_day and row.quota_minutes is None and not row.weekend_rules


def set_today(
    session: Session,
    child_id: int,
    today: dt.date,
    *,
    extra_minutes: int = 0,
    no_limit: bool = False,
) -> tuple[DayOverride, list[dict[str, Any]]]:
    """Make today special with one tap, replacing an earlier tap of today.

    Returns the new row and what it replaced, for the audit log.
    """
    if no_limit:
        extra_minutes = 0
    elif extra_minutes not in TODAY_EXTRA_MINUTES:
        raise SpecialDayError("error.invalid_input")
    replaced = [
        row
        for row in current(session, child_id, today)
        if row.start_day == today and is_tap(row)
    ]
    before = [snapshot(row) for row in replaced]
    for row in replaced:
        session.delete(row)
    row = DayOverride(
        child_id=child_id,
        start_day=today,
        end_day=today,
        extra_minutes=extra_minutes,
        no_limit=no_limit,
    )
    session.add(row)
    session.flush()
    return row, before


def add_range(
    session: Session,
    child_id: int,
    today: dt.date,
    *,
    start: dt.date,
    end: dt.date,
    weekend_rules: bool = False,
    quota_minutes: int | None = None,
    label: str = "",
) -> DayOverride:
    """A holiday: from `start` to `end`, the weekend's rules or a quota of its own.

    A start in the past is moved to today rather than refused: a parent who
    enters the whole holiday on its third day means the rest of it, and the
    days already behind were enforced as they were.
    """
    if end < start:
        raise SpecialDayError("special.error.order")
    if end < today:
        raise SpecialDayError("special.error.past")
    start = max(start, today)
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise SpecialDayError("special.error.too_long")
    if weekend_rules == (quota_minutes is not None):
        # Exactly one of the two: the form offers them as a choice.
        raise SpecialDayError("error.invalid_input")
    if quota_minutes is not None and not 0 <= quota_minutes <= 24 * 60:
        raise SpecialDayError("error.invalid_input")
    row = DayOverride(
        child_id=child_id,
        start_day=start,
        end_day=end,
        weekend_rules=weekend_rules,
        quota_minutes=quota_minutes,
        label=clean_label(label),
    )
    session.add(row)
    session.flush()
    return row


def cancel(
    session: Session, child_id: int, override_id: int, today: dt.date
) -> dict[str, Any] | None:
    """Take a special day back, from today on. None if there is nothing to take.

    Days already over keep it: they were enforced with it, and the history
    screen would otherwise draw them against a quota they never had.
    """
    row = session.get(DayOverride, override_id)
    if row is None or row.child_id != child_id or row.end_day < today:
        return None
    before = snapshot(row)
    if row.start_day < today:
        row.end_day = today - dt.timedelta(days=1)
    else:
        session.delete(row)
    session.flush()
    return before


def clean_label(raw: str) -> str:
    return " ".join(raw.split())[:LABEL_MAX]


def snapshot(row: DayOverride) -> dict[str, Any]:
    """What the audit log records about one special day."""
    return {
        "start": row.start_day.isoformat(),
        "end": row.end_day.isoformat(),
        "extra_minutes": row.extra_minutes,
        "quota_minutes": row.quota_minutes,
        "no_limit": bool(row.no_limit),
        "weekend_rules": bool(row.weekend_rules),
        "label": row.label,
    }


def covering(rows: Sequence[DayOverride], day: dt.date) -> list[DayOverride]:
    """The rows in force on `day`."""
    return [row for row in rows if row.start_day <= day <= row.end_day]
