"""Everything set per child: who they are, their time, apps, internet, assistant."""

from __future__ import annotations

import datetime as dt
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete
from sqlalchemy.orm import Session

from biokids_core import album, daily, offscreen, repo, search_filter
from biokids_core.appearance import TEXT_SIZES, Appearance, reading_font_installed
from biokids_core.catalog import (
    ALWAYS_ON_TILES,
    TILE_ICONS,
    Catalog,
    CatalogEntry,
    enable_for_child,
    load_catalog,
    new_apps,
    new_for_child,
    sync_catalog,
)
from biokids_core.db.models import CategoryLimit, Child, Policy, birthday_value
from biokids_core.errors import ConfigError, IpcError
from biokids_core.ipc import Op
from biokids_core.provision import unprovision, unprovision_via_systemd
from biokids_core.types import AgeBand, Category, EndAction, FilterMode
from biokids_panel import auth
from biokids_panel.common import PanelView, View, _call_daemon, _daemon_status, see_other
from biokids_panel.context import AVATARS, PanelContext
from biokids_panel.forms import (
    _minutes_or_zero,
    _one_of,
    _parse_window_list,
    _policy_snapshot,
    _schedule_from_form,
    _schedule_from_json,
    _windows_from_pickers,
)
from biokids_panel.routes import special_days

log = logging.getLogger(__name__)

FINISH_UP_CHOICES = (0, 2, 3, 5)
"""The last round's minutes a parent may pick (D-230); 0 is off."""

INDICATORS = ("badge", "small", "hidden")
"""How the launcher shows that the family can see the time and the sites (D-76).
Never covert: `hidden` keeps the first-run explanation."""

router = APIRouter()


@router.get("/children", response_class=HTMLResponse)
def children_list(view: View) -> Response:
    if blocked := view.guard():
        return blocked
    return _children_page(view)


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
        return _children_page(view, status_code=400, error=view.tr(error))
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


# ------------------------------------------------------- who they are (D-191)


@router.get("/children/{child_id}/edit", response_class=HTMLResponse)
def child_edit_form(view: View, child_id: int) -> Response:
    if blocked := view.guard():
        return blocked
    child = repo.get_child(view.session, child_id)
    if child is None:
        return view.not_found()
    return _edit_page(view, child)


@router.post("/children/{child_id}/edit")
def child_edit_save(
    view: View,
    child_id: int,
    display_name: Annotated[str, Form()] = "",
    birth_year: Annotated[int, Form()] = 0,
    avatar: Annotated[str, Form()] = "",
    locale: Annotated[str, Form()] = "",
    supervision_indicator: Annotated[str, Form()] = "",
    screen: Annotated[str, Form()] = "",
    text_size: Annotated[str, Form()] = "",
    high_contrast: Annotated[str, Form()] = "",
    reading_font: Annotated[str, Form()] = "",
    calm_screen: Annotated[str, Form()] = "",
    birthday_day: Annotated[str, Form()] = "",
    birthday_month: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    page = f"/children/{child_id}/edit"
    if blocked := view.guard_form(csrf_token, f"{page}?error=csrf"):
        return blocked
    session = view.session
    child = repo.get_child(session, child_id)
    if child is None:
        return see_other("/children?error=1")
    name = " ".join(display_name.split())[:64]
    this_year = dt.date.today().year
    if not name or not (this_year - 14 <= birth_year <= this_year):
        return _edit_page(view, child, status_code=400, error=view.tr("error.invalid_input"))

    before = _child_snapshot(child)
    child.display_name = name
    child.birth_year = birth_year
    child.age_band = AgeBand.for_age(this_year - birth_year).value
    if avatar in dict(AVATARS):
        child.avatar = avatar
    if locale in {"tr", "en"}:
        child.locale = locale
    if supervision_indicator in INDICATORS:
        child.supervision_indicator = supervision_indicator
    if screen:
        # The Ekran fieldset (D-217) was on the page that posted: its two
        # checkboxes say "off" by being absent, which a form without the
        # fieldset must not be taken to mean.
        chosen = Appearance.of(
            text_size, high_contrast == "on", reading_font == "on", calm_screen == "on"
        )
        child.text_size = chosen.text_size
        child.high_contrast = chosen.high_contrast
        child.reading_font = chosen.reading_font
        child.calm_screen = chosen.calm
    # The birthday (D-236): both or neither; a day that is not one is refused.
    if birthday_day.strip() or birthday_month.strip():
        try:
            given = birthday_value(int(birthday_month), int(birthday_day))
        except ValueError:
            given = None
        if given is None:
            return _edit_page(
                view, child, status_code=400, error=view.tr("child.birthday.invalid")
            )
        child.birthday = given
    else:
        child.birthday = None
    after = _child_snapshot(child)
    if after == before:
        return see_other(f"{page}?saved=1")
    repo.audit(
        session,
        actor=view.actor,
        action="child_changed",
        target=f"child:{child_id}",
        detail={"before": before, "after": after},
    )
    # The login screen shows the name and the picture (D-170).
    shown = True
    if (before["display_name"], before["avatar"]) != (after["display_name"], after["avatar"]):
        from biokids_panel.wizard import provision_child

        shown = provision_child(session, child).ok
    session.commit()
    if not shown:
        return see_other(f"{page}?error=provision")
    return _applied(view, page)


@router.post("/children/{child_id}/delete")
def child_delete(
    view: View,
    child_id: int,
    passphrase: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """Remove a child from BioKids (D-191): behind the passphrase, never while
    they are at the machine, and never half-way."""
    page = f"/children/{child_id}/edit"
    if blocked := view.guard_form(csrf_token, f"{page}?error=csrf"):
        return blocked
    session, state = view.session, view.state
    child = repo.get_child(session, child_id)
    if child is None:
        return see_other("/children?error=1")
    if passphrase:
        auth.confirm_passphrase(session, state, passphrase)
    if not state.passphrase_fresh():
        session.commit()
        return see_other(f"{page}?error=passphrase")

    # A child removed mid-session would carry on with nobody counting.
    status, reachable = _daemon_status(view.ctx, session)
    if not reachable:
        return see_other(f"{page}?error=daemon")
    if any(
        entry.get("child_id") == child_id and (entry.get("present") or entry.get("online"))
        for entry in status.get("children", [])
    ):
        return see_other(f"{page}?error=child.remove.logged_in")

    result = unprovision_via_systemd(child.unix_user)
    if result is None:
        result = unprovision(child.unix_user, view.ctx.config)
    if not result.ok:
        # Still in the login group with no row to supervise it would be an
        # account anyone could enter by picture, unsupervised. Keep the row.
        repo.audit(
            session,
            actor=view.actor,
            action="child_remove_failed",
            target=f"child:{child_id}",
            detail=result.as_detail(),
            severity="warning",
        )
        session.commit()
        log.warning("could not remove %s: %s", child.unix_user, "; ".join(result.problems))
        return see_other(f"{page}?error=child.remove.failed")

    repo.audit(
        session,
        actor=view.actor,
        action="child_removed",
        target=f"child:{child_id}",
        detail={
            "unix_user": child.unix_user,
            "display_name": child.display_name,
            "files": result.files_written,
        },
        severity="warning",
    )
    # Everything that belongs to the child goes with the row (ON DELETE CASCADE).
    session.expunge(child)
    session.execute(delete(Child).where(Child.id == child_id))
    session.commit()
    _call_daemon(view.ctx, Op.RELOAD)
    return see_other("/children?saved=removed")


# ------------------------------------------------------------ preview (D-239)


@router.get("/children/{child_id}/preview", response_class=HTMLResponse)
def preview(view: View, child_id: int) -> Response:
    """What the child's screen looks like with the rules as they are now.

    Drawn from the database - the tiles, today's minutes, the day's card, a
    warning and the end of the day - never from the child's session: a picture
    of the settings, not a view of the screen (CONTRIBUTING: no live mirroring).
    """
    if blocked := view.guard():
        return blocked
    session = view.session
    child = repo.get_child(session, child_id)
    if child is None:
        return view.not_found()
    today = dt.date.today()
    policy = repo.load_policy(session, child, today=today)
    learned = album.learning_seconds(session, child.id, since=today).get(today, 0)
    learn_first = policy.learn_first_min if policy else 0
    waiting = bool(learn_first) and learned < learn_first * 60
    tiles = [
        {
            "slug": app.slug,
            "name": app.name(view.tr.locale),
            "icon": TILE_ICONS.get(app.icon, TILE_ICONS["app"]),
            "waiting": waiting and repo.child_app_category(link, app) is Category.GAMES,
        }
        for link, app in repo.list_child_apps(session, child.id)
        if app.kind != "__gone__" and app.slug not in ALWAYS_ON_TILES
    ]
    band = child.band(today).value
    end_action = policy.end_action.value if policy else ""
    return view.render(
        "child_preview.html",
        child=child,
        band=band,
        tiles=tiles,
        learn_first=max(0, learn_first - learned // 60),
        quota_minutes=policy.quota_minutes(today) if policy else 0,
        daily=daily.card_for(
            today, child_id=child.id, band=band, locale=view.tr.locale, tiles=tiles
        ),
        warning_line=view.tr("quota.warning", minutes=5),
        finish_up=policy.finish_up_min if policy else 0,
        end_line=view.tr(
            f"shell.timeup.body.{end_action}"
            if end_action in ("poweroff", "lock", "logout")
            else "shell.timeup.body"
        ),
        offscreen=[
            c.as_dict(view.tr.locale) for c in offscreen.choose("end", band, today, child.id)
        ],
    )


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
    finish_up_min: Annotated[int, Form()] = 3,
    learn_first_min: Annotated[str, Form()] = "",
    limit_games: Annotated[str, Form()] = "",
    limit_learning: Annotated[str, Form()] = "",
    limit_web: Annotated[str, Form()] = "",
    limit_free: Annotated[str, Form()] = "",
    passphrase: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    if blocked := view.guard():
        return blocked
    session, state = view.session, view.state
    typed_limits = {
        Category.GAMES: limit_games,
        Category.LEARNING: limit_learning,
        Category.WEB: limit_web,
        Category.FREE: limit_free,
    }
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
        limits = _category_limits_from_form(typed_limits)
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
                "finish_up_min": finish_up_min,
                "learn_first_min": learn_first_min,
                **{f"limit_{cat.value}": typed for cat, typed in typed_limits.items()},
            },
            # An install that skipped the wizard's third step has no
            # passphrase. Ask for the PIN again rather than refusing
            # outright, and point at the settings screen: a gate with no
            # key is how a parent gets locked out of their own machine.
            no_passphrase=not repo.family_passphrase_hash(session),
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
    policy.finish_up_min = finish_up_min if finish_up_min in FINISH_UP_CHOICES else 3
    policy.learn_first_min = _minutes_or_zero(learn_first_min, 120)
    limits_before = _save_category_limits(session, child_id, limits)

    after = _policy_snapshot(policy)
    if limits_before != limits:
        before["category_limits"] = limits_before
        after["category_limits"] = limits
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
    today = dt.date.today()
    entries = [e for e in catalog.entries if e.slug not in ALWAYS_ON_TILES]
    new = new_apps(view.session)
    return view.render(
        "child_apps.html",
        child=child,
        apps=[e for e in entries if e.kind == "app"],
        sites=[e for e in entries if e.kind != "app"],
        enabled=set(_enabled_apps(view.session, child_id)),
        installed={e.slug for e in entries if e.available()},
        for_age={e.slug for e in catalog.for_age(child.age_on(today))},
        new=set(new),
        new_line=_new_apps_line(
            view, _new_for(view.session, catalog, new, child, today), tick=True
        ),
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
        search_words=repo.search_words(view.session),
        search_limit=search_filter.MAX_PARENT_TERMS,
    )


@router.post("/children/{child_id}/internet/words")
def search_words_save(
    view: View,
    child_id: int,
    words: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    """The words a parent adds to the search list, for every child (D-202).

    Saved from a child's Internet screen, where a parent looks for it, and
    said there to apply to the whole family: the browser and its policy are
    one per machine (D-167). A word that cannot be one is left out and the
    parent is told; the rest are kept.
    """
    page = f"/children/{child_id}/internet"
    if blocked := view.guard_form(csrf_token, f"{page}?error=csrf"):
        return blocked
    session = view.session
    if repo.get_child(session, child_id) is None:
        return see_other("/children?error=1")
    kept, refused = search_filter.parse_parent_terms(words)
    before = repo.search_words(session)
    if kept != before:
        repo.set_search_words(session, kept)
        repo.audit(
            session,
            actor=view.actor,
            action="search_words_changed",
            detail={
                "added": [word for word in kept if word not in before],
                "removed": [word for word in before if word not in kept],
            },
        )
        session.commit()
    # The launcher hears the words with the state, which the daemon re-reads
    # on a reload; the extension asks the launcher every five minutes.
    if not _call_daemon(view.ctx, Op.RELOAD):
        return see_other(f"{page}?error=daemon")
    if refused:
        return see_other(f"{page}?error=internet.words.refused")
    return see_other(f"{page}?saved=1")


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


def _edit_page(
    view: PanelView, child: Child, status_code: int = 200, **values: Any
) -> HTMLResponse:
    state = view.state
    return view.render(
        "child_edit.html",
        status_code=status_code,
        child=child,
        this_year=dt.date.today().year,
        indicators=INDICATORS,
        screen=Appearance.of(
            child.text_size, child.high_contrast, child.reading_font, child.calm_screen
        ),
        birthday=_birthday_parts(child.birthday),
        text_sizes=TEXT_SIZES,
        reading_font_installed=reading_font_installed(),
        passphrase_fresh=state.passphrase_fresh(),
        no_passphrase=not repo.family_passphrase_hash(view.session),
        **values,
    )


def _child_snapshot(child: Child) -> dict[str, Any]:
    return {
        "display_name": child.display_name,
        "birth_year": child.birth_year,
        "avatar": child.avatar,
        "locale": child.locale,
        "supervision_indicator": child.supervision_indicator,
        "text_size": child.text_size,
        "calm_screen": child.calm_screen,
        "birthday": child.birthday or "",
        "high_contrast": bool(child.high_contrast),
        "reading_font": bool(child.reading_font),
    }


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
        categories=[c.value for c in Category],
        category_limits={
            c.value: m for c, m in repo.load_category_limits(view.session, child.id).items()
        },
        # Today's and the coming special days (D-215), under the rules.
        specials=special_days.listed(view.session, child.id, view.tr, dt.date.today()),
        today=dt.date.today(),
        **values,
    )


def _category_limits_from_form(typed: dict[Category, str]) -> dict[str, int]:
    """Minutes per category from the form: empty is no limit of its own (D-44)."""
    limits: dict[str, int] = {}
    for category, value in typed.items():
        value = value.strip()
        if not value:
            continue
        limits[category.value] = max(0, min(int(value), 24 * 60))
    return limits


def _save_category_limits(
    session: Session, child_id: int, limits: dict[str, int]
) -> dict[str, int]:
    """Make the rows say `limits`; return what they said before."""
    before = {c.value: m for c, m in repo.load_category_limits(session, child_id).items()}
    for category in Category:
        row = session.get(CategoryLimit, {"child_id": child_id, "category": category.value})
        minutes = limits.get(category.value)
        if minutes is None:
            if row is not None:
                session.delete(row)
        elif row is None:
            session.add(
                CategoryLimit(child_id=child_id, category=category.value, minutes=minutes)
            )
        else:
            row.minutes = minutes
    return before


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


def _children_page(view: PanelView, status_code: int = 200, **values: Any) -> HTMLResponse:
    """The children screen, with a line under each child whose new tiles are
    worth a look (D-207)."""
    children = repo.list_children(view.session)
    return view.render(
        "children.html",
        status_code=status_code,
        children=children,
        this_year=dt.date.today().year,
        new_lines=_new_lines(view, children),
        **values,
    )


def _new_lines(view: PanelView, children: Sequence[Child]) -> dict[int, str]:
    """child id -> the sentence about their new tiles, for those who have one."""
    new = new_apps(view.session)
    if not new:
        return {}  # most of the year: nothing arrived this month, nothing to read
    try:
        catalog = load_catalog(config=view.ctx.config)
    except ConfigError as exc:
        # A hint is not worth the children screen, which draws without it.
        log.warning("new apps are not shown: %s", exc)
        return {}
    today = dt.date.today()
    lines = {
        child.id: _new_apps_line(view, _new_for(view.session, catalog, new, child, today))
        for child in children
    }
    return {child_id: line for child_id, line in lines.items() if line}


def _new_for(
    session: Session,
    catalog: Catalog,
    new: Mapping[str, dt.datetime],
    child: Child,
    today: dt.date,
) -> list[CatalogEntry]:
    """The new tiles worth a sentence to `child`'s parent (D-207)."""
    return new_for_child(
        catalog,
        new,
        age=child.age_on(today),
        enabled=set(_enabled_apps(session, child.id)),
        answered_at=repo.apps_saved_at(session, child.id),
    )


def _new_apps_line(
    view: PanelView, entries: Sequence[CatalogEntry], *, tick: bool = False
) -> str:
    """The sentence naming them in the panel's language, or "" for none.

    It says they are not on the child's screen yet: "a new app suits this
    child" alone reads as news, and a parent would think the child has it.
    `tick` adds how to give it them, for the apps screen, where the boxes
    are; on Çocuklar the sentence is a link to that screen instead.
    """
    if not entries:
        return ""
    turkish = view.tr.locale == "tr"
    names = ", ".join(entry.name_tr if turkish else entry.name_en for entry in entries)
    one = len(entries) == 1
    line = view.tr(
        "apps.new.line.one" if one else "apps.new.line.many", count=len(entries), names=names
    )
    if not tick:
        return line
    return f"{line} {view.tr('apps.new.tick.one' if one else 'apps.new.tick.many')}"


def _assistant_status(ctx: PanelContext) -> dict[str, Any]:
    """Whether the assistant service runs and which models it has (D-165)."""
    try:
        response = ctx.client.call(Op.ASSISTANT_STATUS)
    except (IpcError, OSError) as exc:
        log.warning("the daemon did not answer assistant_status: %s", exc)
        return {"running": False}
    return dict(response.data) if response.ok else {"running": False}


def _birthday_parts(value: str | None) -> dict[str, int] | None:
    """`MM-DD` as the two selects show it, or None."""
    if not value:
        return None
    try:
        month, day = (int(part) for part in value.split("-"))
    except ValueError:
        return None
    return {"month": month, "day": day}
