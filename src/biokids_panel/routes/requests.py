"""What the children asked for: more time, an app, a site (D-173)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Response
from fastapi.responses import HTMLResponse

from biokids_core import repo
from biokids_core.ipc import Op
from biokids_panel.common import View, _call_daemon, see_other

REQUEST_GRANT_CHOICES = (15, 30, 60)
"""What "more time" can be answered with, in minutes: three buttons, no typing."""

router = APIRouter()


@router.get("/requests", response_class=HTMLResponse)
def requests_view(view: View) -> Response:
    """What the children asked for, newest first, answerable in one tap (D-173)."""
    if blocked := view.guard():
        return blocked
    session, tr = view.session, view.tr
    children = {child.id: child for child in repo.list_children(session)}
    apps = {app.slug: app for app in repo.list_apps(session)}
    pending, decided = repo.access_requests(session)

    def describe(row: Any) -> dict[str, Any]:
        child = children.get(row.child_id)
        app = apps.get(row.url) if row.kind == "app" else None
        return {
            "id": row.id,
            "kind": row.kind,
            "child": child.display_name if child else "?",
            "avatar": child.avatar if child else "",
            "what": app.name(tr.locale) if app else row.url,
            "status": row.status,
            "minutes": row.minutes,
            "created_at": row.created_at,
        }

    return view.render(
        "requests.html",
        pending=[describe(row) for row in pending],
        decided=[describe(row) for row in decided],
        grant_choices=REQUEST_GRANT_CHOICES,
    )


@router.post("/requests/{request_id}/decide")
def request_decide(
    view: View,
    request_id: int,
    approve: Annotated[str, Form()] = "",
    minutes: Annotated[int, Form()] = 0,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, "/requests?error=csrf"):
        return blocked
    yes = approve == "yes"
    ok = _call_daemon(
        view.ctx,
        Op.DECIDE_REQUEST,
        request_id=request_id,
        approve=yes,
        minutes=minutes if yes else 0,
        actor=view.actor,
    )
    return see_other("/requests?saved=1" if ok else "/requests?error=daemon")
