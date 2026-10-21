"""The way in: the entry redirect, the PIN, signing out, and the recovery code.

With one parent the sign-in page is the PIN box it always was. With two or more
(D-218) it first asks "Kim giriş yapıyor?" with each parent's name as a big
button, then that parent's PIN: a plain GET form, so it works without
JavaScript, and the choice travels with the PIN as a hidden field.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form, Query, Response
from fastapi.responses import HTMLResponse

from biokids_core import repo
from biokids_panel import auth, parents
from biokids_panel.common import PanelView, View, _needs_setup, _set_session_cookie, see_other

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(view: View) -> Response:
    if _needs_setup(view.session):
        return see_other("/wizard/1")
    if not view.state.authenticated:
        return see_other("/login")
    return see_other("/today")


@router.get("/login", response_class=HTMLResponse)
def login_form(
    view: View,
    next: Annotated[str, Query()] = "/today",
    parent: Annotated[str, Query()] = "",
) -> Response:
    if _needs_setup(view.session):
        return see_other("/wizard/1")
    # Checked here too, not only on the POST. An already-signed-in parent is
    # redirected straight away, and the value is rendered into the form as a
    # hidden field - so an unchecked `next` here is the same open redirect
    # one step earlier, reached by following a link while signed in.
    target = auth.safe_next(next)
    if view.state.authenticated:
        return see_other(target)
    return _login_page(view, target, _chosen(parent))


@router.post("/login")
def login_submit(
    view: View,
    pin: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/today",
    parent: Annotated[str, Form()] = "",
) -> Response:
    result = auth.attempt_login(
        view.session,
        view.ctx.config,
        pin,
        parent_id=_chosen(parent),
        user_agent=view.request.headers.get("user-agent", ""),
    )
    if not result.ok:
        return _login_page(
            view,
            next,
            result.parent_id,
            status_code=401,
            error=view.tr(
                result.error_key,
                remaining=result.remaining_attempts,
                minutes=result.lockout_minutes,
            ),
            refused=result,
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
    # Straight to the PIN of the parent whose code it was: with two parents,
    # the sign-in page would otherwise ask who they are, which the code said.
    return see_other(f"/login?recovered=1&parent={result.parent_id}")


def _chosen(raw: str) -> int | None:
    """The parent a form or a link names. Anything but a number names nobody."""
    raw = raw.strip()
    return int(raw) if raw.isdigit() and len(raw) < 10 else None


def _login_page(
    view: PanelView,
    target: str,
    parent_id: int | None,
    *,
    status_code: int = 200,
    refused: auth.LoginResult | None = None,
    **values: Any,
) -> HTMLResponse:
    """The chooser, or one parent's PIN box.

    The PIN box shows that parent's own lockout, so a reload or a reopened app
    says why the form is dead (the lockout is theirs alone, D-218). With one
    parent there is no choice to make and no name on the page, as before.
    """
    session = view.session
    everyone = repo.list_parents(session)
    who = auth.who_signs_in(session, parent_id)
    shown = parents.shown(everyone, view.tr)
    choosing = len(everyone) > 1 and who is None
    locked = auth.lockout_remaining_minutes(who) if who is not None else 0
    if refused is not None and refused.lockout_minutes:
        locked = refused.lockout_minutes
    return view.render(
        "login.html",
        status_code=status_code,
        next=auth.safe_next(target),
        choosing=choosing,
        parents=shown,
        who=next((p for p in shown if who is not None and p.id == who.id), None),
        several=len(everyone) > 1,
        lockout_minutes=locked,
        **values,
    )
