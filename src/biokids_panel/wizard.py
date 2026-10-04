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

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from biokids_core import repo, security
from biokids_core.catalog import enable_for_child, load_catalog, sync_catalog
from biokids_core.config import Config
from biokids_core.db.models import Child, Parent, utcnow
from biokids_core.i18n import Translator
from biokids_core.ipc import Op
from biokids_core.provision import (
    ProvisionResult,
    account_exists,
    account_is_administrative,
    is_child_account,
    provision,
    provision_via_systemd,
)
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


def wizard_is_open(session: Db) -> None:
    """Refuse every wizard request once the family exists.

    The GET checked this and the ten POSTs did not, which made
    `POST /wizard/2` an unauthenticated account takeover: it overwrites
    `pin_hash`, clears `pin_failures` and nulls `pin_locked_until`, so the
    parent's PIN stops working, the attacker's starts, and a lockout from a
    second earlier is gone. No cookie is involved, so `SameSite=Strict` bought
    nothing and any page the child's browser loaded could submit it.

    A dependency cannot return a redirect, so this raises one. Step 10 is safe
    under the gate because it is the handler that *sets* `setup_completed_at`,
    and the flag is still false when it is entered.
    """
    if repo.setup_complete(session):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


Open = Depends(wizard_is_open)


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
        step = max(1, min(step, TOTAL_STEPS + 1))
        if repo.setup_complete(session) and step <= TOTAL_STEPS:
            # The gate that stops a configured machine being re-set-up (D-128),
            # but it caught the confirmation screen too: step 10 redirects to
            # /wizard/11 and setup is complete by then, so the parent pressed
            # "Bitir" at the end of ten steps and landed on a bare PIN prompt
            # with nothing saying the setup had worked or the child been
            # created. Step 11 renders nothing a parent can change.
            return see_other("/login")
        return _render_step(ctx, request, session, state, tr, step)

    @app.post("/wizard/1", dependencies=[Open])
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

    @app.post("/wizard/2", dependencies=[Open])
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
        repo.audit(
            session,
            actor="system",
            action="pin_changed",
            detail={"via": "setup wizard"},
            severity="warning",
        )
        session.commit()
        return see_other("/wizard/3")

    @app.post("/wizard/3", dependencies=[Open])
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

    @app.post("/wizard/4", dependencies=[Open])
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
        child, error = create_child(
            session,
            display_name=display_name,
            birth_year=birth_year,
            unix_user=unix_user,
            avatar=avatar,
            locale=locale,
        )
        if child is None:
            return _render_step(ctx, request, session, state, tr, 4, error=tr(error))

        # Before step 5, so that "Şimdilik geç" there leaves a launcher with
        # tiles on it rather than an empty screen (D-49, D-156).
        assign_default_apps(session, child, ctx.config)
        provisioned = provision_child(session, child)
        repo.set_setting(session, SETTING_PENDING_CHILD, str(child.id))
        repo.audit(
            session,
            actor="system",
            action="child_added",
            target=f"child:{child.id}",
            detail={
                "name": child.display_name,
                "unix_user": child.unix_user,
                "band": child.age_band,
            },
        )
        session.commit()
        # Visible, not blocking. The children screen reports this (D-146) and
        # the wizard did not, which matters more here because the wizard is
        # where the first child is always added - a parent finished setup with
        # no Unix account for their child and found out at the greeter.
        #
        # But it must not be a dead end: on a machine where `adduser` is absent
        # the row is still right and `biokids-admin provision-child` still
        # repairs it, so stopping the setup here would strand a parent halfway
        # through with no way on (principle #4).
        if not provisioned.ok:
            return see_other("/wizard/5?error=provision")
        return see_other("/wizard/5")

    @app.post("/wizard/5", dependencies=[Open])
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

    @app.post("/wizard/6", dependencies=[Open])
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
        # Parsed before anything is written. The two quota lines used to run
        # first, and `get_db` commits on the way out, so a parent who typed
        # "16.00-20.00" - dots, which is how times are often written in Turkish
        # - got the error page with their quotas already saved and their
        # end-action choice silently reverted to the form default, poweroff.
        from biokids_core.types import TimeWindow

        try:
            windows = [str(TimeWindow.parse(window))] if window.strip() else []
        except ValueError:
            return _render_step(
                ctx, request, session, state, tr, 6, error=tr("time.windows.invalid")
            )

        policy = repo.get_or_create_policy(session, child.id)
        policy.weekday_quota_min = max(0, min(weekday_quota_min, 24 * 60))
        policy.weekend_quota_min = max(0, min(weekend_quota_min, 24 * 60))
        policy.windows = {"weekday": windows, "weekend": windows}
        try:
            policy.end_action = EndAction(end_action).value
        except ValueError:
            policy.end_action = EndAction.POWEROFF.value
        session.commit()
        return see_other("/wizard/7")

    @app.post("/wizard/7", dependencies=[Open])
    def step_filter(session: Db, mode: Annotated[str, Form()] = "") -> Response:
        """A stub in Phase 0: the choice is recorded, the enforcement is Phase 1."""
        child = _pending_child(session)
        if child is not None and mode:
            policy = repo.get_or_create_policy(session, child.id)
            with contextlib.suppress(ValueError):
                policy.filter_mode = FilterMode(mode).value
            session.commit()
        return see_other("/wizard/8")

    @app.post("/wizard/8", dependencies=[Open])
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
                detail={
                    "enabled": True,
                    "ok": result.ok,
                    "error": result.error,
                    "detail": result.detail[:300],
                },
                severity="info" if result.ok else "warning",
            )
            repo.set_setting(session, remote.SETTING_APPROVAL_URL, result.approval_url)
            session.commit()
            if result.approval_url:
                # Not a failure to report but a step to show: the page Tailscale
                # needs the parent to open once, on this same screen (D-157).
                return see_other("/wizard/8?error=remote_approval")
            if not result.ok:
                # The whole point of this step is "you can reach the panel from
                # your phone". Walking the parent on to step 9 after it failed
                # means they finish setup believing something that is not true,
                # and find out when they are not at the machine.
                log.warning("wizard: remote access could not be enabled: %s", result.detail)
                return see_other("/wizard/8?error=remote")
        _ = state
        return see_other("/wizard/9")

    @app.post("/wizard/9", dependencies=[Open])
    def step_email() -> Response:
        return see_other("/wizard/10")

    @app.post("/wizard/10", dependencies=[Open])
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

        # D-49 makes every step skippable, and a secret is the one thing that
        # cannot have a sane default. Skipping step 3 used to leave both
        # `passphrase_hash` and `recovery_hash` empty, which quietly removed the
        # gate on turning protection off *and* the only way back in after a
        # forgotten PIN. So this step, and only this step, insists.
        parent = repo.get_parent(session)
        if parent is None or not parent.passphrase_hash or not parent.recovery_hash:
            return see_other("/wizard/3?required=1")

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


def provision_child(session: Session, child: Child) -> ProvisionResult:
    """Give the new child an account to log in to, and say so in the log.

    Without this a parent adds a child, the screen fills with quotas and
    windows, and nothing on the machine changed: `unix_user` names an account
    that does not exist, so there is no avatar on the login screen and nothing
    to supervise. The failure is silent and looks exactly like success, which is
    the worst way for it to fail.

    Never fatal. The panel runs as root and normally succeeds, but a machine
    where `adduser` is absent, or where biokids-shell was not installed, must
    still finish adding the child - the parent can repair it afterwards with
    `biokids-admin provision-child`, and the audit entry says which it was.
    """
    # On a packaged machine the panel cannot do this itself: its sandbox makes
    # /etc read-only and /home an empty tmpfs (SECURITY.md section 2). The
    # privileged half lives in biokids-provision@.service. `None` means there is
    # no systemd to ask - a panel started by hand, or the test suite - and then
    # the work happens here, unsandboxed, exactly as it always did.
    result = provision_via_systemd(child.unix_user)
    if result is None:
        result = provision(child.unix_user, child.display_name)
    repo.audit(
        session,
        actor="system",
        action="child_provisioned",
        target=f"child:{child.id}",
        detail=result.as_detail(),
        severity="info" if result.ok else "warning",
    )
    if not result.ok:
        log.warning(
            "could not fully provision %s: %s",
            child.unix_user,
            "; ".join(result.problems),
        )
    return result


def assign_default_apps(session: Session, child: Child, config: Config) -> None:
    """The tiles a child starts with when nobody has chosen: right for their age,
    and installed on this machine.

    Only for a child with no tiles at all, so it never undoes a parent's choice.
    Without it, "Şimdilik geç" on the wizard's app step - a link straight to
    step 6 - and every child added from the children screen got a launcher
    with nothing on it, not even the volume or the shutdown tile (D-156).
    """
    if repo.list_child_apps(session, child.id, only_enabled=False):
        return
    catalog = load_catalog(config=config)
    sync_catalog(session, catalog)
    age = child.age_on(dt.date.today())
    enable_for_child(
        session,
        child.id,
        [entry.slug for entry in catalog.suggested_for_age(age, installed_only=True)],
    )


def create_child(
    session: Session,
    *,
    display_name: str,
    birth_year: int,
    unix_user: str = "",
    avatar: str = "fox",
    locale: str = "tr",
) -> tuple[Child | None, str]:
    """Create or update a child and give them an age-appropriate starting policy.

    Shared by the wizard's fourth step and by the panel's children screen, so
    that adding a second child in March produces exactly the same defaults as
    adding the first one in January. Returns the child, or None and an i18n key.
    """
    name = display_name.strip()[:64]
    typed = bool(unix_user.strip())
    user = (unix_user.strip() or _suggest_user(name)).lower()
    this_year = dt.date.today().year

    if not name or not UNIX_USER_RE.match(user):
        return None, "child.unix_user.help"
    if not (this_year - 14 <= birth_year <= this_year):
        return None, "error.invalid_input"

    existing = repo.get_child_by_user(session, user)
    if existing is not None and existing.display_name != name:
        # A different child already owns this account name.
        #
        # `_suggest_user` keeps only the first word, so "Mehmet Ali" and
        # "Mehmet Can" both fold to `mehmet`, and this function used to write
        # the second child's details straight over the first one's row: the
        # older brother vanished from the panel and the two shared one quota and
        # one ledger.
        if typed:
            # The parent chose this name. Quietly pointing it at somebody else's
            # account is not a correction we are entitled to make.
            return None, "child.unix_user.taken"
        user = _free_user(session, user)
        existing = None
    elif existing is None and not typed and not _free_on_this_machine(user):
        # The suggestion fell on somebody else's account: a child called Bilal
        # on a machine whose parent logs in as `bilal`. Suggest past it (D-155).
        user = _free_user(session, user)

    if account_is_administrative(user):
        # Typed, then. Made a child's, the parent's own account would get the
        # kiosk, the quota and the power-off (D-155).
        return None, "child.unix_user.admin"

    child = existing or Child(unix_user=user, display_name=name, birth_year=birth_year)
    child.display_name = name
    child.birth_year = birth_year
    child.avatar = avatar if avatar else "fox"
    child.locale = locale if locale in {"tr", "en"} else "tr"
    child.age_band = AgeBand.for_age(this_year - birth_year).value
    if existing is None:
        session.add(child)
    session.flush()

    band = AgeBand(child.age_band)
    weekday, weekend = repo.default_quota_minutes(band)
    policy = repo.get_or_create_policy(session, child.id)
    if existing is None:
        policy.weekday_quota_min = weekday
        policy.weekend_quota_min = weekend
        policy.filter_mode = repo.default_filter_mode(band).value
    return child, ""


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
        values["suggested"] = {e.slug for e in catalog.suggested_for_age(age)}
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
        # The open internet first: it is the default (D-167).
        values["modes"] = [FilterMode.BLOCKLIST.value, FilterMode.ALLOWLIST.value]

    if step == 8:
        from biokids_core import remote

        state_now = remote.detect()
        values["tailscale"] = state_now
        values["panel_port"] = ctx.config.panel.port
        values["panel_url"] = state_now.url
        values["qr"] = remote.qr_svg(state_now.url) if state_now.url else ""
        values["approval_url"] = remote.approval_url_in(
            repo.get_setting(session, remote.SETTING_APPROVAL_URL)
        )

    values.update(extra)
    return render(ctx, request, session, state, tr, f"wizard/step{step}.html", **values)


# -------------------------------------------------------------------- helpers


def _pending_child(session: Session) -> Child | None:
    raw = repo.get_setting(session, SETTING_PENDING_CHILD)
    if raw.isdigit():
        return repo.get_child(session, int(raw))
    children = repo.list_children(session)
    return children[0] if children else None


def _free_user(session: Session, wanted: str) -> str:
    """`mehmet`, then `mehmet2`, `mehmet3` - the first one nobody has.

    Only ever applied to a name BioKids suggested itself. A name the parent
    typed is refused on collision rather than adjusted, because a parent who
    types `mehmet` means *that* account. "Nobody has" covers the machine as well
    as the database: a suggestion must not land on the parent's own account.
    """

    def free(candidate: str) -> bool:
        return repo.get_child_by_user(session, candidate) is None and _free_on_this_machine(
            candidate
        )

    if free(wanted):
        return wanted
    for suffix in range(2, 100):
        candidate = f"{wanted}{suffix}"[:32]
        if free(candidate):
            return candidate
    # Ninety-eight children called Mehmet. The caller validates the name and
    # will refuse this one, which is a better outcome than overwriting a row.
    return f"{wanted}-taken"


def _free_on_this_machine(unix_user: str) -> bool:
    """No such account yet - or one BioKids already made a child's, which a
    reinstall after a purge finds again and should reuse, drawings and all."""
    return not account_exists(unix_user) or is_child_account(unix_user)


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
