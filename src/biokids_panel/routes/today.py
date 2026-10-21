"""The Today screen, its live counters, and the buttons on each child's card."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from biokids_core import insights, repo
from biokids_core.clock import read_boot_id
from biokids_core.errors import IpcError
from biokids_core.i18n import Translator
from biokids_core.ipc import AsyncDaemonClient, Op
from biokids_panel.common import View, _call_daemon, _daemon_status, see_other
from biokids_panel.context import PanelContext
from biokids_panel.routes import special_days

log = logging.getLogger(__name__)

MAX_GRANT_SECONDS = 4 * 3600
"""A sanity bound on "+10 dk". A typo in a form field should not hand a child
the rest of the week."""

router = APIRouter()


@router.get("/today", response_class=HTMLResponse)
def today(view: View) -> Response:
    if blocked := view.guard():
        return blocked
    status, reachable = _daemon_status(view.ctx, view.session)
    children = status.get("children", [])
    return view.render(
        "today.html",
        children=children,
        daemon_up=reachable,
        specials=special_lines(view, children),
        notice=_action_notice(view.request, view.tr, children),
        # Questions the assistant refused and the parent has not seen (D-165):
        # above the cards, like a pending request, because they are why a
        # parent might want to talk to their child today.
        assistant_alerts=[
            {"child": child, "count": count}
            for child_id, count in sorted(repo.unseen_assistant_alerts(view.session).items())
            if (child := repo.get_child(view.session, child_id)) is not None
        ],
        # And searches the browser refused (D-222): the words are in Analiz.
        search_alerts=[
            {"child": child, "count": count}
            for child_id, count in sorted(repo.unseen_search_alerts(view.session).items())
            if (child := repo.get_child(view.session, child_id)) is not None
        ],
        restart_needed=restart_needed(view),
        week=week_cards(view),
    )


TODAY_HIGHLIGHTS = 3
"""Highlights per child on Today: the screen is for glancing (UX.md section 3.2)."""


def week_cards(view: View) -> list[dict[str, Any]]:
    """This week's top highlights per child, for the card under the children (D-206).

    Read from the database alone, so the card is there with the daemon down.
    A convenience, so a failure here costs the card and nothing else: Today is
    the screen a parent opens to grant time or lock a screen, and must render.
    """
    cards: list[dict[str, Any]] = []
    try:
        today = dt.date.today()
        for child in repo.list_children(view.session):
            cards.append(
                {
                    "child": child,
                    "highlights": insights.week_highlights(
                        view.session, child, view.tr, today, TODAY_HIGHLIGHTS, view.ctx.config
                    ),
                }
            )
    except Exception:
        log.exception("could not work out this week's highlights; Today is shown without them")
        # Only reads happened; the rollback leaves the session clean for the
        # rest of the page.
        with contextlib.suppress(Exception):
            view.session.rollback()
        return []
    return cards


def restart_needed(view: View) -> bool:
    """True until the machine has restarted once since setup finished (D-195)."""
    from biokids_panel.wizard import SETTING_RESTART_BOOT

    finished_in = repo.get_setting(view.session, SETTING_RESTART_BOOT)
    return bool(finished_in) and finished_in == read_boot_id()


@router.post("/system/reboot")
def reboot(
    view: View,
    csrf_token: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    """The one restart a fresh install needs (D-155, D-195), from the banner."""
    if blocked := view.guard_form(csrf_token, "/today?error=csrf"):
        return blocked
    if confirm != "yes":
        # As for "Kapat": with JavaScript off the confirm() never runs.
        return see_other("/today")
    ok = _call_daemon(view.ctx, Op.REBOOT_NOW, actor=view.actor)
    return see_other("/today?rebooting=1" if ok else "/today?error=daemon")


@router.get("/today/cards", response_class=HTMLResponse)
def today_cards(view: View) -> Response:
    """The card list on its own, for a client that polls instead of streaming."""
    if blocked := view.guard():
        return blocked
    status, reachable = _daemon_status(view.ctx, view.session)
    children = status.get("children", [])
    return view.render(
        "partials/child_cards.html",
        children=children,
        daemon_up=reachable,
        specials=special_lines(view, children),
    )


def special_lines(view: View, children: Sequence[dict[str, Any]]) -> dict[int, Any]:
    """Today's special days per child (D-215), from the database.

    Like the week card, a convenience a failure costs and Today survives: the
    line goes, the cards and their buttons stay.
    """
    try:
        return special_days.lines_for(
            view.session,
            [int(child.get("child_id", 0)) for child in children],
            view.tr,
            dt.date.today(),
        )
    except Exception:
        log.exception("could not read today's special days; Today is shown without them")
        with contextlib.suppress(Exception):
            view.session.rollback()
        return {}


@router.get("/events")
async def events(view: View) -> Response:
    """Server-sent events, relayed from the daemon (T4.4).

    The panel does not invent events; it forwards the daemon's. If the
    daemon is unreachable the stream still opens and says so, because a
    Today screen that silently stops updating is indistinguishable from a
    child who stopped using the computer.
    """
    if not view.state.authenticated:
        return Response(status_code=401)
    return StreamingResponse(
        _event_stream(view.ctx, view.request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ------------------------------------------------------------ parent actions


@router.post("/children/{child_id}/grant")
def grant(
    view: View,
    child_id: int,
    minutes: Annotated[int, Form()] = 10,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, "/today?error=csrf"):
        return blocked
    minutes = max(1, min(minutes, MAX_GRANT_SECONDS // 60))
    ok = _call_daemon(
        view.ctx, Op.GRANT_TIME, child_id=child_id, seconds=minutes * 60, actor=view.actor
    )
    return see_other(
        f"/today?granted={child_id}&minutes={minutes}" if ok else "/today?error=daemon"
    )


@router.post("/children/{child_id}/unlock")
def unlock_now(view: View, child_id: int, csrf_token: Annotated[str, Form()] = "") -> Response:
    """Back in, without adding time (D-173). "Kilitle" had no way back but "+10 dk"."""
    if blocked := view.guard_form(csrf_token, "/today?error=csrf"):
        return blocked
    ok = _call_daemon(view.ctx, Op.UNLOCK_NOW, child_id=child_id, actor=view.actor)
    return see_other(f"/today?unlocked={child_id}" if ok else "/today?error=daemon")


@router.post("/children/{child_id}/lock")
def lock_now(view: View, child_id: int, csrf_token: Annotated[str, Form()] = "") -> Response:
    if blocked := view.guard_form(csrf_token, "/today?error=csrf"):
        return blocked
    ok = _call_daemon(view.ctx, Op.LOCK_NOW, child_id=child_id, actor=view.actor)
    return see_other(f"/today?locked={child_id}" if ok else "/today?error=daemon")


@router.post("/children/{child_id}/poweroff")
def poweroff(
    view: View,
    child_id: int,
    csrf_token: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, "/today?error=csrf"):
        return blocked
    if confirm != "yes":
        # Reachable with JavaScript off, where the onsubmit confirm() never
        # runs and a mis-tap on "Kapat" - which sits beside "Kilitle" in the
        # same row of three - would otherwise power the machine off under a
        # child with no chance to cancel.
        return see_other(f"/today?confirm=poweroff&child={child_id}")
    ok = _call_daemon(view.ctx, Op.POWEROFF_NOW, actor=view.actor)
    return see_other("/today?poweroff=1" if ok else "/today?error=daemon")


# ------------------------------------------------------------------- helpers


def _action_notice(
    request: Request, tr: Translator, children: Sequence[dict[str, Any]]
) -> dict[str, str] | None:
    """What to tell the parent about the button they just pressed.

    The three actions redirect here with their outcome in the query string, and
    for a long time nothing read it: the page reloaded and said nothing, so a
    grant that never reached a stopped daemon was indistinguishable from one
    that worked. The sentence is assembled here rather than in the template
    because it needs the child's name and a translation, and templates are a bad
    place to keep either.
    """
    query = request.query_params

    failure = query.get("error", "")
    if failure:
        key = "error.daemon_unreachable" if failure == "daemon" else "error.save_failed"
        return {"tone": "danger", "text": tr(key)}

    def name_of(raw: str) -> str:
        try:
            wanted = int(raw)
        except ValueError:
            return ""
        for child in children:
            if int(child.get("child_id", 0)) == wanted:
                return str(child.get("display_name", ""))
        return ""

    minutes = query.get("minutes", "10")
    for flag, key in (
        ("granted", "today.grant.done"),
        ("locked", "today.lock.done"),
        ("unlocked", "today.unlock.done"),
        ("special", "special.done"),
        ("special_removed", "special.removed"),
    ):
        raw = query.get(flag, "")
        if raw:
            return {"tone": "ok", "text": tr(key, name=name_of(raw), minutes=minutes)}

    if query.get("poweroff"):
        return {"tone": "ok", "text": tr("today.poweroff.done")}
    if query.get("rebooting"):
        return {"tone": "ok", "text": tr("restart.done")}
    return None


async def _event_stream(ctx: PanelContext, request: Request) -> AsyncIterator[bytes]:
    """Relay daemon events as SSE, reconnecting quietly when the daemon restarts."""
    client = AsyncDaemonClient(ctx.config.paths.daemon_socket)
    while not await request.is_disconnected():
        try:
            async for event in client.stream_events():
                if await request.is_disconnected():
                    return
                payload = json.dumps(
                    {"child_id": event.child_id, **event.payload}, ensure_ascii=False
                )
                yield f"event: {event.kind}\ndata: {payload}\n\n".encode()
        except (IpcError, OSError) as exc:
            log.debug("event stream dropped: %s", exc)
            yield b'event: daemon_state\ndata: {"state":"unreachable"}\n\n'
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(3)
