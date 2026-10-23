"""Queries, and the bridge from stored rows to the engine's value types.

The policy engine takes a frozen `ChildPolicy` and knows nothing about
SQLAlchemy; the panel writes rows and knows nothing about the engine. This
module is the one place the two meet, so a schema change has exactly one place
to break rather than being spread across four packages.

Everything here is a plain function over a `Session`. There is no repository
object, no unit-of-work abstraction and no caching: the daemon reloads policies
on a `reload` call and on nothing else, and the panel reads per request.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biokids_core import english, search_filter, special_days
from biokids_core.db.models import (
    AccessRequest,
    AiAlert,
    App,
    AuditLog,
    CategoryLimit,
    Child,
    ChildApp,
    EnglishItem,
    Family,
    Parent,
    Policy,
    ReadingItem,
    Setting,
    UsageEvent,
    utcnow,
)
from biokids_core.types import (
    AgeBand,
    Category,
    ChildPolicy,
    EndAction,
    FilterMode,
    TimeWindow,
)

DEFAULT_WARN_MINUTES: tuple[int, ...] = (10, 5, 1)

REQUEST_EXPIRY = dt.timedelta(hours=12)

SEARCH_ALERT_APP = "search"
"""`ai_alert.app` of a search the browser refused (D-222); the assistant's are
"assistant" (D-165)."""
"""A request nobody answered is gone by the next morning: "more time" asked for
yesterday is not a question about today. Here rather than in the daemon, which
marks such requests expired, because the panel's lists and the insights screen
(D-206) must already treat one that old as unanswered before the daemon gets to it."""


# ------------------------------------------------------------------- lookups


def get_family(session: Session) -> Family | None:
    return session.scalars(select(Family).order_by(Family.id)).first()


def get_or_create_family(session: Session) -> Family:
    family = get_family(session)
    if family is None:
        family = Family()
        session.add(family)
        session.flush()
    return family


def get_parent(session: Session) -> Parent | None:
    """The first parent who can still sign in: the one the wizard made, unless
    they were removed (D-218).

    Also where what the family shares is kept - the passphrase and the mail
    settings - so this is the row `mail` and the passphrase gate read. With one
    parent it is that parent, exactly as before there could be two.
    """
    return session.scalars(
        select(Parent).where(Parent.removed_at.is_(None)).order_by(Parent.id)
    ).first()


def list_parents(session: Session) -> list[Parent]:
    """Everyone who can sign in, in the order they were added."""
    return list(
        session.scalars(select(Parent).where(Parent.removed_at.is_(None)).order_by(Parent.id))
    )


def get_active_parent(session: Session, parent_id: int) -> Parent | None:
    parent = session.get(Parent, parent_id)
    return parent if parent is not None and parent.removed_at is None else None


def find_parent_by_name(session: Session, name: str) -> Parent | None:
    """The parent who can sign in with this name, compared the way people type
    it: case, and spaces around and between the words, do not count."""
    wanted = name_key(name)
    if not wanted:
        return None
    for parent in list_parents(session):
        if name_key(parent.display_name) == wanted:
            return parent
    return None


def name_key(name: str) -> str:
    """`"  anne "` and `"Anne"` are the same person to whoever is typing."""
    return " ".join(name.split()).casefold()


def parent_names(session: Session) -> dict[int, str]:
    """id -> name for every parent who ever existed, removed ones included,
    for the audit log: what a parent did stays theirs after they are gone."""
    return {parent.id: parent.display_name for parent in session.scalars(select(Parent))}


def family_passphrase_hash(session: Session) -> str:
    """The family's passphrase (D-48): one for the household, whoever is signed
    in (D-218). Empty when none was ever set."""
    holder = get_parent(session)
    return holder.passphrase_hash if holder is not None else ""


def setup_complete(session: Session) -> bool:
    family = get_family(session)
    return family is not None and family.setup_completed_at is not None


def list_children(session: Session, *, only_enabled: bool = False) -> Sequence[Child]:
    stmt = select(Child).order_by(Child.id)
    if only_enabled:
        stmt = stmt.where(Child.enabled.is_(True))
    return session.scalars(stmt).all()


def get_child(session: Session, child_id: int) -> Child | None:
    return session.get(Child, child_id)


def get_child_by_user(session: Session, unix_user: str) -> Child | None:
    return session.scalars(select(Child).where(Child.unix_user == unix_user)).first()


def children_by_unix_user(session: Session) -> dict[str, Child]:
    """The map the daemon's session watcher needs: logind knows users, not ids."""
    return {child.unix_user: child for child in list_children(session, only_enabled=True)}


# -------------------------------------------------------------- the policy


def load_policy(
    session: Session, child: Child, *, today: dt.date | None = None
) -> ChildPolicy | None:
    """Build the frozen policy the engine evaluates, or None if unmanaged.

    None means "BioKids does not manage this account": either the child was
    disabled in the panel, or no policy row was ever created. The engine treats
    that as allow-everything, which is the only safe reading — an unmanaged
    account must never be locked out by us.
    """
    if not child.enabled:
        return None
    row = session.get(Policy, child.id)
    if row is None or not row.enabled:
        return None

    today = today or dt.date.today()
    windows = row.windows

    return ChildPolicy(
        child_id=child.id,
        display_name=child.display_name,
        age_band=child.band(today),
        locale=child.locale,
        weekday_quota_min=row.weekday_quota_min,
        weekend_quota_min=row.weekend_quota_min,
        weekday_windows=_parse_windows(windows.get("weekday", [])),
        weekend_windows=_parse_windows(windows.get("weekend", [])),
        day_quota_min={
            day: entry["quota"] for day, entry in row.schedule.items() if "quota" in entry
        },
        day_windows={
            day: _parse_windows(entry["windows"])
            for day, entry in row.schedule.items()
            if "windows" in entry
        },
        special_days=special_days.load(session, child.id),
        end_action=_enum_or(EndAction, row.end_action, EndAction.POWEROFF),
        warn_minutes=_warn_minutes(session),
        lock_countdown_seconds=row.lock_countdown_seconds,
        break_enabled=row.break_enabled,
        break_work_min=row.break_work_min,
        break_rest_min=row.break_rest_min,
        earn_enabled=row.earn_enabled,
        earn_ratio=row.earn_ratio,
        earn_cap_min=row.earn_cap_min,
        earn_min_session_min=row.earn_min_session_min,
        earn_idle_pause_min=row.earn_idle_pause_min,
        earn_spendable_general=row.earn_spendable_general,
        category_limits_min=load_category_limits(session, child.id),
        finish_up_min=max(0, row.finish_up_min or 0),
        learn_first_min=max(0, row.learn_first_min or 0),
        voice_enabled=row.voice_enabled,
        filter_mode=_enum_or(FilterMode, row.filter_mode, FilterMode.ALLOWLIST),
        assistant_enabled=bool(row.assistant_enabled),
        assistant_voice=bool(row.assistant_voice),
        enabled=True,
    )


def load_policies(
    session: Session, *, today: dt.date | None = None
) -> dict[int, ChildPolicy | None]:
    today = today or dt.date.today()
    return {
        child.id: load_policy(session, child, today=today) for child in list_children(session)
    }


def load_category_limits(session: Session, child_id: int) -> dict[Category, int]:
    rows = session.scalars(
        select(CategoryLimit).where(CategoryLimit.child_id == child_id)
    ).all()
    limits: dict[Category, int] = {}
    for row in rows:
        try:
            limits[Category(row.category)] = row.minutes
        except ValueError:
            continue
    return limits


def get_or_create_policy(session: Session, child_id: int) -> Policy:
    row = session.get(Policy, child_id)
    if row is None:
        row = Policy(child_id=child_id)
        session.add(row)
        session.flush()
    return row


def _parse_windows(values: Sequence[str]) -> tuple[TimeWindow, ...]:
    """Skip anything malformed rather than failing to enforce at all.

    A window the panel could not have written means the row was hand-edited or
    migrated badly. Dropping it is strictly safer than raising: the child ends up
    with *fewer* allowed hours, never more, and the rest of the policy applies.
    """
    parsed: list[TimeWindow] = []
    for value in values:
        try:
            parsed.append(TimeWindow.parse(value))
        except ValueError:
            continue
    return tuple(parsed)


def _enum_or[T: str](enum_cls: type[T], value: str, fallback: T) -> T:
    try:
        return enum_cls(value)
    except ValueError:
        return fallback


def _warn_minutes(session: Session) -> tuple[int, ...]:
    raw = get_setting(session, "warn_minutes")
    if not raw:
        return DEFAULT_WARN_MINUTES
    try:
        values = sorted({int(x) for x in json.loads(raw)}, reverse=True)
    except (TypeError, ValueError):
        return DEFAULT_WARN_MINUTES
    return tuple(v for v in values if v > 0) or DEFAULT_WARN_MINUTES


# --------------------------------------------------------------- the catalog


def list_apps(session: Session) -> Sequence[App]:
    return session.scalars(select(App).order_by(App.slug)).all()


def list_child_apps(
    session: Session, child_id: int, *, only_enabled: bool = True
) -> list[tuple[ChildApp, App]]:
    """The tiles for one child, in the order the parent arranged them."""
    stmt = (
        select(ChildApp, App)
        .join(App, App.slug == ChildApp.app_slug)
        .where(ChildApp.child_id == child_id)
        .order_by(ChildApp.position, App.slug)
    )
    if only_enabled:
        stmt = stmt.where(ChildApp.enabled.is_(True))
    return [(link, app) for link, app in session.execute(stmt).all()]


def child_app(session: Session, child_id: int, app_slug: str) -> tuple[ChildApp, App] | None:
    """One of the child's enabled apps, with its catalog entry; None when they
    do not have it."""
    link = session.get(ChildApp, {"child_id": child_id, "app_slug": app_slug})
    if link is None or not link.enabled:
        return None
    app = session.get(App, app_slug)
    return None if app is None else (link, app)


def child_app_category(link: ChildApp, app: App) -> Category:
    raw = link.category_override or app.default_category
    try:
        return Category(raw)
    except ValueError:
        return Category.FREE


def app_is_allowed(session: Session, child_id: int, app_slug: str) -> bool:
    link = session.get(ChildApp, {"child_id": child_id, "app_slug": app_slug})
    return bool(link and link.enabled)


def suggested_apps_for_age(session: Session, age: int) -> list[App]:
    """What the wizard pre-selects. Age-appropriate, in catalog order (D-20)."""
    return [app for app in list_apps(session) if app.min_age <= age <= app.max_age]


def apps_saved_at(session: Session, child_id: int) -> dt.datetime | None:
    """When a parent last saved this child's apps screen, in naive UTC (D-207).

    Read from the audit log, which has a row for every save, changed or not.
    The upgrade's own hand-down (D-203, actor `system`) is not a parent
    answering, so only a parent's rows count.
    """
    return session.scalar(
        select(func.max(AuditLog.ts)).where(
            AuditLog.action == "apps_changed",
            AuditLog.target == f"child:{child_id}",
            AuditLog.actor.like("parent%"),
        )
    )


# --------------------------------------------------------------- settings


def get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if row is not None else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    session.flush()


def get_flag(session: Session, key: str, default: bool = False) -> bool:
    raw = get_setting(session, key, "1" if default else "0")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def set_flag(session: Session, key: str, value: bool) -> None:
    set_setting(session, key, "1" if value else "0")


def search_words(session: Session) -> list[str]:
    """The words the parent added to the search list, for every child (D-202).

    Only the text that is stored: `search_filter` decides again what each one
    becomes, so a row edited by hand cannot reach the browser as anything else.
    """
    raw = get_setting(session, search_filter.SETTING_KEY)
    try:
        stored = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(word) for word in stored if isinstance(word, str)][
        : search_filter.MAX_PARENT_TERMS
    ]


def set_search_words(session: Session, words: Sequence[str]) -> None:
    set_setting(session, search_filter.SETTING_KEY, json.dumps(list(words), ensure_ascii=False))


# ------------------------------------------------------- audit and events


def audit(
    session: Session,
    *,
    actor: str,
    action: str,
    target: str = "",
    detail: dict[str, object] | None = None,
    severity: str = "info",
) -> AuditLog:
    """Append to the audit log. Never truncated by the application (D-53)."""
    entry = AuditLog(
        actor=actor,
        action=action,
        target=target,
        detail_json=json.dumps(detail or {}, ensure_ascii=False, default=str),
        severity=severity,
    )
    session.add(entry)
    session.flush()
    return entry


def record_event(
    session: Session,
    *,
    kind: str,
    child_id: int | None = None,
    ts_boot: float = 0.0,
    payload: dict[str, object] | None = None,
) -> UsageEvent:
    entry = UsageEvent(
        child_id=child_id,
        kind=kind,
        ts_boot=ts_boot,
        payload_json=json.dumps(payload or {}, ensure_ascii=False, default=str),
    )
    session.add(entry)
    return entry


def recent_audit(
    session: Session,
    *,
    limit: int = 100,
    offset: int = 0,
    child_id: int | None = None,
    action: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
) -> Sequence[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.ts.desc(), AuditLog.id.desc())
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if child_id is not None:
        stmt = stmt.where(AuditLog.target == f"child:{child_id}")
    if since is not None:
        stmt = stmt.where(AuditLog.ts >= since)
    if until is not None:
        stmt = stmt.where(AuditLog.ts <= until)
    return session.scalars(stmt.limit(limit).offset(offset)).all()


def audit_actions(session: Session) -> list[str]:
    """The distinct actions present, for the audit view's filter dropdown."""
    return sorted(set(session.scalars(select(AuditLog.action).distinct())))


# ------------------------------------------------------------- age helpers


def default_quota_minutes(band: AgeBand) -> tuple[int, int]:
    """The wizard's pre-filled weekday/weekend quota: 1 h for 4-7, 2 h for 8-10."""
    return (60, 90) if band is AgeBand.YOUNG else (120, 180)


def default_filter_mode(band: AgeBand) -> FilterMode:
    """Open internet, harmful sites closed, at every age (D-167, the owner's call
    over D-61). "Selected sites only" is there for a parent who wants it."""
    del band
    return FilterMode.BLOCKLIST


def access_requests(
    session: Session, *, decided_limit: int = 20
) -> tuple[list[AccessRequest], list[AccessRequest]]:
    """(waiting, recently answered). Waiting ones older than `REQUEST_EXPIRY` are
    not shown: the daemon marks them expired on the next request, and "more
    time" from yesterday is not a question about today."""
    since = utcnow() - REQUEST_EXPIRY
    waiting = list(
        session.scalars(
            select(AccessRequest)
            .where(AccessRequest.status == "pending", AccessRequest.created_at >= since)
            .order_by(AccessRequest.created_at.desc())
        )
    )
    answered = list(
        session.scalars(
            select(AccessRequest)
            .where(AccessRequest.status.in_(("approved", "denied")))
            .order_by(AccessRequest.decided_at.desc())
            .limit(decided_limit)
        )
    )
    return waiting, answered


def assistant_alerts(session: Session, child_id: int, *, limit: int = 20) -> list[AiAlert]:
    """The assistant's refusals a parent has not yet marked as seen (D-165)."""
    return list(
        session.scalars(
            select(AiAlert)
            .where(
                AiAlert.child_id == child_id,
                AiAlert.app == "assistant",
                AiAlert.dismissed.is_(False),
            )
            .order_by(AiAlert.ts.desc())
            .limit(limit)
        ).all()
    )


def unseen_assistant_alerts(session: Session) -> dict[int, int]:
    """child id -> how many of the assistant's alerts are waiting, for Today."""
    counts: dict[int, int] = {}
    for alert in session.scalars(
        select(AiAlert).where(AiAlert.app == "assistant", AiAlert.dismissed.is_(False))
    ).all():
        counts[alert.child_id] = counts.get(alert.child_id, 0) + 1
    return counts


def unseen_search_alerts(session: Session) -> dict[int, int]:
    """child id -> refused searches the parent has not yet marked as seen (D-222)."""
    counts: dict[int, int] = {}
    for child_id in session.scalars(
        select(AiAlert.child_id).where(
            AiAlert.app == SEARCH_ALERT_APP, AiAlert.dismissed.is_(False)
        )
    ).all():
        counts[child_id] = counts.get(child_id, 0) + 1
    return counts


def search_alerts(
    session: Session, child_id: int, since: dt.datetime, until: dt.datetime
) -> list[AiAlert]:
    """A child's refused searches between two UTC times, newest first (D-222)."""
    return list(
        session.scalars(
            select(AiAlert)
            .where(
                AiAlert.child_id == child_id,
                AiAlert.app == SEARCH_ALERT_APP,
                AiAlert.ts >= since,
                AiAlert.ts < until,
            )
            .order_by(AiAlert.ts.desc())
        ).all()
    )


def dismiss_search_alerts(session: Session, child_id: int) -> int:
    """Mark a child's refused searches as seen; how many were not yet."""
    seen = 0
    for alert in session.scalars(
        select(AiAlert).where(
            AiAlert.child_id == child_id,
            AiAlert.app == SEARCH_ALERT_APP,
            AiAlert.dismissed.is_(False),
        )
    ).all():
        alert.dismissed = True
        seen += 1
    return seen


def english_progress(session: Session, child_id: int) -> dict[str, english.ItemState]:
    """A child's boxes in the English corner (D-225), by item."""
    return english.states(
        (row.item, row.box, row.due, row.first_day)
        for row in session.scalars(select(EnglishItem).where(EnglishItem.child_id == child_id))
    )


def reading_progress(session: Session, child_id: int) -> dict[str, english.ItemState]:
    """A child's boxes in the reading corner (D-241), by item."""
    return english.states(
        (row.item, row.box, row.due, row.first_day)
        for row in session.scalars(select(ReadingItem).where(ReadingItem.child_id == child_id))
    )


def record_reading_answer(
    session: Session, child_id: int, item: str, right: bool, today: dt.date
) -> english.ItemState | None:
    """One answer in a reading lesson: the item's box moves, as in English."""
    from biokids_core import reading

    if not reading.is_item(item):
        return None
    row = session.get(ReadingItem, (child_id, item))
    before = english.ItemState(row.box, row.due, row.first_day) if row is not None else None
    after = english.answered(before, right, today)
    assert after.due is not None and after.first_day is not None
    if row is None:
        row = ReadingItem(
            child_id=child_id,
            item=item,
            first_day=after.first_day,
            due=after.due,
            last_day=today,
        )
        session.add(row)
    row.box = after.box
    row.due = after.due
    row.last_day = today
    if right:
        row.right = (row.right or 0) + 1
    else:
        row.wrong = (row.wrong or 0) + 1
    session.flush()
    return after


def record_english_answer(
    session: Session, child_id: int, item: str, right: bool, today: dt.date
) -> english.ItemState | None:
    """One answer in a lesson: the item's box moves (`english.answered`).

    None for an id no lesson has: the launcher is the child's own process, and
    only the corner's own items are kept.
    """
    if not english.is_item(item):
        return None
    row = session.get(EnglishItem, (child_id, item))
    before = english.ItemState(row.box, row.due, row.first_day) if row is not None else None
    after = english.answered(before, right, today)
    assert after.due is not None and after.first_day is not None
    if row is None:
        row = EnglishItem(
            child_id=child_id,
            item=item,
            first_day=after.first_day,
            due=after.due,
            last_day=today,
        )
        session.add(row)
    row.box = after.box
    row.due = after.due
    row.last_day = today
    if right:
        row.right = (row.right or 0) + 1
    else:
        row.wrong = (row.wrong or 0) + 1
    session.flush()
    return after


def dismiss_alert(session: Session, alert_id: int) -> bool:
    alert = session.get(AiAlert, alert_id)
    if alert is None:
        return False
    alert.dismissed = True
    return True
