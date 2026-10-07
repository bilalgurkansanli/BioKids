"""Everything set per child: who they are, their time, apps, internet, assistant."""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from biokids_core import repo
from biokids_core.catalog import enable_for_child, load_catalog, sync_catalog
from biokids_core.db.models import Child, Policy
from biokids_core.errors import IpcError
from biokids_core.ipc import Op
from biokids_core.types import EndAction, FilterMode
from biokids_panel import auth
from biokids_panel.common import PanelView, View, _call_daemon, _daemon_status, see_other
from biokids_panel.context import PanelContext
from biokids_panel.forms import (
    _one_of,
    _parse_window_list,
    _policy_snapshot,
    _schedule_from_form,
    _schedule_from_json,
    _windows_from_pickers,
)

log = logging.getLogger(__name__)

ALWAYS_ON_TILES = ("volume", "brightness", "shutdown")
"""Built-in tiles every child keeps whatever the apps screen says (D-164). The
shutdown tile is how a child ends their own session properly."""

router = APIRouter()


@router.get("/children", response_class=HTMLResponse)
def children_list(view: View) -> Response:
    if blocked := view.guard():
        return blocked
    return view.render(
        "children.html",
        children=repo.list_children(view.session),
        this_year=dt.date.today().year,
    )


@router.post("/children")
def children_add(
    view: View,
    display_name: Annotated[str, Form()] = "",
    birth_year: Annotated[int, Form()] = 0,
    unix_user: Annotated[str, Form()] = "",
    avatar: Annotated[str, Form()] = "fox",
    locale: Annotated[str, Form()] = "tr",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """The second child. The wizard adds the first; families have more."""
    if blocked := view.guard_form(csrf_token, "/children?error=csrf"):
        return blocked

    from biokids_panel.wizard import assign_default_apps, create_child, provision_child

    session = view.session
    child, error = create_child(
        session,
        display_name=display_name,
        birth_year=birth_year,
        unix_user=unix_user,
        avatar=avatar,
        locale=locale,
    )
    if child is None:
        return view.render(
            "children.html",
            status_code=400,
            children=repo.list_children(session),
            this_year=dt.date.today().year,
            error=view.tr(error),
        )
    repo.audit(
        session,
        actor=view.actor,
        action="child_added",
        target=f"child:{child.id}",
        detail={"unix_user": child.unix_user},
    )
    assign_default_apps(session, child, view.ctx.config)
    provisioned = provision_child(session, child)
    session.commit()
    _call_daemon(view.ctx, Op.RELOAD)
    if not provisioned.ok:
        # The row exists and the quota is set, but there is no account to
        # log in to. Saying "saved" here is how a parent finds out weeks
        # later that their child never had a profile on the greeter.
        return see_other(f"/children/{child.id}/time?error=provision")
    return see_other(f"/children/{child.id}/time?saved=1")


# ---------------------------------------------------------------------- time


@router.get("/children/{child_id}/time", response_class=HTMLResponse)
def time_form(view: View, child_id: int) -> Response:
    if blocked := view.guard():
        return blocked
    child = repo.get_child(view.session, child_id)
    if child is None:
        return view.not_found()
    return _time_page(view, child, repo.get_or_create_policy(view.session, child_id))


@router.post("/children/{child_id}/time")
def time_save(
    view: View,
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
    if blocked := view.guard():
        return blocked
    session, state = view.session, view.state
    child = repo.get_child(session, child_id)
    if child is None or not view.csrf_ok(csrf_token):
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
        return _time_page(
            view,
            child,
            repo.get_or_create_policy(session, child_id),
            status_code=400,
            error=view.tr("time.windows.invalid"),
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
        return _time_page(
            view,
            child,
            policy,
            status_code=403,
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
        actor=view.actor,
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
    return _applied(view, f"/children/{child_id}/time")


# ---------------------------------------------------------------- apps (D-164)


@router.get("/children/{child_id}/apps", response_class=HTMLResponse)
def apps_form(view: View, child_id: int) -> Response:
    """Which apps and sites a child sees. Until D-164 only the wizard asked."""
    if blocked := view.guard():
        return blocked
    child = repo.get_child(view.session, child_id)
    if child is None:
        return view.not_found()
    catalog = load_catalog(config=view.ctx.config)
    age = child.age_on(dt.date.today())
    entries = [e for e in catalog.entries if e.slug not in ALWAYS_ON_TILES]
    return view.render(
        "child_apps.html",
        child=child,
        apps=[e for e in entries if e.kind == "app"],
        sites=[e for e in entries if e.kind != "app"],
        enabled=set(_enabled_apps(view.session, child_id)),
        installed={e.slug for e in entries if e.available()},
        for_age={e.slug for e in catalog.for_age(age)},
    )


@router.post("/children/{child_id}/apps")
async def apps_save(view: View, child_id: int) -> Response:
    """A repeated checkbox needs the raw multidict, so this one reads the form."""
    if blocked := view.guard():
        return blocked
    form = await view.request.form()
    if not view.csrf_ok(str(form.get("csrf_token", ""))):
        return see_other(f"/children/{child_id}/apps?error=csrf")
    session = view.session
    if repo.get_child(session, child_id) is None:
        return see_other("/children?error=1")

    catalog = load_catalog(config=view.ctx.config)
    sync_catalog(session, catalog)
    # Only what can start here: a ticked app that is not installed would be
    # a tile that does nothing (D-164).
    known = {e.slug for e in catalog.entries if e.available()}
    chosen = [
        slug
        for slug in dict.fromkeys(str(value) for value in form.getlist("apps"))
        if slug in known and slug not in ALWAYS_ON_TILES
    ]
    before = _enabled_apps(session, child_id)
    # Volume, brightness and shutdown are not the parent's to take away:
    # the shutdown tile is how a child ends their own session properly.
    enable_for_child(
        session,
        child_id,
        chosen + [slug for slug in ALWAYS_ON_TILES if slug in known],
    )
    after = _enabled_apps(session, child_id)
    repo.audit(
        session,
        actor=view.actor,
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
    return _applied(view, f"/children/{child_id}/apps")


# ------------------------------------------------------------ internet (D-167)


@router.get("/children/{child_id}/internet", response_class=HTMLResponse)
def internet_form(view: View, child_id: int) -> Response:
    if blocked := view.guard():
        return blocked
    child = repo.get_child(view.session, child_id)
    if child is None:
        return view.not_found()
    status, _reachable = _daemon_status(view.ctx, view.session)
    return view.render(
        "child_internet.html",
        child=child,
        policy=repo.get_or_create_policy(view.session, child_id),
        modes=[FilterMode.BLOCKLIST.value, FilterMode.ALLOWLIST.value],
        filter=status.get("dns_filter") or {"running": False},
    )


@router.post("/children/{child_id}/internet")
def internet_save(
    view: View,
    child_id: int,
    mode: Annotated[str, Form()] = FilterMode.BLOCKLIST.value,
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, f"/children/{child_id}/internet?error=csrf"):
        return blocked
    session = view.session
    if repo.get_child(session, child_id) is None:
        return see_other("/children?error=1")
    policy = repo.get_or_create_policy(session, child_id)
    before = policy.filter_mode
    policy.filter_mode = _one_of(mode, FilterMode, FilterMode.BLOCKLIST).value
    repo.audit(
        session,
        actor=view.actor,
        action="filter_mode_changed",
        target=f"child:{child_id}",
        detail={"before": before, "after": policy.filter_mode},
    )
    session.commit()
    # The daemon rewrites the browser policy for the new mode.
    return _applied(view, f"/children/{child_id}/internet")


# ----------------------------------------------------------- assistant (D-165)


@router.get("/children/{child_id}/assistant", response_class=HTMLResponse)
def assistant_form(view: View, child_id: int) -> Response:
    if blocked := view.guard():
        return blocked
    child = repo.get_child(view.session, child_id)
    if child is None:
        return view.not_found()
    return view.render(
        "child_assistant.html",
        child=child,
        policy=repo.get_or_create_policy(view.session, child_id),
        status=_assistant_status(view.ctx),
        alerts=repo.assistant_alerts(view.session, child_id),
    )


@router.post("/children/{child_id}/assistant")
def assistant_save(
    view: View,
    child_id: int,
    assistant_enabled: Annotated[str, Form()] = "",
    assistant_voice: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard_form(csrf_token, f"/children/{child_id}/assistant?error=csrf"):
        return blocked
    session = view.session
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
        actor=view.actor,
        action="assistant_changed",
        target=f"child:{child_id}",
        detail={"before": before, "after": after},
    )
    session.commit()
    return _applied(view, f"/children/{child_id}/assistant")


@router.post("/children/{child_id}/assistant/download")
def assistant_download(
    view: View, child_id: int, csrf_token: Annotated[str, Form()] = ""
) -> Response:
    """The one-time model download (D-165), run by the daemon as its own unit."""
    if blocked := view.guard_form(csrf_token, f"/children/{child_id}/assistant?error=csrf"):
        return blocked
    repo.audit(
        view.session,
        actor=view.actor,
        action="assistant_download",
        target=f"child:{child_id}",
    )
    view.session.commit()
    if not _call_daemon(view.ctx, Op.ASSISTANT_SETUP):
        return see_other(f"/children/{child_id}/assistant?error=daemon")
    return see_other(f"/children/{child_id}/assistant?saved=download")


@router.post("/alerts/{alert_id}/dismiss")
def alert_dismiss(
    view: View,
    alert_id: int,
    next: Annotated[str, Form()] = "/today",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    target = auth.safe_next(next)
    if blocked := view.guard_form(csrf_token, f"{target}?error=csrf"):
        return blocked
    repo.dismiss_alert(view.session, alert_id)
    view.session.commit()
    return see_other(target)


# ------------------------------------------------------------------- helpers


def _time_page(
    view: PanelView, child: Child, policy: Policy, status_code: int = 200, **values: Any
) -> HTMLResponse:
    """The time screen, as the form, a refusal or the passphrase step draws it."""
    return view.render(
        "child_time.html",
        status_code=status_code,
        child=child,
        policy=policy,
        windows=policy.windows,
        schedule=policy.schedule,
        end_actions=[e.value for e in EndAction],
        **values,
    )


def _applied(view: PanelView, page: str) -> RedirectResponse:
    """Back to `page`, saying "saved" only once the daemon has reloaded.

    A policy the daemon never heard about is not a policy that has been
    applied, and "Kaydedildi" over a failed reload is the panel telling a
    parent their change took effect when it did not.
    """
    if not _call_daemon(view.ctx, Op.RELOAD):
        return see_other(f"{page}?error=daemon")
    return see_other(f"{page}?saved=1")


def _enabled_apps(session: Session, child_id: int) -> list[str]:
    """The slugs a child has switched on, sorted."""
    return sorted(
        link.app_slug
        for link, _app in repo.list_child_apps(session, child_id, only_enabled=True)
    )


def _assistant_status(ctx: PanelContext) -> dict[str, Any]:
    """Whether the assistant service runs and which models it has (D-165)."""
    try:
        response = ctx.client.call(Op.ASSISTANT_STATUS)
    except (IpcError, OSError) as exc:
        log.warning("the daemon did not answer assistant_status: %s", exc)
        return {"running": False}
    return dict(response.data) if response.ok else {"running": False}
