"""Form fields as policy values, and a policy as the audit log records it."""

from __future__ import annotations

from typing import Any

from biokids_core.db.models import Policy
from biokids_core.types import TimeWindow


def _windows_from_pickers(starts: list[str], ends: list[str]) -> str:
    """Pairs of `<input type="time">` values as the text the old field took.

    A pair with only one side filled is a mistake worth saying so about, not a
    window to guess at.
    """
    ranges: list[str] = []
    for start, end in zip(starts, ends, strict=False):
        start, end = start.strip(), end.strip()
        if start and end:
            ranges.append(f"{start}-{end}")
        elif start or end:
            raise ValueError("half a window")
    return ", ".join(ranges)


def _schedule_from_form(
    custom: list[str] | None, quotas: list[str], starts: list[str], ends: list[str]
) -> dict[int, dict[str, Any]]:
    """The per-day overrides (D-176): only the days ticked as different."""
    ticked = {int(day) for day in (custom or []) if day.isdigit() and 0 <= int(day) <= 6}
    days: dict[int, dict[str, Any]] = {}
    for day in sorted(ticked):
        entry: dict[str, Any] = {}
        quota = quotas[day].strip() if day < len(quotas) else ""
        if quota:
            entry["quota"] = max(0, min(int(quota), 24 * 60))
        start = starts[day] if day < len(starts) else ""
        end = ends[day] if day < len(ends) else ""
        text = _windows_from_pickers([start], [end])
        if text:
            entry["windows"] = _parse_window_list(text)
        if entry:
            days[day] = entry
    return days


def _schedule_from_json(raw: str) -> dict[int, dict[str, Any]]:
    """The same overrides, as the passphrase step carries them back."""
    if not raw:
        return {}
    probe = Policy(schedule_json=raw)
    return probe.schedule


def _parse_window_list(raw: str) -> list[str]:
    """`"16:00-20:00, 08:00-09:00"` -> a validated list, or ValueError."""
    parts = [chunk.strip() for chunk in raw.replace("\n", ",").split(",") if chunk.strip()]
    return [str(TimeWindow.parse(part)) for part in parts]


def _one_of[T: str](value: str, enum_cls: type[T], fallback: T) -> T:
    try:
        return enum_cls(value)
    except ValueError:
        return fallback


def _policy_snapshot(policy: Policy) -> dict[str, Any]:
    """What goes in the audit log when a rule changes: the fields, not the row."""
    return {
        "weekday_quota_min": policy.weekday_quota_min,
        "weekend_quota_min": policy.weekend_quota_min,
        "windows": policy.windows,
        "end_action": policy.end_action,
        "enabled": policy.enabled,
        "break_enabled": policy.break_enabled,
        "earn_enabled": policy.earn_enabled,
        "finish_up_min": policy.finish_up_min,
        "learn_first_min": policy.learn_first_min,
    }


def _minutes_or_zero(typed: str, most: int) -> int:
    """A number of minutes a parent may leave empty, which means none."""
    typed = typed.strip()
    if not typed:
        return 0
    try:
        return max(0, min(int(typed), most))
    except ValueError:
        return 0
