"""D-189 — nothing is kept forever."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biokids_core.config import Config
from biokids_core.db import AccessRequest, AiAlert, AuditLog, Child, LedgerDay
from biokids_core.db.models import BootEpoch, UsageEvent, UsageHour, UsageTick, utcnow
from biokids_core.retention import BOOT_EPOCHS_KEPT, prune

NOW = dt.datetime(2026, 10, 7, 12, 0)


def count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_the_most_personal_records_go_first(
    session: Session, config: Config, child: Child
) -> None:
    """What a child asked the assistant and which sites they opened: a month."""
    old, recent = NOW - dt.timedelta(days=31), NOW - dt.timedelta(days=2)
    for when in (old, recent):
        session.add(AiAlert(child_id=child.id, ts=when, score=100, text="the child's words"))
        session.add(
            UsageEvent(
                child_id=child.id, ts_wall=when, ts_boot=0.0, kind="visit", payload_json="{}"
            )
        )
    session.commit()

    deleted = prune(session, config, NOW)
    session.commit()

    assert deleted["ai_alert"] == 1 and deleted["usage_event"] == 1
    assert count(session, AiAlert) == 1 and count(session, UsageEvent) == 1


def test_minutes_per_day_are_kept_for_the_history_screen(
    session: Session, config: Config, child: Child
) -> None:
    keep = config.retention.usage_days
    for days_ago in (keep + 1, keep - 1):
        day = (NOW - dt.timedelta(days=days_ago)).date()
        session.add(LedgerDay(child_id=child.id, day=day, general_s=600))
        session.add(
            UsageTick(child_id=child.id, day=day, category="games", app_slug="x", seconds=600)
        )
        # The hours of the day go with the day they belong to (D-206).
        session.add(UsageHour(child_id=child.id, day=day, hour=16, seconds=600))
    session.commit()

    deleted = prune(session, config, NOW)
    session.commit()

    assert count(session, LedgerDay) == 1 and count(session, UsageTick) == 1
    assert count(session, UsageHour) == 1 and deleted["usage_hour"] == 1


def test_the_audit_log_and_requests_have_windows_of_their_own(
    session: Session, config: Config, child: Child
) -> None:
    session.add(
        AuditLog(actor="daemon", action="daemon_started", ts=NOW - dt.timedelta(days=400))
    )
    session.add(
        AuditLog(actor="daemon", action="daemon_started", ts=NOW - dt.timedelta(days=10))
    )
    session.add(
        AccessRequest(
            child_id=child.id, url="", kind="time", created_at=NOW - dt.timedelta(days=100)
        )
    )
    session.commit()

    prune(session, config, NOW)
    session.commit()

    assert count(session, AuditLog) == 1
    assert count(session, AccessRequest) == 0


def test_the_windows_are_the_familys_to_change(
    session: Session, config: Config, child: Child
) -> None:
    longer = replace(config, retention=replace(config.retention, activity_days=90))
    session.add(AiAlert(child_id=child.id, ts=NOW - dt.timedelta(days=60), score=100))
    session.commit()
    prune(session, longer, NOW)
    assert count(session, AiAlert) == 1


def test_only_recent_boot_epochs_are_kept(session: Session, config: Config) -> None:
    for n in range(BOOT_EPOCHS_KEPT + 5):
        session.add(
            BootEpoch(boot_id=f"boot-{n}", last_seen_wall=utcnow() - dt.timedelta(days=n))
        )
    session.commit()
    prune(session, config, NOW)
    session.commit()
    assert count(session, BootEpoch) == BOOT_EPOCHS_KEPT
    assert session.scalar(select(BootEpoch).where(BootEpoch.boot_id == "boot-0")) is not None
