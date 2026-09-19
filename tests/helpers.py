"""Small builders shared by the test modules.

Kept out of `conftest.py` because these are plain functions, not fixtures, and
pytest puts the test directory on `sys.path` so a flat import works.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from biokids_core.types import AgeBand, ChildPolicy, EndAction, LedgerState

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
