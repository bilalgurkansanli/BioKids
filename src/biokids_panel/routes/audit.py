"""The audit log: what changed, who changed it, filtered by child, kind and day."""

from __future__ import annotations

import contextlib
import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import HTMLResponse

from biokids_core import repo
from biokids_panel.common import View

router = APIRouter()


@router.get("/audit", response_class=HTMLResponse)
def audit_view(
    view: View,
    child: Annotated[int | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    day: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query()] = 1,
) -> Response:
    if blocked := view.guard():
        return blocked
    per_page = 50
    since = until = None
    if day:
        with contextlib.suppress(ValueError):
            chosen = dt.date.fromisoformat(day)
            since, until = _utc_bounds_of_local_day(chosen)
    entries = repo.recent_audit(
        view.session,
        limit=per_page,
        offset=max(0, page - 1) * per_page,
        child_id=child,
        action=action or None,
        since=since,
        until=until,
    )
    children = repo.list_children(view.session)
    return view.render(
        "audit.html",
        entries=entries,
        who=_names(repo.parent_names(view.session), {c.id: c.display_name for c in children}),
        children=children,
        actions=repo.audit_actions(view.session),
        filter_child=child,
        filter_action=action or "",
        filter_day=day or "",
        page=max(1, page),
        has_more=len(entries) == per_page,
    )


def _names(parents: dict[int, str], children: dict[int, str]) -> dict[str, str]:
    """`parent:2` -> "Baba", `child:1` -> "Elif": the names the rows are about.

    Which parent did something is the point of the log once there are two
    (D-218). A removed parent keeps their row, so their name is still here; a
    removed child does not, and their rows keep the bare reference.
    """
    names = {f"parent:{pid}": name for pid, name in parents.items() if name}
    names.update({f"child:{cid}": name for cid, name in children.items()})
    return names


def _utc_bounds_of_local_day(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """The parent's day, expressed the way the rows are stored.

    `AuditLog.ts` is naive UTC (`utcnow()`), and the screen renders it by
    attaching UTC and converting to local. The filter did neither: it built
    local-midnight boundaries and compared them raw, so in Istanbul (UTC+3)
    every row between 00:00 and 03:00 fell out of its own day - which is the
    window a parent asking "what happened last night" means. The unfiltered
    list showed the 01:30 row under today and filtering for today hid it.
    """
    local_start = dt.datetime.combine(day, dt.time.min).astimezone()
    local_end = dt.datetime.combine(day, dt.time.max).astimezone()
    return (
        local_start.astimezone(dt.UTC).replace(tzinfo=None),
        local_end.astimezone(dt.UTC).replace(tzinfo=None),
    )
