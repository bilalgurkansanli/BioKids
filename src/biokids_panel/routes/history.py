"""What a child did on a day, and the two weeks before it (D-192).

The Today screen answers "how much is left"; this one answers the question a
parent asks at dinner - "what did you do on the computer today?" - from data
BioKids already keeps: the ledger's minutes, the per-app seconds, the sites the
browser extension reported (D-187) and the child's requests. Everything here is
deleted on the retention schedule (D-189); nothing is kept for this page.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Query, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core import repo
from biokids_core.db.models import AccessRequest, App, Child, LedgerDay, UsageEvent, UsageTick
from biokids_core.policy import general_allowance_seconds
from biokids_core.types import ChildPolicy, LedgerState
from biokids_panel.common import View
from biokids_panel.routes.audit import _utc_bounds_of_local_day

router = APIRouter()

DAYS = 14
"""How far back the bars go. Two weeks shows a pattern; a month is a chart."""

YEAR_WEEKS = 53
"""The year at a glance (D-245): a column a week, Monday at the top, the way a
Turkish calendar is laid out; 53 so that a whole year shows whatever weekday
today is. Minutes per day are kept for 400 days (`retention.usage_days`)."""

LEVELS = 4
"""How many shades a used day can have; 0 is a day with no minutes."""


@router.get("/history", response_class=HTMLResponse)
def history_view(
    view: View,
    child: Annotated[int | None, Query()] = None,
    day: Annotated[str | None, Query()] = None,
) -> Response:
    if blocked := view.guard():
        return blocked
    session = view.session
    children = list(repo.list_children(session))
    chosen = next((c for c in children if c.id == child), children[0] if children else None)
    today = dt.date.today()
    selected = today
    if day:
        with contextlib.suppress(ValueError):
            selected = min(today, dt.date.fromisoformat(day))
    if chosen is None:
        return view.render("history.html", children=[], child=None)

    locale = view.tr.locale
    return view.render(
        "history.html",
        children=children,
        child=chosen,
        day=selected,
        today=today,
        bars=_bars(session, chosen, today),
        year=year_map(session, chosen, today),
        apps=_apps(session, chosen.id, selected, locale),
        visits=_visits(session, chosen.id, selected),
        requests=_requests(session, chosen.id, selected, locale),
        kept_days=view.ctx.config.retention.activity_days,
    )


def _bars(session: Session, child: Child, today: dt.date) -> list[dict[str, Any]]:
    """Minutes used on each of the last DAYS days, against that day's quota."""
    first = today - dt.timedelta(days=DAYS - 1)
    rows = {
        row.day: row
        for row in session.scalars(
            select(LedgerDay).where(LedgerDay.child_id == child.id, LedgerDay.day >= first)
        )
    }
    used = {day: row.general_s for day, row in rows.items()}
    policy = repo.load_policy(session, child, today=today)
    days = [first + dt.timedelta(days=n) for n in range(DAYS)]
    quotas = {d: allowance(policy, child.id, d, rows.get(d)) for d in days}
    top = max([*used.values(), *quotas.values(), 60])
    return [
        {
            "day": d,
            "seconds": used.get(d, 0),
            "quota_seconds": quotas[d],
            "percent": round(100 * used.get(d, 0) / top),
            "over": quotas[d] > 0 and used.get(d, 0) > quotas[d],
        }
        for d in days
    ]


def allowance(
    policy: ChildPolicy | None, child_id: int, day: dt.date, row: LedgerDay | None
) -> int:
    """The day's time as the engine gave it, in seconds: its quota and what was
    added to it that day - a parent's "+10 dk", earned minutes, "Bitireyim".
    Measured by the quota alone, every day a parent added minutes to looked
    over its limit (D-250)."""
    if policy is None:
        return 0
    state = LedgerState(
        child_id=child_id,
        day=day,
        general_used_s=row.general_s if row is not None else 0,
        earned_s=row.earned_s if row is not None else 0,
        granted_s=row.granted_s if row is not None else 0,
        finish_up_s=(row.finish_up_s or 0) if row is not None else 0,
    )
    return general_allowance_seconds(policy, state)


def day_level(seconds: int, quota_seconds: int) -> int:
    """The shade of a day (D-245): 0 for none, then by how much of that day's
    quota was used - a third, two thirds, all of it, more than it. A day with
    no quota to measure by (a day off) is shaded by the hour."""
    if seconds < 60:
        return 0
    if quota_seconds <= 0:
        return min(LEVELS, 1 + seconds // 3600)
    if seconds > quota_seconds:
        return LEVELS
    return min(LEVELS - 1, 1 + (3 * seconds) // (quota_seconds + 1))


def year_map(session: Session, child: Child, today: dt.date) -> dict[str, Any]:
    """The last year as GitHub draws a contribution graph (D-245): one cell a
    day, a column a week, shaded by the minutes used against that day's quota.

    Weeks start on Monday. The days after today in this week are left out; the
    days before the first one kept, or before the child was added, are empty.
    """
    last_monday = today - dt.timedelta(days=today.weekday())
    first = last_monday - dt.timedelta(weeks=YEAR_WEEKS - 1)
    rows = {
        row.day: row
        for row in session.scalars(
            select(LedgerDay).where(LedgerDay.child_id == child.id, LedgerDay.day >= first)
        )
    }
    policy = repo.load_policy(session, child, today=today)
    weeks: list[list[dict[str, Any] | None]] = []
    months: list[dict[str, Any]] = []
    total = active = 0
    for week in range(YEAR_WEEKS):
        monday = first + dt.timedelta(weeks=week)
        if monday.day <= 7:
            # The week a month begins in carries its name, as GitHub's does.
            months.append({"week": week, "month": monday.month})
        cells: list[dict[str, Any] | None] = []
        for offset in range(7):
            day = monday + dt.timedelta(days=offset)
            if day > today:
                cells.append(None)
                continue
            row = rows.get(day)
            seconds = row.general_s if row is not None else 0
            quota = allowance(policy, child.id, day, row)
            total += seconds
            active += seconds >= 60
            cells.append({"day": day, "seconds": seconds, "level": day_level(seconds, quota)})
        weeks.append(cells)
    return {"weeks": weeks, "months": months, "total_seconds": total, "active_days": active}


def _apps(session: Session, child_id: int, day: dt.date, locale: str) -> list[dict[str, Any]]:
    """Seconds per app on `day`, most first. The launcher itself is "" here."""
    totals: dict[str, int] = {}
    for row in session.scalars(
        select(UsageTick).where(UsageTick.child_id == child_id, UsageTick.day == day)
    ):
        totals[row.app_slug] = totals.get(row.app_slug, 0) + row.seconds
    names = {
        app.slug: app.name(locale)
        for app in session.scalars(select(App).where(App.slug.in_([s for s in totals if s])))
    }
    return [
        {"slug": slug, "name": names.get(slug, slug), "seconds": seconds}
        for slug, seconds in sorted(totals.items(), key=lambda item: -item[1])
        if seconds >= 30
    ]


def _visits(session: Session, child_id: int, day: dt.date) -> list[dict[str, Any]]:
    """The sites opened on `day`: each host once, with when it was first and last
    opened. A site open all afternoon is one line, not two hundred."""
    since, until = _utc_bounds_of_local_day(day)
    hosts: dict[str, dict[str, Any]] = {}
    for event in session.scalars(
        select(UsageEvent)
        .where(
            UsageEvent.child_id == child_id,
            UsageEvent.kind == "visit",
            UsageEvent.ts_wall >= since,
            UsageEvent.ts_wall <= until,
        )
        .order_by(UsageEvent.ts_wall)
    ):
        host = str(event.payload.get("host", "")).removeprefix("www.")
        if not host:
            continue
        entry = hosts.setdefault(host, {"host": host, "first": event.ts_wall, "times": 0})
        entry["last"] = event.ts_wall
        entry["times"] += 1
    return sorted(hosts.values(), key=lambda entry: entry["first"])


def _requests(
    session: Session, child_id: int, day: dt.date, locale: str
) -> list[dict[str, Any]]:
    since, until = _utc_bounds_of_local_day(day)
    apps = {app.slug: app.name(locale) for app in repo.list_apps(session)}
    rows = session.scalars(
        select(AccessRequest)
        .where(
            AccessRequest.child_id == child_id,
            AccessRequest.created_at >= since,
            AccessRequest.created_at <= until,
        )
        .order_by(AccessRequest.created_at)
    )
    return [
        {
            "kind": row.kind,
            "what": _what(row, apps),
            "status": row.status,
            "minutes": row.minutes,
            "created_at": row.created_at,
        }
        for row in rows
    ]


def _what(row: AccessRequest, apps: dict[str, str]) -> str:
    if row.kind == "site":
        return urlsplit(row.url).hostname or row.url
    if row.kind == "app":
        return apps.get(row.url, row.url)
    return ""
