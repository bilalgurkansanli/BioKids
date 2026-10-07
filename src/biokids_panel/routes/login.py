"""The way in: the entry redirect, the PIN, signing out, and the recovery code."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Query, Response
from fastapi.responses import HTMLResponse

from biokids_core import repo
from biokids_panel import auth
from biokids_panel.common import View, _needs_setup, _set_session_cookie, see_other

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(view: View) -> Response:
    if _needs_setup(view.session):
        return see_other("/wizard/1")
    if not view.state.authenticated:
        return see_other("/login")
    return see_other("/today")


@router.get("/login", response_class=HTMLResponse)
def login_form(view: View, next: Annotated[str, Query()] = "/today") -> Response:
    if _needs_setup(view.session):
        return see_other("/wizard/1")
    # Checked here too, not only on the POST. An already-signed-in parent is
    # redirected straight away, and the value is rendered into the form as a
    # hidden field - so an unchecked `next` here is the same open redirect
    # one step earlier, reached by following a link while signed in.
    target = auth.safe_next(next)
    if view.state.authenticated:
        return see_other(target)
    parent = repo.get_parent(view.session)
    locked = auth.lockout_remaining_minutes(parent) if parent else 0
    return view.render("login.html", next=target, lockout_minutes=locked)


@router.post("/login")
def login_submit(
    view: View,
    pin: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/today",
) -> Response:
    result = auth.attempt_login(
        view.session,
        view.ctx.config,
        pin,
        user_agent=view.request.headers.get("user-agent", ""),
    )
    if not result.ok:
        return view.render(
            "login.html",
            status_code=401,
            next=next,
            error=view.tr(
                result.error_key,
                remaining=result.remaining_attempts,
                minutes=result.lockout_minutes,
            ),
            lockout_minutes=result.lockout_minutes,
        )
    response = see_other(auth.safe_next(next))
    _set_session_cookie(response, result.token, view.ctx.config)
    return response


@router.post("/logout")
def logout(view: View, csrf_token: Annotated[str, Form()] = "") -> Response:
    # A page elsewhere must not be able to sign the parent out mid-task.
    if view.state.session is not None and not view.csrf_ok(csrf_token):
        return see_other("/today?error=csrf")
    auth.logout(view.session, view.request.cookies.get(auth.SESSION_COOKIE))
    response = see_other("/login")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@router.get("/recovery", response_class=HTMLResponse)
def recovery_form(view: View) -> Response:
    return view.render("recovery.html")


@router.post("/recovery")
def recovery_submit(
    view: View,
    code: Annotated[str, Form()] = "",
    pin: Annotated[str, Form()] = "",
) -> Response:
    result = auth.attempt_recovery(view.session, code, pin)
    if not result.ok:
        return view.render("recovery.html", status_code=400, error=view.tr(result.error_key))
    return see_other("/login?recovered=1")
