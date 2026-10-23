"""Small builders shared by the test modules.

Kept out of `conftest.py` because these are plain functions, not fixtures, and
pytest puts the test directory on `sys.path` so a flat import works.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.orm import Session

from biokids_core.db.models import App, ChildApp
from biokids_core.types import AgeBand, Category, ChildPolicy, EndAction, LedgerState

MONDAY = dt.date(2026, 9, 21)
SATURDAY = dt.date(2026, 9, 19)
AFTERNOON = dt.datetime(2026, 9, 21, 17, 0, 0)


def make_policy(**overrides: Any) -> ChildPolicy:
    """A plain one-hour-a-day policy, with whatever the test wants changed."""
    base: dict[str, Any] = {
        "child_id": 1,
        "display_name": "Elif",
        "age_band": AgeBand.YOUNG,
        "weekday_quota_min": 60,
        "weekend_quota_min": 60,
        "end_action": EndAction.POWEROFF,
    }
    base.update(overrides)
    return ChildPolicy(**base)


def make_state(**overrides: Any) -> LedgerState:
    base: dict[str, Any] = {"child_id": 1, "day": MONDAY}
    base.update(overrides)
    return LedgerState(**base)


def give_app(
    session: Session,
    child_id: int,
    slug: str,
    category: Category | str,
    *,
    override: Category | str | None = None,
    kind: str = "app",
    enabled: bool = True,
) -> None:
    """One app in the catalog and on a child's screen, for a test that reports it
    in front: the daemon looks its kind of app up there, never in the report
    (D-228). `override` is the parent's own choice of kind for this child."""
    if session.get(App, slug) is None:
        session.add(
            App(
                slug=slug,
                name_tr=slug.title(),
                name_en=slug.title(),
                exec_cmd=slug,
                default_category=str(category),
                kind=kind,
            )
        )
    link = session.get(ChildApp, {"child_id": child_id, "app_slug": slug})
    if link is None:
        session.add(
            ChildApp(
                child_id=child_id,
                app_slug=slug,
                enabled=enabled,
                category_override=None if override is None else str(override),
            )
        )
    else:
        link.enabled = enabled
        link.category_override = None if override is None else str(override)
    session.commit()
