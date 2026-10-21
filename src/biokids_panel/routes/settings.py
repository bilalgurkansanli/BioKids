"""The settings: language, the parent's own PIN and appearance, the family's
passphrase, mail and phone access, and who the parents are."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Response
from fastapi.responses import HTMLResponse

from biokids_core import appearance, mail, repo, security
from biokids_panel import auth, parents
from biokids_panel.common import View, see_other

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_view(view: View) -> Response:
    if blocked := view.guard():
        return blocked
    from biokids_panel import phone

    session = view.session
    everyone = repo.list_parents(session)
    return view.render(
        "settings.html",
        family=repo.get_family(session),
        daemon_up=view.ctx.client.available(),
        mail_settings=mail.load_settings(session, view.ctx.config),
        text_sizes=appearance.TEXT_SIZES,
        parents=parents.shown(everyone, view.tr),
        several_parents=len(everyone) > 1,
        **phone.view(view.ctx, session),
    )


@router.post("/settings/appearance")
def settings_appearance(
    view: View,
    text_size: Annotated[str, Form()] = "",
    high_contrast: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Text size and contrast for the signed-in parent (D-217).

    On their own row, not in a cookie: chosen once at the computer, it is
    there on the phone too, and the other parent's panel is not changed.
    """
    if blocked := view.guard_form(csrf_token, "/settings?error=csrf#appearance"):
        return blocked
    parent = view.state.parent
    if parent is None:
        return see_other("/settings?error=csrf#appearance")
    before = appearance.Appearance.of(parent.text_size, parent.high_contrast)
    after = appearance.Appearance.of(text_size, high_contrast == "on")
    if after != before:
        parent.text_size = after.text_size
        parent.high_contrast = after.high_contrast
        repo.audit(
            view.session,
            actor=view.actor,
            action="appearance_changed",
            target=f"parent:{parent.id}",
            detail={"before": before.as_dict(), "after": after.as_dict()},
        )
        view.session.commit()
    return see_other("/settings?saved=1#appearance")


@router.post("/settings/mail")
def settings_mail(
    view: View,
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
    if blocked := view.guard_form(csrf_token, "/settings?error=csrf#mail"):
        return blocked
    session, config = view.session, view.ctx.config
    current = mail.load_settings(session, config)
    if action == "remove":
        mail.save_settings(session, config, None)
        repo.audit(session, actor=view.actor, action="mail_removed")
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
    mail.save_settings(session, config, settings)
    repo.audit(session, actor=view.actor, action="mail_saved")
    session.commit()
    if action == "test":
        try:
            mail.send(settings, view.tr("mail.test.subject"), view.tr("mail.test.body"))
        except mail.MailError as exc:
            log.warning("the test mail failed: %s", exc)
            return see_other("/settings?error=mail.failed#mail")
        return see_other("/settings?saved=mail_sent#mail")
    return see_other("/settings?saved=1#mail")


@router.get("/settings/phone", response_class=HTMLResponse)
def settings_phone(view: View) -> Response:
    """Phone access on a page of its own, which refreshes itself while
    Tailscale is installed and signed in (D-169)."""
    if blocked := view.guard():
        return blocked
    from biokids_panel import phone

    return view.render("phone.html", **phone.view(view.ctx, view.session))


@router.post("/settings/language")
def settings_language(
    view: View,
    lang: Annotated[str, Form()] = "tr",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, "/settings?error=csrf"):
        return blocked
    family = repo.get_or_create_family(view.session)
    family.locale = lang if lang in {"tr", "en"} else "tr"
    view.session.commit()
    response = see_other("/settings?saved=1")
    response.set_cookie("biokids_lang", family.locale, max_age=31_536_000, samesite="strict")
    return response


@router.post("/settings/pin")
def settings_pin(
    view: View,
    current_pin: Annotated[str, Form()] = "",
    new_pin: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard():
        return blocked
    parent = view.state.parent
    if parent is None or not view.csrf_ok(csrf_token):
        return see_other("/settings?error=csrf")
    if not security.verify_secret(parent.pin_hash, current_pin.strip()):
        return see_other("/settings?error=pin")
    try:
        chosen = security.validate_pin(new_pin)
    except ValueError as exc:
        return see_other(f"/settings?error={exc}")
    parent.pin_hash = security.hash_secret(chosen)
    repo.audit(view.session, actor=view.actor, action="pin_changed", severity="warning")
    auth.logout_everywhere(view.session, parent.id)
    view.session.commit()
    response = see_other("/login?pin_changed=1")
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return response


@router.post("/settings/passphrase")
def settings_passphrase(
    view: View,
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

    The passphrase is the family's (D-218): it is kept on the first parent,
    whoever changes it, and changing it needs the current one whoever is
    signed in. Read from the signed-in parent's own row, a second parent -
    whose row has none - would have been "setting the first one" and could
    have replaced it without knowing it.
    """
    if blocked := view.guard():
        return blocked
    parent = view.state.parent
    holder = repo.get_parent(view.session)
    if parent is None or holder is None or not view.csrf_ok(csrf_token):
        return see_other("/settings?error=csrf")
    if not security.verify_secret(parent.pin_hash, current_pin.strip()):
        return see_other("/settings?error=pin")
    # Changing an existing passphrase needs the old one; setting the first
    # one does not, because there is nothing to prove. A wrong one counts
    # towards the gate's limit like any other (D-171): this form was a place
    # to guess the passphrase with no limit at all, for anyone with a PIN.
    if holder.passphrase_hash and (
        auth.passphrase_locked(view.session)
        or not security.verify_secret(holder.passphrase_hash, current_passphrase.strip())
    ):
        repo.audit(
            view.session,
            actor=view.actor,
            action="passphrase_failed",
            detail={"via": "settings"},
            severity="warning",
        )
        view.session.commit()
        return see_other("/settings?error=passphrase")
    try:
        chosen = security.validate_passphrase(new_passphrase)
    except ValueError as exc:
        return see_other(f"/settings?error={exc}")

    holder.passphrase_hash = security.hash_secret(chosen)
    repo.audit(
        view.session,
        actor=view.actor,
        action="passphrase_changed",
        severity="warning",
    )
    view.session.commit()
    return see_other("/settings?saved=1")


@router.post("/settings/remote")
def settings_remote(
    view: View,
    enable: Annotated[str, Form()] = "on",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, "/settings?error=csrf"):
        return blocked
    from biokids_core import remote
    from biokids_panel import phone

    session = view.session
    if enable == "on":
        outcome = phone.turn_on(view.ctx, session, actor=view.actor)
        if outcome == "approval":
            return see_other("/settings/phone?error=remote_approval")
        if outcome == "error":
            return see_other("/settings/phone?error=remote")
        return see_other("/settings/phone?saved=1" if outcome == "on" else "/settings/phone")

    port = view.ctx.config.panel.port
    result = remote.disable(port)
    if not result.ok:
        # `RemoteResult.detail` is the only thing that says *why* tailscale
        # refused, and it was being dropped on the floor: the parent saw an
        # unrendered error slug in a URL and the journal said nothing.
        log.warning("remote access could not be disabled: %s (%s)", result.error, result.detail)
    repo.audit(
        session,
        actor=view.actor,
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
