"""What every panel screen shares: the dependencies, rendering and the guard.

A screen's handler takes one `View` - the request, the panel context, the
database session and the signed-in parent - and opens with the same check:

    if blocked := view.guard():
        return blocked

or, for a form that changes something, the guard and the CSRF check together:

    if blocked := view.guard_form(csrf_token, "/today?error=csrf"):
        return blocked

Both return the redirect rather than raising it from a dependency: dependencies
run before FastAPI validates the path and the form, so raising there would turn
a malformed request's 422 into a trip to the login page.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from biokids_core import repo
from biokids_core.config import Config
from biokids_core.errors import IpcError
from biokids_core.i18n import Translator
from biokids_core.ipc import Op
from biokids_panel import auth
from biokids_panel.context import PanelContext, base_context, request_translator

log = logging.getLogger(__name__)


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


@dataclass
class PanelView:
    """One request to a screen, and what its handler reaches for."""

    request: Request
    ctx: PanelContext
    session: Session
    state: auth.AuthState

    @functools.cached_property
    def tr(self) -> Translator:
        """Looked up on first use: most actions redirect without saying a word."""
        return request_translator(self.request, self.session)

    @property
    def actor(self) -> str:
        return _actor(self.state)

    def guard(self) -> RedirectResponse | None:
        return _guard(self.request, self.session, self.state)

    def guard_form(self, csrf_token: str, stale: str) -> RedirectResponse | None:
        """`guard`, then the CSRF check; a form from elsewhere goes to `stale`."""
        blocked = self.guard()
        if blocked is None and not self.csrf_ok(csrf_token):
            return see_other(stale)
        return blocked

    def csrf_ok(self, presented: str | None) -> bool:
        return auth.csrf_ok(self.state, presented)

    def render(self, template: str, status_code: int = 200, **values: Any) -> HTMLResponse:
        return render(
            self.ctx,
            self.request,
            self.session,
            self.state,
            self.tr,
            template,
            status_code,
            **values,
        )

    def not_found(self) -> HTMLResponse:
        return self.render("error.html", status_code=404, message=self.tr("error.not_found"))


def get_view(request: Request, ctx: Ctx, session: Db, state: Auth) -> PanelView:
    return PanelView(request=request, ctx=ctx, session=session, state=state)


View = Annotated[PanelView, Depends(get_view)]


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


# ---------------------------------------------------------------- the guard


def _needs_setup(session: Session) -> bool:
    return not repo.setup_complete(session)


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


def _actor(state: auth.AuthState) -> str:
    return f"parent:{state.parent.id}" if state.parent else "parent"


# ----------------------------------------------------------------- the daemon


def _call_daemon(ctx: PanelContext, op: Op, **args: Any) -> bool:
    try:
        return ctx.client.call(op, **args).ok
    except (IpcError, OSError) as exc:
        log.warning("the daemon did not answer %s: %s", op, exc)
        return False


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
