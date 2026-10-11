"""Insights: a child's week in numbers and in plain words (D-206).

History (D-192) answers "what did you do today?"; this answers the questions a
parent asks over a week or a month - is it more than before, where does the
time go, is there anything I should do. The arithmetic and the rules behind
every sentence are in `biokids_core.insights`; this module only picks the
child and the period and turns the numbers into bars a server can draw.

Plain HTML, no script and no chart library (D-129): the bars are elements with
a height or a width, and every number they show is also written out.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core import insights, repo
from biokids_core.db.models import App, Child
from biokids_core.i18n import Translator
from biokids_panel.common import View

router = APIRouter()

DAY_LABELS_UP_TO = 14
"""Up to two weeks every bar is labelled with its day; a month only its Mondays."""

DAY_LINKS_UP_TO = 14
"""Up to two weeks every bar opens its day in History. Thirty bars on a phone are
six pixels wide each: not something to tap, and not offered as one."""

HOUR_LABELS = (0, 6, 12, 18)


@router.get("/insights", response_class=HTMLResponse)
def insights_view(
    view: View,
    child: Annotated[int | None, Query()] = None,
    period: Annotated[str | None, Query()] = None,
) -> Response:
    if blocked := view.guard():
        return blocked
    session, tr = view.session, view.tr
    children = list(repo.list_children(session))
    chosen = next((c for c in children if c.id == child), children[0] if children else None)
    picked = insights.Period.parse(period)
    if chosen is None:
        return view.render("insights.html", children=[], child=None, period=picked)

    today = dt.date.today()
    found = insights.insights_for(session, chosen, picked, today)
    context = insights.context_for(session, chosen, view.ctx.config)
    day_rows = _day_rows(session, chosen, found, tr)
    return view.render(
        "insights.html",
        children=children,
        child=chosen,
        period=picked,
        periods=list(insights.Period),
        ins=found,
        managed=context.managed,
        # The intro says where these sentences go, and with the Sunday mail on
        # that is not "nowhere".
        mail_weekly=context.mail_weekly,
        highlights=[insights.render(h, tr) for h in insights.highlights(found, context)],
        delta=_delta(found, tr),
        day_rows=day_rows,
        days_linked=len(day_rows) <= DAY_LINKS_UP_TO,
        hour_rows=_hour_rows(found),
        app_rows=_app_rows(session, found, tr.locale),
        request_rows=_request_rows(found, tr),
        kept_days=view.ctx.config.retention.activity_days,
    )


def _delta(found: insights.Insights, tr: Translator) -> dict[str, str] | None:
    """The average's change, as the stat card shows it: an arrow and a percent,
    or words beyond tripling, by the same rule as the highlight."""
    comparison = found.comparison
    if comparison is None:
        return None
    change = comparison.change
    if change is insights.Change.SAME:
        text = tr("insights.delta.same")
    elif change is insights.Change.LESS:
        text = tr("insights.delta.down", percent=-comparison.percent)
    elif change is insights.Change.MUCH_MORE:
        text = tr("insights.delta.much_more")
    else:
        text = tr("insights.delta.up", percent=comparison.percent)
    return {
        "text": text,
        "vs": tr(insights.comparison_phrase(found.period, comparison.partial)),
    }


def _day_rows(
    session: Session, child: Child, found: insights.Insights, tr: Translator
) -> list[dict[str, Any]]:
    """One bar per day, on a scale shared with the day's quota.

    The quota is today's policy, as on the history screen: a quota changed in
    the middle of the month is drawn as it is now. Its line shows how full a
    day was; the bar turns warm only where the time actually ran out.
    """
    policy = repo.load_policy(session, child, today=found.today)
    quotas = {
        bar.day: (policy.quota_minutes(bar.day) * 60 if policy else 0) for bar in found.days
    }
    # A tenth of headroom, so the highest quota's line is drawn inside the bar
    # rather than on its top edge, where it cannot be seen.
    top = max([*(bar.seconds for bar in found.days), *quotas.values(), 60]) * 1.1
    labelled = len(found.days) <= DAY_LABELS_UP_TO
    rows = []
    for bar in found.days:
        if labelled:
            label = tr(f"day.short.{bar.day.weekday()}")
        else:
            label = bar.day.strftime("%d") if bar.day.weekday() == 0 else ""
        rows.append(
            {
                "day": bar.day,
                "seconds": bar.seconds,
                "percent": round(100 * bar.seconds / top),
                "quota_percent": round(100 * quotas[bar.day] / top) if quotas[bar.day] else 0,
                "ran_out": bar.ran_out,
                "future": bar.future,
                "today": bar.day == found.today,
                "label": label,
            }
        )
    return rows


def _hour_rows(found: insights.Insights) -> list[dict[str, Any]]:
    top = max([*found.hours, 1])
    return [
        {
            "hour": hour,
            "seconds": seconds,
            "percent": round(100 * seconds / top),
            "label": f"{hour:02d}" if hour in HOUR_LABELS else "",
        }
        for hour, seconds in enumerate(found.hours)
    ]


def _request_rows(found: insights.Insights, tr: Translator) -> list[dict[str, Any]]:
    """Each kind of request with how it was answered, in one line.

    A refused search has no answer to count: there is nothing to allow, and
    the parent's "we will talk" is the whole of it (D-202).
    """
    rows = []
    for line in found.requests:
        answers = (
            []
            if line.kind == "search"
            else [
                tr(key, count=count)
                for count, key in (
                    (line.approved, "insights.req.approved"),
                    (line.denied, "insights.req.denied"),
                    (line.unanswered, "insights.req.unanswered"),
                    # Still open: "bekliyor", in the present, never "cevapsız".
                    (line.waiting, "insights.req.waiting"),
                )
                if count
            ]
        )
        rows.append(
            {
                "name": tr(f"insights.req.{line.kind}"),
                "asked": line.asked,
                "detail": " · ".join(answers),
            }
        )
    return rows


def _app_rows(session: Session, found: insights.Insights, locale: str) -> list[dict[str, Any]]:
    """The apps with their names in the parent's language, and a bar each."""
    slugs = [line.slug for line in found.apps if line.slug]
    names = {
        app.slug: app.name(locale)
        for app in session.scalars(select(App).where(App.slug.in_(slugs)))
    }
    top = max([line.seconds for line in found.apps] or [1])
    return [
        {
            "slug": line.slug,
            "name": names.get(line.slug, line.slug),
            "category": line.category,
            "seconds": line.seconds,
            "percent": max(2, round(100 * line.seconds / top)),
            "trend": line.trend.value,
        }
        for line in found.apps
    ]
