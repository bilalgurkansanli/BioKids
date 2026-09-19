"""The setup wizard: ten steps, under five minutes, every one skippable (D-49).

Phase 0 implements steps 1–6 and 8. Steps 7, 9 and 10 — filtering, SMTP and the
AI layer — are present and say "coming soon" rather than being absent, because
the shape of the flow should be right from the start and a parent who meets nine
steps today and twelve next month has been told the product changed under them.

The wizard runs before a PIN exists, so it is unauthenticated by necessity. That
is safe only because the panel is bound to `127.0.0.1` and Tailscale is not
serving it until step 8 — which is exactly why step 8 comes after the PIN and
not before it. Once `family.setup_completed_at` is set the wizard refuses to run
again and redirects to the login page.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import re
from typing import Annotated, Any

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from biokids_core import repo, security
from biokids_core.catalog import enable_for_child, load_catalog, sync_catalog
from biokids_core.db.models import Child, Parent, utcnow
from biokids_core.i18n import Translator
from biokids_core.ipc import Op
from biokids_core.types import AgeBand, EndAction, FilterMode
from biokids_panel import auth
from biokids_panel.app import (
    Auth,
    Ctx,
    Db,
    Tr,
    _call_daemon,
    render,
    see_other,
)
from biokids_panel.context import PanelContext

log = logging.getLogger(__name__)

TOTAL_STEPS = 10
UNIX_USER_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")

SETTING_PENDING_CHILD = "wizard.child_id"
"""Which child the later steps are configuring. Kept in `setting` rather than a
cookie so that a parent who closes the tab and comes back does not start again."""


def register_wizard(app: FastAPI) -> None:
    """Ten steps, in order. Each POST validates, persists, and moves on."""

    @app.get("/wizard", response_class=HTMLResponse)
    def wizard_root() -> Response:
        return see_other("/wizard/1")

    @app.get("/wizard/{step}", response_class=HTMLResponse)
    def wizard_step(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        step: int,
    ) -> Response:
        if repo.setup_complete(session):
            return see_other("/login")
        step = max(1, min(step, TOTAL_STEPS + 1))
        return _render_step(ctx, request, session, state, tr, step)

    @app.post("/wizard/1")
    def step_language(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        lang: Annotated[str, Form()] = "tr",
    ) -> Response:
        family = repo.get_or_create_family(session)
        family.locale = lang if lang in {"tr", "en"} else "tr"
        session.commit()
        response = see_other("/wizard/2")
        response.set_cookie(
            "biokids_lang", family.locale, max_age=31_536_000, samesite="strict"
        )
        _ = (ctx, request, state, tr)
        return response

    @app.post("/wizard/2")
    def step_parent(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        name: Annotated[str, Form()] = "",
        pin: Annotated[str, Form()] = "",
        pin_again: Annotated[str, Form()] = "",
    ) -> Response:
        if pin.strip() != pin_again.strip():
            return _render_step(
                ctx, request, session, state, tr, 2, error=tr("wizard.parent.pin.mismatch")
            )
        try:
            chosen = security.validate_pin(pin)
        except ValueError as exc:
            return _render_step(ctx, request, session, state, tr, 2, error=tr(str(exc)))

        repo.get_or_create_family(session)
        parent = repo.get_parent(session)
        if parent is None:
            parent = Parent()
            session.add(parent)
        parent.display_name = name.strip()[:64]
        parent.pin_hash = security.hash_secret(chosen)
        parent.pin_failures = 0
        parent.pin_locked_until = None
        session.commit()
        return see_other("/wizard/3")

    @app.post("/wizard/3")
    def step_passphrase(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        passphrase: Annotated[str, Form()] = "",
        recovery: Annotated[str, Form()] = "",
        written_down: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            chosen = security.validate_passphrase(passphrase)
        except ValueError as exc:
            return _render_step(
                ctx, request, session, state, tr, 3, error=tr(str(exc)), recovery=recovery
            )
        if written_down != "on":
            return _render_step(
                ctx,
                request,
                session,
                state,
                tr,
                3,
                error=tr("wizard.recovery.not_confirmed"),
                recovery=recovery,
            )
        parent = repo.get_parent(session)
        if parent is None:
            return see_other("/wizard/2")

        parent.passphrase_hash = security.hash_secret(chosen)
        parent.recovery_hash = security.hash_secret(security.normalise_recovery_code(recovery))
        repo.audit(
            session,
            actor="system",
            action="setup_passphrase_set",
            detail={"note": "the recovery code was shown once and is not stored"},
        )
        session.commit()
        return see_other("/wizard/4")

    @app.post("/wizard/4")
    def step_child(
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
    ) -> Response:
        name = display_name.strip()[:64]
        user = (unix_user.strip() or _suggest_user(name)).lower()
        this_year = dt.date.today().year

        if not name or not UNIX_USER_RE.match(user):
            return _render_step(
                ctx, request, session, state, tr, 4, error=tr("child.unix_user.help")
            )
        if not (this_year - 14 <= birth_year <= this_year):
            return _render_step(
                ctx, request, session, state, tr, 4, error=tr("error.invalid_input")
            )

        existing = repo.get_child_by_user(session, user)
        child = existing or Child(unix_user=user, display_name=name, birth_year=birth_year)
        child.display_name = name
        child.birth_year = birth_year
        child.avatar = avatar
        child.locale = locale if locale in {"tr", "en"} else "tr"
        child.age_band = AgeBand.for_age(this_year - birth_year).value
        if existing is None:
            session.add(child)
        session.flush()

        policy = repo.get_or_create_policy(session, child.id)
        band = AgeBand(child.age_band)
        weekday, weekend = repo.default_quota_minutes(band)
        policy.weekday_quota_min = weekday
        policy.weekend_quota_min = weekend
        policy.filter_mode = repo.default_filter_mode(band).value

        repo.set_setting(session, SETTING_PENDING_CHILD, str(child.id))
        repo.audit(
            session,
            actor="system",
            action="child_added",
            target=f"child:{child.id}",
            detail={"name": name, "unix_user": user, "band": child.age_band},
        )
        session.commit()
        return see_other("/wizard/5")

    @app.post("/wizard/5")
    async def step_apps(request: Request, ctx: Ctx, session: Db) -> Response:
        """A repeated checkbox needs the raw multidict, so this one reads the form."""
        child = _pending_child(session)
        if child is None:
            return see_other("/wizard/4")
        form = await request.form()
        slugs = [str(value) for value in form.getlist("apps")]
        sync_catalog(session, load_catalog(config=ctx.config))
        enable_for_child(session, child.id, slugs)
        session.commit()
        return see_other("/wizard/6")

    @app.post("/wizard/6")
    def step_time(
        request: Request,
        ctx: Ctx,
        session: Db,
        state: Auth,
        tr: Tr,
        weekday_quota_min: Annotated[int, Form()] = 60,
        weekend_quota_min: Annotated[int, Form()] = 90,
        window: Annotated[str, Form()] = "",
        end_action: Annotated[str, Form()] = EndAction.POWEROFF.value,
    ) -> Response:
        child = _pending_child(session)
        if child is None:
            return see_other("/wizard/4")
        policy = repo.get_or_create_policy(session, child.id)
        policy.weekday_quota_min = max(0, min(weekday_quota_min, 24 * 60))
        policy.weekend_quota_min = max(0, min(weekend_quota_min, 24 * 60))
        try:
            from biokids_core.types import TimeWindow

            windows = [str(TimeWindow.parse(window))] if window.strip() else []
        except ValueError:
            return _render_step(
                ctx, request, session, state, tr, 6, error=tr("time.windows.invalid")
            )
        policy.windows = {"weekday": windows, "weekend": windows}
        try:
            policy.end_action = EndAction(end_action).value
        except ValueError:
            policy.end_action = EndAction.POWEROFF.value
        session.commit()
        return see_other("/wizard/7")

    @app.post("/wizard/7")
    def step_filter(session: Db, mode: Annotated[str, Form()] = "") -> Response:
        """A stub in Phase 0: the choice is recorded, the enforcement is Phase 1."""
        child = _pending_child(session)
        if child is not None and mode:
            policy = repo.get_or_create_policy(session, child.id)
            with contextlib.suppress(ValueError):
                policy.filter_mode = FilterMode(mode).value
            session.commit()
        return see_other("/wizard/8")

    @app.post("/wizard/8")
    def step_remote(
        ctx: Ctx, session: Db, state: Auth, enable: Annotated[str, Form()] = ""
    ) -> Response:
        from biokids_core import remote

        if enable == "on":
            result = remote.enable(ctx.config.panel.port)
            repo.audit(
                session,
                actor="system",
                action="remote_access_changed",
                detail={"enabled": True, "ok": result.ok},
            )
            session.commit()
        _ = state
        return see_other("/wizard/9")

    @app.post("/wizard/9")
    def step_email() -> Response:
        return see_other("/wizard/10")

    @app.post("/wizard/10")
    def step_extras(
        ctx: Ctx,
        session: Db,
        voices: Annotated[str, Form()] = "on",
    ) -> Response:
        child = _pending_child(session)
        if child is not None:
            policy = repo.get_or_create_policy(session, child.id)
            policy.voice_enabled = voices == "on"
            # The AI layer stays off. It is an optional package that is not even
            # installed in Phase 0, and D-82 says default off regardless.
            policy.ai_enabled = False

        family = repo.get_or_create_family(session)
        family.setup_completed_at = utcnow()
        repo.audit(
            session,
            actor="system",
            action="setup_completed",
            detail={"children": len(repo.list_children(session))},
        )
        session.commit()
        _call_daemon(ctx, Op.RELOAD)
        return see_other("/wizard/11")


# ------------------------------------------------------------------ rendering


def _render_step(
    ctx: PanelContext,
    request: Request,
    session: Session,
    state: auth.AuthState,
    tr: Translator,
    step: int,
    **extra: Any,
) -> HTMLResponse:
    values: dict[str, Any] = {
        "step": step,
        "total_steps": TOTAL_STEPS,
        "family": repo.get_family(session),
        "parent": repo.get_parent(session),
        "child": _pending_child(session),
        "this_year": dt.date.today().year,
    }

    if step == 3 and "recovery" not in extra:
        # Generated fresh each time the step is rendered and never stored in
        # plaintext. Only its hash survives the POST (D-50).
        values["recovery"] = security.generate_recovery_code()

    if step == 5:
        child = values["child"]
        age = child.age_on(dt.date.today()) if child else 6
        catalog = load_catalog(config=ctx.config)
        values["catalog"] = catalog.entries
        values["suggested"] = {e.slug for e in catalog.for_age(age)}
        values["installed"] = {e.slug for e in catalog.entries if e.available()}

    if step == 6:
        child = values["child"]
        band = AgeBand(child.age_band) if child else AgeBand.YOUNG
        weekday, weekend = repo.default_quota_minutes(band)
        policy = repo.get_or_create_policy(session, child.id) if child else None
        values["policy"] = policy
        values["default_weekday"] = policy.weekday_quota_min if policy else weekday
        values["default_weekend"] = policy.weekend_quota_min if policy else weekend
        values["end_actions"] = [e.value for e in EndAction]

    if step == 7:
        child = values["child"]
        band = AgeBand(child.age_band) if child else AgeBand.YOUNG
        values["suggested_mode"] = repo.default_filter_mode(band).value
        values["modes"] = [m.value for m in FilterMode]

    if step == 8:
        from biokids_core import remote

        state_now = remote.detect()
        values["tailscale"] = state_now
        values["panel_port"] = ctx.config.panel.port
        values["panel_url"] = state_now.url
        values["qr"] = remote.qr_svg(state_now.url) if state_now.url else ""

    values.update(extra)
    return render(ctx, request, session, state, tr, f"wizard/step{step}.html", **values)


# -------------------------------------------------------------------- helpers


def _pending_child(session: Session) -> Child | None:
    raw = repo.get_setting(session, SETTING_PENDING_CHILD)
    if raw.isdigit():
        return repo.get_child(session, int(raw))
    children = repo.list_children(session)
    return children[0] if children else None


def _suggest_user(display_name: str) -> str:
    """ "Elif Nur" -> "elif". Turkish letters folded, so the account name is typable."""
    folded = (
        display_name.lower()
        .replace("ı", "i")
        .replace("ş", "s")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ö", "o")
        .replace("ç", "c")
    )
    first = re.sub(r"[^a-z0-9]", "", folded.split(" ")[0] if folded else "")
    return first or "cocuk"
