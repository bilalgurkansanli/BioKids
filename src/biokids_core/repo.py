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

from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core.db.models import (
    App,
    AuditLog,
    CategoryLimit,
    Child,
    ChildApp,
    Family,
    Parent,
    Policy,
    Setting,
    UsageEvent,
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
    """v1 has exactly one parent account; the schema allows more for later."""
    return session.scalars(select(Parent).order_by(Parent.id)).first()


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
        filter_mode=_enum_or(FilterMode, row.filter_mode, FilterMode.ALLOWLIST),
        ai_enabled=row.ai_enabled,
        ai_block_enabled=row.ai_block_enabled,
        ai_timeout_min=row.ai_timeout_min,
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
    return FilterMode.ALLOWLIST if band is AgeBand.YOUNG else FilterMode.BLOCKLIST
