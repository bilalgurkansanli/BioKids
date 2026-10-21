"""Special days (D-215): from Today with one tap, and from the child's time screen.

"Bugün özel gün" gives today a different quota - an hour more, two, or no daily
limit with the allowed hours still in force - and a range of dates ("Tatil:
20-31 Ocak") can follow the weekend's rules or a quota of its own. Each is
audited, shown where the parent will look for it, and can be taken back.

After a change the daemon is told with `day_changed` rather than a plain
reload: a child whose time ran out a minute ago and whose day is now longer
must be let back in, as "+10 dk" does, or the parent's tap would leave them
at a lock screen they have no password for (D-99).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Form, Response
from sqlalchemy.orm import Session

from biokids_core import repo, special_days
from biokids_core.db.models import DayOverride
from biokids_core.i18n import Translator, duration_text
from biokids_core.ipc import Op
from biokids_panel import auth
from biokids_panel.common import View, _call_daemon, see_other

router = APIRouter()

TODAY_CHOICES: dict[str, tuple[int, bool]] = {
    "30": (30, False),
    "60": (60, False),
    "120": (120, False),
    "nolimit": (0, True),
}
"""The one-tap choices on Today and on the time screen: (extra minutes, no limit)."""


@router.post("/children/{child_id}/special/today")
def special_today(
    view: View,
    child_id: int,
    choice: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/today",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """One tap: today is special. Replaces an earlier tap of today."""
    back = auth.safe_next(next)
    if blocked := view.guard_form(csrf_token, _with(back, "error=csrf")):
        return blocked
    session = view.session
    if repo.get_child(session, child_id) is None or choice not in TODAY_CHOICES:
        return see_other(_with(back, "error=error.invalid_input"))
    extra, no_limit = TODAY_CHOICES[choice]
    row, replaced = special_days.set_today(
        session, child_id, _today(), extra_minutes=extra, no_limit=no_limit
    )
    repo.audit(
        session,
        actor=view.actor,
        action="special_day_set",
        target=f"child:{child_id}",
        detail={"special": special_days.snapshot(row), "replaced": replaced},
    )
    session.commit()
    return _told(view, child_id, back, "special")


@router.post("/children/{child_id}/special/range")
def special_range(
    view: View,
    child_id: int,
    start: Annotated[str, Form()] = "",
    end: Annotated[str, Form()] = "",
    rule: Annotated[str, Form()] = "weekend",
    quota_minutes: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Some days with rules of their own: a holiday, a bayram."""
    page = f"/children/{child_id}/time"
    if blocked := view.guard_form(csrf_token, f"{page}?error=csrf#special"):
        return blocked
    session = view.session
    if repo.get_child(session, child_id) is None:
        return see_other("/children?error=1")
    try:
        first = dt.date.fromisoformat(start.strip())
        last = dt.date.fromisoformat(end.strip()) if end.strip() else first
    except ValueError:
        # Not a date: what a phone's own picker cannot send, and an empty
        # field or a hand-made form can.
        return see_other(f"{page}?error=special.error.dates#special")
    own: int | None = None
    if rule == "quota":
        if not quota_minutes.strip().isdigit():
            return see_other(f"{page}?error=special.error.minutes#special")
        # Clamped to a day, as the time screen clamps its quotas.
        own = min(int(quota_minutes.strip()), 24 * 60)
    try:
        row = special_days.add_range(
            session,
            child_id,
            _today(),
            start=first,
            end=last,
            weekend_rules=rule != "quota",
            quota_minutes=own,
            label=label,
        )
    except special_days.SpecialDayError as exc:
        return see_other(f"{page}?error={exc}#special")
    repo.audit(
        session,
        actor=view.actor,
        action="special_day_set",
        target=f"child:{child_id}",
        detail={"special": special_days.snapshot(row)},
    )
    session.commit()
    return _told(view, child_id, page, "saved")


@router.post("/children/{child_id}/special/{override_id}/cancel")
def special_cancel(
    view: View,
    child_id: int,
    override_id: int,
    next: Annotated[str, Form()] = "/today",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Take a special day back, from today on; the days already over keep it."""
    back = auth.safe_next(next)
    if blocked := view.guard_form(csrf_token, _with(back, "error=csrf")):
        return blocked
    session = view.session
    before = special_days.cancel(session, child_id, override_id, _today())
    if before is None:
        return see_other(_with(back, "error=error.not_found"))
    repo.audit(
        session,
        actor=view.actor,
        action="special_day_removed",
        target=f"child:{child_id}",
        detail={"special": before},
    )
    session.commit()
    return _told(view, child_id, back, "special_removed")


# ------------------------------------------------------------------- helpers


def _today() -> dt.date:
    """The family's today: the machine's local date, as the rest of the panel reads it."""
    return dt.date.today()


def _with(page: str, query: str) -> str:
    path, _, fragment = page.partition("#")
    joined = f"{path}{'&' if '?' in path else '?'}{query}"
    return f"{joined}#{fragment}" if fragment else joined


def _told(view: View, child_id: int, back: str, flag: str) -> Response:
    """Back where the parent was, saying so only once the daemon has the change.

    Today says it in its own words, naming the child; the time screen says
    "Kaydedildi" as it does for its other forms.
    """
    if not _call_daemon(view.ctx, Op.DAY_CHANGED, child_id=child_id):
        return see_other(_with(back, "error=daemon"))
    if back.split("?")[0].split("#")[0] == "/today":
        return see_other(f"/today?{flag}={child_id}")
    return see_other(_with(back, "saved=1"))


def describe(row: DayOverride, tr: Translator, today: dt.date) -> str:
    """One special day, in the words a parent reads on Today and the time screen.

    "Bugün özel gün: +1 sa", "Bugün özel gün: süre sınırı yok", "Tatil (20.01 -
    31.01): hafta sonu kuralları", "21.10: günde 3 sa".
    """
    if special_days.is_tap(row):
        if row.no_limit:
            return tr("special.today.no_limit")
        return tr("special.today.extra", duration=duration_text(row.extra_minutes * 60, tr))
    when = _dates(row, tr, today)
    if row.label:
        when = tr("special.when.labelled", label=row.label, dates=when)
    if row.weekend_rules:
        return tr("special.range.weekend", when=when)
    if row.no_limit:
        return tr("special.range.no_limit", when=when)
    return tr(
        "special.range.quota",
        when=when,
        duration=duration_text((row.quota_minutes or 0) * 60, tr),
    )


def _dates(row: DayOverride, tr: Translator, today: dt.date) -> str:
    if row.start_day == row.end_day:
        return tr("special.when.today") if row.start_day == today else _day(row.start_day)
    return tr("special.when.range", start=_day(row.start_day), end=_day(row.end_day))


def _day(day: dt.date) -> str:
    return day.strftime("%d.%m")


def lines_for(
    session: Session, child_ids: Sequence[int], tr: Translator, today: dt.date
) -> dict[int, list[dict[str, Any]]]:
    """child id -> today's special days, each with its sentence and its id.

    Read from the database, as Today's week card is: the line and its "Kaldır"
    are there with the daemon down, and a failure costs only the line.
    """
    found: dict[int, list[dict[str, Any]]] = {}
    for child_id in child_ids:
        rows = special_days.covering(special_days.current(session, child_id, today), today)
        if rows:
            found[child_id] = [{"id": row.id, "text": describe(row, tr, today)} for row in rows]
    return found


def listed(
    session: Session, child_id: int, tr: Translator, today: dt.date
) -> list[dict[str, Any]]:
    """Today's and the coming special days of one child, for the time screen."""
    return [
        {"id": row.id, "text": describe(row, tr, today), "today": row.start_day <= today}
        for row in special_days.current(session, child_id, today)
    ]
