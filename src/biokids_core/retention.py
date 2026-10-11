"""Deleting what BioKids no longer needs to keep (D-189).

ARCHITECTURE.md section 5 promised retention from the start and nothing
deleted anything: every app a child opened, every question the assistant
refused - with the child's own words - and every line of the audit log stayed
for as long as the machine did. A parental-control product that keeps
everything forever is building the dataset principle 1 says should not exist.

The daemon runs `prune` once a day, a few minutes after it starts and then
every 24 hours (`Supervisor.housekeeping`).
The windows are in `[retention]` of config.toml:

* activity (usage events, which include the sites visited, and the assistant's
  alerts): `activity_days`, a month by default - the most personal records go
  first;
* minutes per day, per app and per hour of the day, for the history and
  insights screens: `usage_days`;
* the audit log: `audit_days`;
* children's requests: `requests_days`;
* parent sessions that have expired, and boot epochs from long ago, which no
  longer mean anything.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Final

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.dml import Delete

from biokids_core.config import Config
from biokids_core.db.models import (
    AccessRequest,
    AiAlert,
    AuditLog,
    BootEpoch,
    LedgerDay,
    PanelSession,
    UsageEvent,
    UsageHour,
    UsageTick,
    utcnow,
)

log = logging.getLogger(__name__)

BOOT_EPOCHS_KEPT: Final = 20
SESSION_GRACE: Final = dt.timedelta(days=1)


def _run(session: Session, statement: Delete) -> int:
    result = session.execute(statement)
    return int(result.rowcount or 0) if isinstance(result, CursorResult) else 0


def prune(session: Session, config: Config, now: dt.datetime | None = None) -> dict[str, int]:
    """Delete everything past its window. Returns rows deleted per table.

    Does not commit: the caller decides, so a failure leaves nothing half done.
    """
    now = now or utcnow()
    keep = config.retention
    activity = now - dt.timedelta(days=keep.activity_days)
    usage_day = (now - dt.timedelta(days=keep.usage_days)).date()

    deleted = {
        "usage_event": _run(session, delete(UsageEvent).where(UsageEvent.ts_wall < activity)),
        "ai_alert": _run(session, delete(AiAlert).where(AiAlert.ts < activity)),
        "usage_tick": _run(session, delete(UsageTick).where(UsageTick.day < usage_day)),
        "usage_hour": _run(session, delete(UsageHour).where(UsageHour.day < usage_day)),
        "ledger_day": _run(session, delete(LedgerDay).where(LedgerDay.day < usage_day)),
        "audit_log": _run(
            session,
            delete(AuditLog).where(AuditLog.ts < now - dt.timedelta(days=keep.audit_days)),
        ),
        "access_request": _run(
            session,
            delete(AccessRequest).where(
                AccessRequest.created_at < now - dt.timedelta(days=keep.requests_days)
            ),
        ),
        "panel_session": _run(
            session,
            delete(PanelSession).where(
                PanelSession.last_seen_at
                < now - dt.timedelta(minutes=config.panel.session_idle_minutes) - SESSION_GRACE
            ),
        ),
    }

    epochs = session.scalar(select(func.count()).select_from(BootEpoch)) or 0
    if epochs > BOOT_EPOCHS_KEPT:
        newest = (
            select(BootEpoch.id)
            .order_by(BootEpoch.last_seen_wall.desc())
            .limit(BOOT_EPOCHS_KEPT)
        )
        deleted["boot_epoch"] = _run(
            session, delete(BootEpoch).where(BootEpoch.id.not_in(newest))
        )

    removed = {table: count for table, count in deleted.items() if count}
    if removed:
        log.info("retention: deleted %s", ", ".join(f"{n} {t}" for t, n in removed.items()))
    return deleted
