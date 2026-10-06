"""The panel application: routes, dependencies and the two hard rules.

**It binds to 127.0.0.1 and nothing else.** There is no setting for a LAN
address, because the moment one exists someone forwards a port to it. Remote
access is `tailscale serve`, which publishes it inside the family's own tailnet
over real HTTPS with no open port at all (D-33).

**Every mutation that affects a running child goes through the daemon socket.**
The panel writes policy rows and then asks the daemon to reload; it never locks
a session or stops a process itself. That keeps one writer of live state and one
place where enforcement can go wrong.

Everything works without JavaScript: the forms are ordinary forms and every POST
ends in a redirect. The live counters on the Today screen are an enhancement
layered on top with `EventSource`, so a phone with a flaky tailnet degrades to a
page that is merely not live rather than a page that does not work.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import functools
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from biokids_core import mail, recorded_voice, repo, security
from biokids_core.catalog import enable_for_child, load_catalog, sync_catalog
from biokids_core.config import Config, get_config
from biokids_core.db.models import Policy
from biokids_core.errors import IpcError
from biokids_core.i18n import Translator
from biokids_core.ipc import AsyncDaemonClient, Op
from biokids_core.types import EndAction, FilterMode, TimeWindow
from biokids_panel import auth
from biokids_panel.context import (
    SHARED_STATIC_DIR,
    STATIC_DIR,
    PanelContext,
    base_context,
    request_locale,
    request_translator,
)

log = logging.getLogger(__name__)

MAX_GRANT_SECONDS = 4 * 3600
"""A sanity bound on "+10 dk". A typo in a form field should not hand a child
the rest of the week."""

ALWAYS_ON_TILES = ("volume", "brightness", "shutdown")
"""Built-in tiles every child keeps whatever the apps screen says (D-164). The
shutdown tile is how a child ends their own session properly."""


# --------------------------------------------------------------- dependencies


def get_ctx(request: Request) -> PanelContext:
    ctx: PanelContext = request.app.state.ctx
    return ctx


def get_db(request: Request) -> Iterator[Session]:
    ctx = get_ctx(request)
    with ctx.database.session() as session:
        yield session


Ctx = Annotated[PanelContext, Depends(get_ctx)]
Db = Annotated[Session, Depends(get_db)]


def current_auth(request: Request, session: Db, ctx: Ctx) -> auth.AuthState:
    """Resolve the cookie, and commit before the handler runs.

    Resolving touches `last_seen_at`, which is a write. SQLite allows one writer
    at a time, so leaving that transaction open for the rest of the request means
    the panel holds the write lock while it calls the daemon — and the daemon's
    first act on `get_status` is to write a `ledger_day` row. The two then wait
    for each other until `busy_timeout` gives up.

    Committing here costs one fsync per request and removes the window entirely.
    """
    token = request.cookies.get(auth.SESSION_COOKIE)
    state = auth.resolve(session, ctx.config, token)
    session.commit()
    return state


Auth = Annotated[auth.AuthState, Depends(current_auth)]


def current_tr(request: Request, session: Db) -> Translator:
    return request_translator(request, session)


Tr = Annotated[Translator, Depends(current_tr)]


# ------------------------------------------------------------------ rendering


def render(
    ctx: PanelContext,
    request: Request,
    session: Session,
    state: auth.AuthState,
    tr: Translator,
    template: str,
    status_code: int = 200,
    **values: Any,
) -> HTMLResponse:
    context = base_context(request, session, state, tr)
    context.update(values)
    html = ctx.env.get_template(template).render(**context)
    response = HTMLResponse(html, status_code=status_code)
    if state.authenticated:
        # Slide the cookie with the session it represents. Without this the
        # browser drops it thirty minutes after sign-in however busy the parent
        # has been, which is not the idle timeout D-51 describes.
        #
        # Re-sent from the request rather than from the session row: only the
        # hash is stored, which is the point - the raw token exists in the
        # parent's browser and nowhere else.
        token = request.cookies.get(auth.SESSION_COOKIE)
        if token:
            _set_session_cookie(response, token, ctx.config)
    return response


def see_other(url: str) -> RedirectResponse:
    """POST then redirect, always. A refresh must never repeat an action."""
    return RedirectResponse(url, status_code=303)


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


REQUEST_GRANT_CHOICES = (15, 30, 60)
"""What "more time" can be answered with, in minutes: three buttons, no typing."""


def _needs_setup(session: Session) -> bool:
    return not repo.setup_complete(session)


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
    ):
        raw = query.get(flag, "")
        if raw:
            return {"tone": "ok", "text": tr(key, name=name_of(raw), minutes=minutes)}

    if query.get("poweroff"):
        return {"tone": "ok", "text": tr("today.poweroff.done")}
    return None


def _guard(
    request: Request, session: Session, state: auth.AuthState
) -> RedirectResponse | None:
    """The one place "are you allowed to see this page?" is decided."""
    if _needs_setup(session):
        return see_other("/wizard/1")
    if not state.authenticated:
        nxt = quote(auth.safe_next(request.url.path), safe="/")
        return see_other(f"/login?next={nxt}")
    return None


# ------------------------------------------------------------------ the app


def create_app(config: Config | None = None) -> FastAPI:
    config = config or get_config()
    app = FastAPI(
        title="BioKids",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # The panel is a web page for one parent, not an API for anyone. There is
        # nothing to document and an interactive docs page would be one more
        # surface on a host a child shares.
    )
    app.state.ctx = PanelContext.build(config)
    app.mount("/static/shared", StaticFiles(directory=str(SHARED_STATIC_DIR)), name="shared")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next: Any) -> Response:
        """Static files are checked with the launcher before every use.

        A Debian package stamps every file with the same old modification time,
        so a browser guessing freshness from Last-Modified decided an upgraded
        stylesheet was the one it already had and kept the old one for days:
        after an upgrade the child saw yesterday's launcher. `no-cache` still
        lets the browser keep a copy; it only has to ask first, and the answer
        from a server on the same machine is a 304 in a millisecond.
        """
        response: Response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    register_routes(app)

    from biokids_panel.wizard import register_wizard

    register_wizard(app)
    return app


def register_routes(app: FastAPI) -> None:
    """One router, read top to bottom: entry, login, today, actions, settings."""
    # ------------------------------------------------------------- entry

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, session: Db, state: Auth) -> Response:
        if _needs_setup(session):
            return see_other("/wizard/1")
        if not state.authenticated:
            return see_other("/login")
        return see_other("/today")

    @app.get("/healthz")
    def healthz(ctx: Ctx) -> JSONResponse:
        """Liveness only. Deliberately says nothing about a family."""
        return JSONResponse({"ok": True, "daemon": ctx.client.available()})

    # -------------------------------------------------------------- login

    @app.get("/login", response_class=HTMLResponse)
    def login_form(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        next: Annotated[str, Query()] = "/today",
    ) -> Response:
        if _needs_setup(session):
            return see_other("/wizard/1")
        # Checked here too, not only on the POST. An already-signed-in parent is
        # redirected straight away, and the value is rendered into the form as a
        # hidden field - so an unchecked `next` here is the same open redirect
        # one step earlier, reached by following a link while signed in.
        target = auth.safe_next(next)
        if state.authenticated:
            return see_other(target)
        parent = repo.get_parent(session)
        locked = auth.lockout_remaining_minutes(parent) if parent else 0
        return render(
            ctx, request, session, state, tr, "login.html", next=target, lockout_minutes=locked
        )

    @app.post("/login")
    def login_submit(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        pin: Annotated[str, Form()] = "",
        next: Annotated[str, Form()] = "/today",
    ) -> Response:
        result = auth.attempt_login(
            session,
            ctx.config,
            pin,
            user_agent=request.headers.get("user-agent", ""),
        )
        if not result.ok:
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "login.html",
                status_code=401,
                next=next,
                error=tr(
                    result.error_key,
                    remaining=result.remaining_attempts,
                    minutes=result.lockout_minutes,
                ),
                lockout_minutes=result.lockout_minutes,
            )
        response = see_other(auth.safe_next(next))
        _set_session_cookie(response, result.token, ctx.config)
        return response

    @app.post("/logout")
    def logout(
        request: Request,
        session: Db,
        state: Auth,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        # A page elsewhere must not be able to sign the parent out mid-task.
        if state.session is not None and not auth.csrf_ok(state, csrf_token):
            return see_other("/today?error=csrf")
        auth.logout(session, request.cookies.get(auth.SESSION_COOKIE))
        response = see_other("/login")
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    @app.get("/recovery", response_class=HTMLResponse)
    def recovery_form(request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr) -> Response:
        return render(ctx, request, session, state, tr, "recovery.html")

    @app.post("/recovery")
    def recovery_submit(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        code: Annotated[str, Form()] = "",
        pin: Annotated[str, Form()] = "",
    ) -> Response:
        result = auth.attempt_recovery(session, code, pin)
        if not result.ok:
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "recovery.html",
                status_code=400,
                error=tr(result.error_key),
            )
        return see_other("/login?recovered=1")

    # -------------------------------------------------------------- today

    @app.get("/today", response_class=HTMLResponse)
    def today(request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        status, reachable = _daemon_status(ctx, session)
        children = status.get("children", [])
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "today.html",
            children=children,
            daemon_up=reachable,
            notice=_action_notice(request, tr, children),
            # Questions the assistant refused and the parent has not seen (D-165):
            # above the cards, like a pending request, because they are why a
            # parent might want to talk to their child today.
            assistant_alerts=[
                {"child": child, "count": count}
                for child_id, count in sorted(repo.unseen_assistant_alerts(session).items())
                if (child := repo.get_child(session, child_id)) is not None
            ],
        )

    @app.get("/today/cards", response_class=HTMLResponse)
    def today_cards(request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr) -> Response:
        """The card list on its own, for a client that polls instead of streaming."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        status, reachable = _daemon_status(ctx, session)
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "partials/child_cards.html",
            children=status.get("children", []),
            daemon_up=reachable,
        )

    @app.get("/events")
    async def events(request: Request, ctx: Ctx, session: Db, state: Auth) -> Response:
        """Server-sent events, relayed from the daemon (T4.4).

        The panel does not invent events; it forwards the daemon's. If the
        daemon is unreachable the stream still opens and says so, because a
        Today screen that silently stops updating is indistinguishable from a
        child who stopped using the computer.
        """
        if not state.authenticated:
            return Response(status_code=401)
        return StreamingResponse(
            _event_stream(ctx, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ----------------------------------------------------- parent actions

    @app.post("/children/{child_id}/grant")
    def grant(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        child_id: int,
        minutes: Annotated[int, Form()] = 10,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/today?error=csrf")
        minutes = max(1, min(minutes, MAX_GRANT_SECONDS // 60))
        ok = _call_daemon(
            ctx, Op.GRANT_TIME, child_id=child_id, seconds=minutes * 60, actor=_actor(state)
        )
        return see_other(
            f"/today?granted={child_id}&minutes={minutes}" if ok else "/today?error=daemon"
        )

    @app.post("/children/{child_id}/unlock")
    def unlock_now(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        child_id: int,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        """Back in, without adding time (D-173). "Kilitle" had no way back but "+10 dk"."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/today?error=csrf")
        ok = _call_daemon(ctx, Op.UNLOCK_NOW, child_id=child_id, actor=_actor(state))
        return see_other(f"/today?unlocked={child_id}" if ok else "/today?error=daemon")

    # ------------------------------------------------------------- voices

    @app.get("/voices", response_class=HTMLResponse)
    def voices_view(request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr) -> Response:
        """A parent records Zio and Sio's lines in their own voice (D-174)."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        directory = ctx.config.paths.recorded_voice_dir
        locale = tr.locale
        lines = []
        for event in recorded_voice.EVENTS:
            found = recorded_voice.lookup(directory, locale, event)
            lines.append(
                {
                    "event": event,
                    "recorded": found is not None,
                    "text": found.text
                    if found and found.text
                    else tr(f"voices.suggest.{event}"),
                    "seconds": round(found.seconds, 1) if found else 0,
                }
            )
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "voices.html",
            lines=lines,
            locale=locale,
            persona=recorded_voice.persona(directory),
        )

    @app.get("/voices/{locale}/{event}.wav")
    def voice_file(
        request: Request, ctx: Ctx, session: Db, state: Auth, locale: str, event: str
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        found = recorded_voice.lookup(ctx.config.paths.recorded_voice_dir, locale, event)
        if found is None:
            return Response(status_code=404)
        return Response(found.path.read_bytes(), media_type="audio/wav")

    @app.post("/voices/{event}")
    async def voice_upload(
        request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr, event: str, text: str = ""
    ) -> Response:
        """The recording itself: a WAV body, the CSRF token in a header."""
        if _guard(request, session, state) is not None:
            return JSONResponse({"ok": False, "error": tr("error.unknown")}, status_code=401)
        if not auth.csrf_ok(state, request.headers.get("x-csrf-token")):
            return JSONResponse({"ok": False, "error": tr("error.unknown")}, status_code=403)
        data = await request.body()
        try:
            recorded_voice.save(
                ctx.config.paths.recorded_voice_dir,
                tr.locale,
                event,
                data,
                text or tr(f"voices.suggest.{event}"),
            )
        except recorded_voice.RecordingError as exc:
            return JSONResponse({"ok": False, "error": tr(str(exc))}, status_code=400)
        except OSError:
            return JSONResponse(
                {"ok": False, "error": tr("error.save_failed")}, status_code=500
            )
        repo.audit(
            session, actor=_actor(state), action="voice_recorded", detail={"event": event}
        )
        session.commit()
        return JSONResponse({"ok": True})

    @app.post("/voices/{event}/delete")
    def voice_delete(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        event: str,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/voices?error=csrf")
        try:
            recorded_voice.delete(ctx.config.paths.recorded_voice_dir, tr.locale, event)
        except recorded_voice.RecordingError:
            return see_other("/voices?error=1")
        return see_other("/voices?saved=1")

    @app.post("/voice-persona")
    def voice_persona(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        persona: Annotated[str, Form()] = "zio",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/voices?error=csrf")
        try:
            recorded_voice.set_persona(ctx.config.paths.recorded_voice_dir, persona)
        except (recorded_voice.RecordingError, OSError):
            return see_other("/voices?error=1")
        return see_other("/voices?saved=1")

    # ----------------------------------------------------------- requests

    @app.get("/requests", response_class=HTMLResponse)
    def requests_view(request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr) -> Response:
        """What the children asked for, newest first, answerable in one tap (D-173)."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
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

        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "requests.html",
            pending=[describe(row) for row in pending],
            decided=[describe(row) for row in decided],
            grant_choices=REQUEST_GRANT_CHOICES,
        )

    @app.post("/requests/{request_id}/decide")
    def request_decide(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        request_id: int,
        approve: Annotated[str, Form()] = "",
        minutes: Annotated[int, Form()] = 0,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/requests?error=csrf")
        yes = approve == "yes"
        ok = _call_daemon(
            ctx,
            Op.DECIDE_REQUEST,
            request_id=request_id,
            approve=yes,
            minutes=minutes if yes else 0,
            actor=_actor(state),
        )
        return see_other("/requests?saved=1" if ok else "/requests?error=daemon")

    @app.post("/children/{child_id}/lock")
    def lock_now(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        child_id: int,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/today?error=csrf")
        ok = _call_daemon(ctx, Op.LOCK_NOW, child_id=child_id, actor=_actor(state))
        return see_other(f"/today?locked={child_id}" if ok else "/today?error=daemon")

    @app.post("/children/{child_id}/poweroff")
    def poweroff(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        child_id: int,
        csrf_token: Annotated[str, Form()] = "",
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/today?error=csrf")
        if confirm != "yes":
            # Reachable with JavaScript off, where the onsubmit confirm() never
            # runs and a mis-tap on "Kapat" - which sits beside "Kilitle" in the
            # same row of three - would otherwise power the machine off under a
            # child with no chance to cancel.
            return see_other(f"/today?confirm=poweroff&child={child_id}")
        ok = _call_daemon(ctx, Op.POWEROFF_NOW, actor=_actor(state))
        return see_other("/today?poweroff=1" if ok else "/today?error=daemon")

    # ------------------------------------------------------------ children

    @app.get("/children", response_class=HTMLResponse)
    def children_list(request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "children.html",
            children=repo.list_children(session),
            this_year=dt.date.today().year,
        )

    @app.post("/children")
    def children_add(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        display_name: Annotated[str, Form()] = "",
        birth_year: Annotated[int, Form()] = 0,
        unix_user: Annotated[str, Form()] = "",
        avatar: Annotated[str, Form()] = "fox",
        locale: Annotated[str, Form()] = "tr",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        """The second child. The wizard adds the first; families have more."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/children?error=csrf")

        from biokids_panel.wizard import assign_default_apps, create_child, provision_child

        child, error = create_child(
            session,
            display_name=display_name,
            birth_year=birth_year,
            unix_user=unix_user,
            avatar=avatar,
            locale=locale,
        )
        if child is None:
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "children.html",
                status_code=400,
                children=repo.list_children(session),
                this_year=dt.date.today().year,
                error=tr(error),
            )
        repo.audit(
            session,
            actor=_actor(state),
            action="child_added",
            target=f"child:{child.id}",
            detail={"unix_user": child.unix_user},
        )
        assign_default_apps(session, child, ctx.config)
        provisioned = provision_child(session, child)
        session.commit()
        _call_daemon(ctx, Op.RELOAD)
        if not provisioned.ok:
            # The row exists and the quota is set, but there is no account to
            # log in to. Saying "saved" here is how a parent finds out weeks
            # later that their child never had a profile on the greeter.
            return see_other(f"/children/{child.id}/time?error=provision")
        return see_other(f"/children/{child.id}/time?saved=1")

    @app.get("/children/{child_id}/time", response_class=HTMLResponse)
    def time_form(
        request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr, child_id: int
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        child = repo.get_child(session, child_id)
        if child is None:
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "error.html",
                status_code=404,
                message=tr("error.not_found"),
            )
        policy = repo.get_or_create_policy(session, child_id)
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "child_time.html",
            child=child,
            policy=policy,
            windows=policy.windows,
            schedule=policy.schedule,
            end_actions=[e.value for e in EndAction],
        )

    @app.post("/children/{child_id}/time")
    def time_save(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        child_id: int,
        weekday_quota_min: Annotated[int, Form()] = 60,
        weekend_quota_min: Annotated[int, Form()] = 120,
        weekday_windows: Annotated[str, Form()] = "",
        weekend_windows: Annotated[str, Form()] = "",
        weekday_from: Annotated[list[str] | None, Form()] = None,
        weekday_to: Annotated[list[str] | None, Form()] = None,
        weekend_from: Annotated[list[str] | None, Form()] = None,
        weekend_to: Annotated[list[str] | None, Form()] = None,
        day_custom: Annotated[list[str] | None, Form()] = None,
        day_quota: Annotated[list[str] | None, Form()] = None,
        day_from: Annotated[list[str] | None, Form()] = None,
        day_to: Annotated[list[str] | None, Form()] = None,
        schedule: Annotated[str, Form()] = "",
        end_action: Annotated[str, Form()] = EndAction.POWEROFF.value,
        enabled: Annotated[str, Form()] = "",
        break_enabled: Annotated[str, Form()] = "",
        break_work_min: Annotated[int, Form()] = 40,
        break_rest_min: Annotated[int, Form()] = 5,
        earn_enabled: Annotated[str, Form()] = "",
        earn_ratio: Annotated[float, Form()] = 1.0,
        earn_cap_min: Annotated[int, Form()] = 60,
        passphrase: Annotated[str, Form()] = "",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        child = repo.get_child(session, child_id)
        if child is None or not auth.csrf_ok(state, csrf_token):
            return see_other("/children?error=1")

        if passphrase:
            auth.confirm_passphrase(session, state, passphrase)

        try:
            # The time pickers when the page sent them (D-176); the old text
            # field otherwise, which is also what the passphrase step re-posts.
            if weekday_from is not None:
                weekday_windows = _windows_from_pickers(weekday_from, weekday_to or [])
            if weekend_from is not None:
                weekend_windows = _windows_from_pickers(weekend_from, weekend_to or [])
            windows = {
                "weekday": _parse_window_list(weekday_windows),
                "weekend": _parse_window_list(weekend_windows),
            }
            days = (
                _schedule_from_form(day_custom, day_quota or [], day_from or [], day_to or [])
                if day_custom is not None or day_quota is not None
                else _schedule_from_json(schedule)
            )
        except ValueError:
            policy = repo.get_or_create_policy(session, child_id)
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "child_time.html",
                status_code=400,
                child=child,
                policy=policy,
                windows=policy.windows,
                schedule=policy.schedule,
                end_actions=[e.value for e in EndAction],
                error=tr("time.windows.invalid"),
            )

        policy = repo.get_or_create_policy(session, child_id)
        before = _policy_snapshot(policy)

        # Turning a child's protection off is the one setting on this screen
        # that D-48 puts behind the passphrase: a PIN a ten-year-old watched
        # being typed must not be enough. Everything else here changes how much
        # time a child gets; this one stops BioKids managing them at all.
        turning_off = bool(before["enabled"]) and enabled != "on"
        # The other half of the same transition. It was audited as an ordinary
        # settings change, which buried it: a parent scanning the log for "when
        # did supervision come back" found a line that looks like a quota edit.
        turning_on = not before["enabled"] and enabled == "on"
        if turning_off and not state.passphrase_fresh():
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "child_time.html",
                status_code=403,
                child=child,
                policy=policy,
                windows=policy.windows,
                schedule=policy.schedule,
                end_actions=[e.value for e in EndAction],
                confirm_disable=True,
                submitted={
                    "weekday_quota_min": weekday_quota_min,
                    "weekend_quota_min": weekend_quota_min,
                    "weekday_windows": weekday_windows,
                    "weekend_windows": weekend_windows,
                    "schedule": json.dumps({str(k): v for k, v in days.items()}),
                    "end_action": end_action,
                    "break_enabled": break_enabled,
                    "break_work_min": break_work_min,
                    "break_rest_min": break_rest_min,
                    "earn_enabled": earn_enabled,
                    "earn_ratio": earn_ratio,
                    "earn_cap_min": earn_cap_min,
                },
                # An install that skipped the wizard's third step has no
                # passphrase. Ask for the PIN again rather than refusing
                # outright, and point at the settings screen: a gate with no
                # key is how a parent gets locked out of their own machine.
                no_passphrase=not (state.parent and state.parent.passphrase_hash),
            )

        policy.weekday_quota_min = max(0, min(weekday_quota_min, 24 * 60))
        policy.weekend_quota_min = max(0, min(weekend_quota_min, 24 * 60))
        policy.windows = windows
        policy.schedule = days
        policy.end_action = _one_of(end_action, EndAction, EndAction.POWEROFF).value
        policy.enabled = enabled == "on"
        policy.break_enabled = break_enabled == "on"
        policy.break_work_min = max(5, min(break_work_min, 240))
        policy.break_rest_min = max(1, min(break_rest_min, 60))
        policy.earn_enabled = earn_enabled == "on"
        policy.earn_ratio = max(0.0, min(earn_ratio, 5.0))
        policy.earn_cap_min = max(0, min(earn_cap_min, 480))

        after = _policy_snapshot(policy)
        repo.audit(
            session,
            actor=_actor(state),
            action=(
                "protection_disabled"
                if turning_off
                else "protection_enabled"
                if turning_on
                else "policy_changed"
            ),
            target=f"child:{child_id}",
            detail={"before": before, "after": after},
            severity="warning" if turning_off else "info",
        )
        session.commit()
        # A policy the daemon never heard about is not a policy that has been
        # applied, and "Kaydedildi" over a failed reload is the panel telling a
        # parent their change took effect when it did not.
        if not _call_daemon(ctx, Op.RELOAD):
            return see_other(f"/children/{child_id}/time?error=daemon")
        return see_other(f"/children/{child_id}/time?saved=1")

    # ---------------------------------------------------------- apps (D-164)

    @app.get("/children/{child_id}/apps", response_class=HTMLResponse)
    def apps_form(
        request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr, child_id: int
    ) -> Response:
        """Which apps and sites a child sees. Until D-164 only the wizard asked."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        child = repo.get_child(session, child_id)
        if child is None:
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "error.html",
                status_code=404,
                message=tr("error.not_found"),
            )
        catalog = load_catalog(config=ctx.config)
        age = child.age_on(dt.date.today())
        enabled = {
            link.app_slug
            for link, _app in repo.list_child_apps(session, child_id, only_enabled=True)
        }
        entries = [e for e in catalog.entries if e.slug not in ALWAYS_ON_TILES]
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "child_apps.html",
            child=child,
            apps=[e for e in entries if e.kind == "app"],
            sites=[e for e in entries if e.kind != "app"],
            enabled=enabled,
            installed={e.slug for e in entries if e.available()},
            for_age={e.slug for e in catalog.for_age(age)},
        )

    @app.post("/children/{child_id}/apps")
    async def apps_save(
        request: Request, ctx: Ctx, session: Db, state: Auth, child_id: int
    ) -> Response:
        """A repeated checkbox needs the raw multidict, so this one reads the form."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        form = await request.form()
        if not auth.csrf_ok(state, str(form.get("csrf_token", ""))):
            return see_other(f"/children/{child_id}/apps?error=csrf")
        child = repo.get_child(session, child_id)
        if child is None:
            return see_other("/children?error=1")

        catalog = load_catalog(config=ctx.config)
        sync_catalog(session, catalog)
        # Only what can start here: a ticked app that is not installed would be
        # a tile that does nothing (D-164).
        known = {e.slug for e in catalog.entries if e.available()}
        chosen = [
            slug
            for slug in dict.fromkeys(str(value) for value in form.getlist("apps"))
            if slug in known and slug not in ALWAYS_ON_TILES
        ]
        before = sorted(
            link.app_slug
            for link, _app in repo.list_child_apps(session, child_id, only_enabled=True)
        )
        # Volume, brightness and shutdown are not the parent's to take away:
        # the shutdown tile is how a child ends their own session properly.
        enable_for_child(
            session,
            child_id,
            chosen + [slug for slug in ALWAYS_ON_TILES if slug in known],
        )
        after = sorted(
            link.app_slug
            for link, _app in repo.list_child_apps(session, child_id, only_enabled=True)
        )
        repo.audit(
            session,
            actor=_actor(state),
            action="apps_changed",
            target=f"child:{child_id}",
            detail={
                "added": sorted(set(after) - set(before)),
                "removed": sorted(set(before) - set(after)),
            },
        )
        session.commit()
        # The daemon rewrites the browser policy from the enabled web tiles, so
        # a site just ticked opens only once it has heard about it.
        if not _call_daemon(ctx, Op.RELOAD):
            return see_other(f"/children/{child_id}/apps?error=daemon")
        return see_other(f"/children/{child_id}/apps?saved=1")

    # -------------------------------------------------------- internet (D-167)

    @app.get("/children/{child_id}/internet", response_class=HTMLResponse)
    def internet_form(
        request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr, child_id: int
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        child = repo.get_child(session, child_id)
        if child is None:
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "error.html",
                status_code=404,
                message=tr("error.not_found"),
            )
        status, _reachable = _daemon_status(ctx, session)
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "child_internet.html",
            child=child,
            policy=repo.get_or_create_policy(session, child_id),
            modes=[FilterMode.BLOCKLIST.value, FilterMode.ALLOWLIST.value],
            filter=status.get("dns_filter") or {"running": False},
        )

    @app.post("/children/{child_id}/internet")
    def internet_save(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        child_id: int,
        mode: Annotated[str, Form()] = FilterMode.BLOCKLIST.value,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other(f"/children/{child_id}/internet?error=csrf")
        if repo.get_child(session, child_id) is None:
            return see_other("/children?error=1")
        policy = repo.get_or_create_policy(session, child_id)
        before = policy.filter_mode
        policy.filter_mode = _one_of(mode, FilterMode, FilterMode.BLOCKLIST).value
        repo.audit(
            session,
            actor=_actor(state),
            action="filter_mode_changed",
            target=f"child:{child_id}",
            detail={"before": before, "after": policy.filter_mode},
        )
        session.commit()
        # The daemon rewrites the browser policy for the new mode.
        if not _call_daemon(ctx, Op.RELOAD):
            return see_other(f"/children/{child_id}/internet?error=daemon")
        return see_other(f"/children/{child_id}/internet?saved=1")

    # ------------------------------------------------------ assistant (D-165)

    @app.get("/children/{child_id}/assistant", response_class=HTMLResponse)
    def assistant_form(
        request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr, child_id: int
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        child = repo.get_child(session, child_id)
        if child is None:
            return render(
                ctx,
                request,
                session,
                state,
                tr,
                "error.html",
                status_code=404,
                message=tr("error.not_found"),
            )
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "child_assistant.html",
            child=child,
            policy=repo.get_or_create_policy(session, child_id),
            status=_assistant_status(ctx),
            alerts=repo.assistant_alerts(session, child_id),
        )

    @app.post("/children/{child_id}/assistant")
    def assistant_save(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        child_id: int,
        assistant_enabled: Annotated[str, Form()] = "",
        assistant_voice: Annotated[str, Form()] = "",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other(f"/children/{child_id}/assistant?error=csrf")
        if repo.get_child(session, child_id) is None:
            return see_other("/children?error=1")
        policy = repo.get_or_create_policy(session, child_id)
        before = {"enabled": policy.assistant_enabled, "voice": policy.assistant_voice}
        policy.assistant_enabled = assistant_enabled == "on"
        # The microphone only ever with the assistant itself (D-166).
        policy.assistant_voice = policy.assistant_enabled and assistant_voice == "on"
        after = {"enabled": policy.assistant_enabled, "voice": policy.assistant_voice}
        repo.audit(
            session,
            actor=_actor(state),
            action="assistant_changed",
            target=f"child:{child_id}",
            detail={"before": before, "after": after},
        )
        session.commit()
        if not _call_daemon(ctx, Op.RELOAD):
            return see_other(f"/children/{child_id}/assistant?error=daemon")
        return see_other(f"/children/{child_id}/assistant?saved=1")

    @app.post("/children/{child_id}/assistant/download")
    def assistant_download(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        child_id: int,
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        """The one-time model download (D-165), run by the daemon as its own unit."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other(f"/children/{child_id}/assistant?error=csrf")
        repo.audit(
            session,
            actor=_actor(state),
            action="assistant_download",
            target=f"child:{child_id}",
        )
        session.commit()
        if not _call_daemon(ctx, Op.ASSISTANT_SETUP):
            return see_other(f"/children/{child_id}/assistant?error=daemon")
        return see_other(f"/children/{child_id}/assistant?saved=download")

    @app.post("/alerts/{alert_id}/dismiss")
    def alert_dismiss(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        alert_id: int,
        next: Annotated[str, Form()] = "/today",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        target = auth.safe_next(next)
        if not auth.csrf_ok(state, csrf_token):
            return see_other(f"{target}?error=csrf")
        repo.dismiss_alert(session, alert_id)
        session.commit()
        return see_other(target)

    # ---------------------------------------------------------------- audit

    @app.get("/audit", response_class=HTMLResponse)
    def audit_view(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        child: Annotated[int | None, Query()] = None,
        action: Annotated[str | None, Query()] = None,
        day: Annotated[str | None, Query()] = None,
        page: Annotated[int, Query()] = 1,
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        per_page = 50
        since = until = None
        if day:
            with contextlib.suppress(ValueError):
                chosen = dt.date.fromisoformat(day)
                since, until = _utc_bounds_of_local_day(chosen)
        entries = repo.recent_audit(
            session,
            limit=per_page,
            offset=max(0, page - 1) * per_page,
            child_id=child,
            action=action or None,
            since=since,
            until=until,
        )
        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "audit.html",
            entries=entries,
            children=repo.list_children(session),
            actions=repo.audit_actions(session),
            filter_child=child,
            filter_action=action or "",
            filter_day=day or "",
            page=max(1, page),
            has_more=len(entries) == per_page,
        )

    # ------------------------------------------------------------ settings

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        from biokids_panel import phone

        return render(
            ctx,
            request,
            session,
            state,
            tr,
            "settings.html",
            family=repo.get_family(session),
            daemon_up=ctx.client.available(),
            mail_settings=mail.load_settings(session, ctx.config),
            **phone.view(ctx, session),
        )

    @app.post("/settings/mail")
    def settings_mail(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        action: Annotated[str, Form()] = "save",
        host: Annotated[str, Form()] = "",
        port: Annotated[int, Form()] = 587,
        security: Annotated[str, Form()] = "starttls",
        username: Annotated[str, Form()] = "",
        password: Annotated[str, Form()] = "",
        sender: Annotated[str, Form()] = "",
        recipients: Annotated[str, Form()] = "",
        alerts: Annotated[str, Form()] = "",
        weekly: Annotated[str, Form()] = "",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        """The parent's own mail server, for alerts and the weekly summary (D-177).

        The password field is never filled in on the page; left empty, the one
        already stored is kept.
        """
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/settings?error=csrf#mail")
        current = mail.load_settings(session, ctx.config)
        if action == "remove":
            mail.save_settings(session, ctx.config, None)
            repo.audit(session, actor=_actor(state), action="mail_removed")
            session.commit()
            return see_other("/settings?saved=1#mail")

        settings = mail.MailSettings(
            host=host.strip(),
            port=max(1, min(port, 65535)),
            security=security if security in mail.SECURITY_MODES else "starttls",
            username=username.strip(),
            password=password or (current.password if current else ""),
            sender=sender.strip(),
            recipients=mail.parse_recipients(recipients),
            alerts=alerts == "on",
            weekly=weekly == "on",
        )
        if not settings.usable:
            return see_other("/settings?error=mail.incomplete#mail")
        mail.save_settings(session, ctx.config, settings)
        repo.audit(session, actor=_actor(state), action="mail_saved")
        session.commit()
        if action == "test":
            try:
                mail.send(settings, tr("mail.test.subject"), tr("mail.test.body"))
            except mail.MailError as exc:
                log.warning("the test mail failed: %s", exc)
                return see_other("/settings?error=mail.failed#mail")
            return see_other("/settings?saved=mail_sent#mail")
        return see_other("/settings?saved=1#mail")

    @app.get("/settings/phone", response_class=HTMLResponse)
    def settings_phone(
        request: Request, ctx: Ctx, session: Db, state: Auth, tr: Tr
    ) -> Response:
        """Phone access on a page of its own, which refreshes itself while
        Tailscale is installed and signed in (D-169)."""
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        from biokids_panel import phone

        return render(
            ctx, request, session, state, tr, "phone.html", **phone.view(ctx, session)
        )

    @app.post("/settings/language")
    def settings_language(
        request: Request,
        session: Db,
        state: Auth,
        lang: Annotated[str, Form()] = "tr",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/settings?error=csrf")
        family = repo.get_or_create_family(session)
        family.locale = lang if lang in {"tr", "en"} else "tr"
        session.commit()
        response = see_other("/settings?saved=1")
        response.set_cookie(
            "biokids_lang", family.locale, max_age=31_536_000, samesite="strict"
        )
        return response

    @app.post("/settings/pin")
    def settings_pin(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        current_pin: Annotated[str, Form()] = "",
        new_pin: Annotated[str, Form()] = "",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        parent = state.parent
        if parent is None or not auth.csrf_ok(state, csrf_token):
            return see_other("/settings?error=csrf")
        if not security.verify_secret(parent.pin_hash, current_pin.strip()):
            return see_other("/settings?error=pin")
        try:
            chosen = security.validate_pin(new_pin)
        except ValueError as exc:
            return see_other(f"/settings?error={exc}")
        parent.pin_hash = security.hash_secret(chosen)
        repo.audit(session, actor=_actor(state), action="pin_changed", severity="warning")
        auth.logout_everywhere(session, parent.id)
        session.commit()
        _ = (ctx, tr)
        response = see_other("/login?pin_changed=1")
        response.delete_cookie(auth.SESSION_COOKIE, path="/")
        return response

    @app.post("/settings/passphrase")
    def settings_passphrase(
        request: Request,
        session: Db,
        state: Auth,
        current_pin: Annotated[str, Form()] = "",
        current_passphrase: Annotated[str, Form()] = "",
        new_passphrase: Annotated[str, Form()] = "",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        """Set or change the passphrase.

        This route is what makes the gate on turning protection off safe to
        add. An install that skipped the wizard's third step has no passphrase,
        and without somewhere to set one the gate would lock a parent out of
        their own settings with no way back — which is principle #4 exactly.
        """
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        parent = state.parent
        if parent is None or not auth.csrf_ok(state, csrf_token):
            return see_other("/settings?error=csrf")
        if not security.verify_secret(parent.pin_hash, current_pin.strip()):
            return see_other("/settings?error=pin")
        # Changing an existing passphrase needs the old one; setting the first
        # one does not, because there is nothing to prove.
        if parent.passphrase_hash and not security.verify_secret(
            parent.passphrase_hash, current_passphrase.strip()
        ):
            return see_other("/settings?error=passphrase")
        try:
            chosen = security.validate_passphrase(new_passphrase)
        except ValueError as exc:
            return see_other(f"/settings?error={exc}")

        parent.passphrase_hash = security.hash_secret(chosen)
        repo.audit(
            session,
            actor=_actor(state),
            action="passphrase_changed",
            severity="warning",
        )
        session.commit()
        return see_other("/settings?saved=1")

    @app.post("/settings/remote")
    def settings_remote(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        enable: Annotated[str, Form()] = "on",
        csrf_token: Annotated[str, Form()] = "",
    ) -> Response:
        blocked = _guard(request, session, state)
        if blocked:
            return blocked
        if not auth.csrf_ok(state, csrf_token):
            return see_other("/settings?error=csrf")
        from biokids_core import remote
        from biokids_panel import phone

        if enable == "on":
            outcome = phone.turn_on(ctx, session, actor=_actor(state))
            if outcome == "approval":
                return see_other("/settings/phone?error=remote_approval")
            if outcome == "error":
                return see_other("/settings/phone?error=remote")
            return see_other(
                "/settings/phone?saved=1" if outcome == "on" else "/settings/phone"
            )

        port = ctx.config.panel.port
        result = remote.disable(port)
        if not result.ok:
            # `RemoteResult.detail` is the only thing that says *why* tailscale
            # refused, and it was being dropped on the floor: the parent saw an
            # unrendered error slug in a URL and the journal said nothing.
            log.warning(
                "remote access could not be disabled: %s (%s)", result.error, result.detail
            )
        repo.audit(
            session,
            actor=_actor(state),
            action="remote_access_changed",
            detail={
                "enabled": False,
                "ok": result.ok,
                "error": result.error,
                "detail": result.detail[:300],
            },
            severity="info" if result.ok else "warning",
        )
        repo.set_setting(session, remote.SETTING_APPROVAL_URL, result.approval_url)
        session.commit()
        if result.ok:
            return see_other("/settings?saved=1")
        if result.approval_url:
            return see_other("/settings?error=remote_approval")
        return see_other("/settings?error=remote")

    # ---------------------------------------------------------------- PWA

    @app.get("/sw.js")
    def service_worker() -> Response:
        """Served from the root, not from /static.

        A service worker can only control paths at or below its own URL, so one
        at `/static/sw.js` would never see a navigation to `/today`.
        """
        # The cache name carries a fingerprint of the files it caches. With a
        # fixed name a phone kept the first stylesheet it ever saw, for good:
        # new screens arrived with no styles after every upgrade.
        script = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
        script = script.replace(SW_CACHE_NAME, f'"biokids-shell-{static_fingerprint()}"')
        return Response(
            script,
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
        )

    @app.get("/manifest.webmanifest")
    def manifest(session: Db, request: Request) -> JSONResponse:
        tr = request_translator(request, session)
        return JSONResponse(
            {
                "name": "BioKids",
                "short_name": "BioKids",
                "description": tr("panel.subtitle"),
                "start_url": "/today",
                "scope": "/",
                "display": "standalone",
                "orientation": "portrait",
                "background_color": "#FBF7F2",
                "theme_color": "#4B54C8",
                "lang": request_locale(request, session),
                "icons": [
                    {
                        "src": "/static/icons/icon-192.png",
                        "sizes": "192x192",
                        "type": "image/png",
                        "purpose": "any maskable",
                    },
                    {
                        "src": "/static/icons/icon-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "any maskable",
                    },
                ],
            },
            media_type="application/manifest+json",
        )


# ------------------------------------------------------------------- helpers


def _set_session_cookie(response: Response, token: str, config: Config) -> None:
    """Mint or refresh the session cookie.

    Refreshed on every authenticated request, not only at sign-in. D-51 says
    "30 minutes idle, then PIN again", and `auth.resolve` implements exactly
    that server-side by sliding `last_seen_at` - but the cookie was minted once
    with an absolute Max-Age, so a parent who signed in at 20:00 and spent half
    an hour setting quotas for two children was thrown back to the PIN screen at
    20:30 while actively using the panel. The server thought the session was
    alive; the browser had already dropped the cookie.
    """
    response.set_cookie(
        auth.SESSION_COOKIE,
        token,
        httponly=True,
        samesite="strict",
        path="/",
        max_age=config.panel.session_idle_minutes * 60,
        # `secure` is deliberately not set: over Tailscale the connection is
        # already HTTPS, and on localhost it is http, where a secure cookie would
        # simply never be sent and the parent could not log in at all.
    )


def _actor(state: auth.AuthState) -> str:
    return f"parent:{state.parent.id}" if state.parent else "parent"


def _daemon_status(ctx: PanelContext, session: Session) -> tuple[dict[str, Any], bool]:
    """Ask the daemon, and say plainly when it did not answer.

    A Today screen that renders stale numbers with no warning is worse than one
    that says the service is not responding: the first lets a parent believe
    their child is supervised when nobody is watching.

    But "not responding" must not be shown as "no children". The children are in
    the database, which the panel can read perfectly well; it is only their
    *live* state that is unknown. So the cards are still drawn, from the
    database, with the counters blank and the banner above them.
    """
    try:
        response = ctx.client.call(Op.GET_STATUS)
    except (IpcError, OSError) as exc:
        log.warning("the daemon did not answer get_status: %s", exc)
        return {"children": _children_from_database(session)}, False
    if not response.ok:
        return {"children": _children_from_database(session)}, False
    return response.data, True


def _children_from_database(session: Session) -> list[dict[str, Any]]:
    """The cards the panel can still draw with the daemon gone."""
    today = dt.date.today()
    rows: list[dict[str, Any]] = []
    for child in repo.list_children(session):
        policy = repo.load_policy(session, child, today=today)
        rows.append(
            {
                "child_id": child.id,
                "unix_user": child.unix_user,
                "display_name": child.display_name,
                "avatar": child.avatar,
                "age_band": child.band(today).value,
                "managed": policy is not None,
                "online": False,
                "unknown": True,
                "app": "",
                "remaining_seconds": 0,
                "remaining_minutes": 0,
                "used_seconds": 0,
                "quota_seconds": policy.quota_minutes(today) * 60 if policy else 0,
                "granted_seconds": 0,
                "allow": True,
                "reason_code": "",
                "end_action": policy.end_action.value if policy else "",
            }
        )
    return rows


SW_CACHE_NAME = '"biokids-shell-v1"'
"""The placeholder in sw.js that `service_worker` replaces."""


@functools.cache
def static_fingerprint() -> str:
    """Twelve hex digits that change whenever any file the panel serves does."""
    digest = hashlib.sha256()
    for directory in (STATIC_DIR, SHARED_STATIC_DIR):
        for path in sorted(Path(directory).rglob("*")):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _assistant_status(ctx: PanelContext) -> dict[str, Any]:
    """Whether the assistant service runs and which models it has (D-165)."""
    try:
        response = ctx.client.call(Op.ASSISTANT_STATUS)
    except (IpcError, OSError) as exc:
        log.warning("the daemon did not answer assistant_status: %s", exc)
        return {"running": False}
    return dict(response.data) if response.ok else {"running": False}


def _call_daemon(ctx: PanelContext, op: Op, **args: Any) -> bool:
    try:
        return ctx.client.call(op, **args).ok
    except (IpcError, OSError) as exc:
        log.warning("the daemon did not answer %s: %s", op, exc)
        return False


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


def _windows_from_pickers(starts: list[str], ends: list[str]) -> str:
    """Pairs of `<input type="time">` values as the text the old field took.

    A pair with only one side filled is a mistake worth saying so about, not a
    window to guess at.
    """
    ranges: list[str] = []
    for start, end in zip(starts, ends, strict=False):
        start, end = start.strip(), end.strip()
        if start and end:
            ranges.append(f"{start}-{end}")
        elif start or end:
            raise ValueError("half a window")
    return ", ".join(ranges)


def _schedule_from_form(
    custom: list[str] | None, quotas: list[str], starts: list[str], ends: list[str]
) -> dict[int, dict[str, Any]]:
    """The per-day overrides (D-176): only the days ticked as different."""
    ticked = {int(day) for day in (custom or []) if day.isdigit() and 0 <= int(day) <= 6}
    days: dict[int, dict[str, Any]] = {}
    for day in sorted(ticked):
        entry: dict[str, Any] = {}
        quota = quotas[day].strip() if day < len(quotas) else ""
        if quota:
            entry["quota"] = max(0, min(int(quota), 24 * 60))
        start = starts[day] if day < len(starts) else ""
        end = ends[day] if day < len(ends) else ""
        text = _windows_from_pickers([start], [end])
        if text:
            entry["windows"] = _parse_window_list(text)
        if entry:
            days[day] = entry
    return days


def _schedule_from_json(raw: str) -> dict[int, dict[str, Any]]:
    """The same overrides, as the passphrase step carries them back."""
    if not raw:
        return {}
    probe = Policy(schedule_json=raw)
    return probe.schedule


def _parse_window_list(raw: str) -> list[str]:
    """`"16:00-20:00, 08:00-09:00"` -> a validated list, or ValueError."""
    parts = [chunk.strip() for chunk in raw.replace("\n", ",").split(",") if chunk.strip()]
    return [str(TimeWindow.parse(part)) for part in parts]


def _one_of[T: str](value: str, enum_cls: type[T], fallback: T) -> T:
    try:
        return enum_cls(value)
    except ValueError:
        return fallback


def _policy_snapshot(policy: Policy) -> dict[str, Any]:
    """What goes in the audit log when a rule changes: the fields, not the row."""
    return {
        "weekday_quota_min": policy.weekday_quota_min,
        "weekend_quota_min": policy.weekend_quota_min,
        "windows": policy.windows,
        "end_action": policy.end_action,
        "enabled": policy.enabled,
        "break_enabled": policy.break_enabled,
        "earn_enabled": policy.earn_enabled,
    }


__all__ = ["create_app"]
