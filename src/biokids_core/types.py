"""Value types shared by the policy engine, the ledger, the daemon and the panel.

Everything here is immutable and free of I/O. `reason_code` values are i18n keys,
never sentences: `ARCHITECTURE.md` §4 is explicit that the engine must not build
user-facing text, and that rule is what keeps Turkish and English honest.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

# --------------------------------------------------------------------------- enums


class Category(StrEnum):
    """The four activity categories from D-44. Per-category limits, never per-app."""

    GAMES = "games"
    LEARNING = "learning"
    WEB = "web"
    FREE = "free"


class AgeBand(StrEnum):
    """Two bands drive the whole child UI (D-96). Derived from age, parent-overridable."""

    YOUNG = "4-7"
    OLDER = "8-10"

    @classmethod
    def for_age(cls, age_years: int) -> AgeBand:
        return cls.YOUNG if age_years <= 7 else cls.OLDER


class EndAction(StrEnum):
    """What happens when the countdown after zero expires (D-43)."""

    LOCK = "lock"
    LOGOUT = "logout"
    POWEROFF = "poweroff"


class FilterMode(StrEnum):
    """Allowlist for 4-7, blocklist for 8-10 (D-61). Phase 1 gives these teeth."""

    ALLOWLIST = "allowlist"
    BLOCKLIST = "blocklist"


class Action(StrEnum):
    """What the enforcer should do about a decision."""

    NONE = "none"
    WARN = "warn"
    LOCK = "lock"
    LOGOUT = "logout"
    POWEROFF = "poweroff"
    TIMEOUT = "timeout"
    BLOCK = "block"


class Weekday(StrEnum):
    """Which quota applies today. Weekend is Saturday and Sunday."""

    WEEKDAY = "weekday"
    WEEKEND = "weekend"

    @classmethod
    def for_date(cls, day: dt.date) -> Weekday:
        return cls.WEEKEND if day.weekday() >= 5 else cls.WEEKDAY


# --------------------------------------------------------------------- reason codes


class Reason:
    """i18n keys for every decision the engine can reach.

    These strings are part of the contract between the engine and the UI. Changing
    one means changing both locale catalogs.
    """

    ALLOWED: Final = "allowed"
    PENALTY_ACTIVE: Final = "penalty.active"
    WINDOW_CLOSED: Final = "window.closed"
    WINDOW_WAITING: Final = "window.waiting"
    """Between two of today's windows. The screen locks and nothing else happens.

    Distinct from WINDOW_CLOSED, which means the last window of the day has
    ended and the day is genuinely over. Treating the two as one shut the
    machine down at 08:17 for a child whose next window opened at 16:00."""
    QUOTA_EXHAUSTED: Final = "quota.exhausted"
    QUOTA_WARNING: Final = "quota.warning"
    BREAK_DUE: Final = "break.due"
    BREAK_ACTIVE: Final = "break.active"
    CATEGORY_EXHAUSTED: Final = "category.exhausted"
    APP_NOT_ALLOWED: Final = "app.not_allowed"
    FILTER_BLOCKED: Final = "filter.blocked"
    AI_BLOCKED: Final = "ai.blocked"
    NO_POLICY: Final = "policy.missing"


# ------------------------------------------------------------------- time windows


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """An allowed interval within a single day, in local wall-clock time.

    A window may wrap past midnight (`start > end`), which is how "20:00-06:00"
    is expressed. Both ends are treated as [start, end): a window ending at 20:00
    closes the moment the clock reads 20:00:00.
    """

    start: dt.time
    end: dt.time

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError("a time window may not be empty (start == end)")

    @property
    def wraps_midnight(self) -> bool:
        return self.start > self.end

    def contains(self, moment: dt.time) -> bool:
        if self.wraps_midnight:
            return moment >= self.start or moment < self.end
        return self.start <= moment < self.end

    def seconds_until_close(self, moment: dt.time) -> int:
        """How long this window stays open, from `moment`. Zero if already closed."""
        if not self.contains(moment):
            return 0
        now_s = _seconds_of_day(moment)
        end_s = _seconds_of_day(self.end)
        if self.wraps_midnight and moment >= self.start:
            end_s += 86_400
        return max(0, end_s - now_s)

    @classmethod
    def parse(cls, text: str) -> TimeWindow:
        """Parse `"16:00-20:00"`. Whitespace tolerated; seconds are not accepted."""
        raw = text.strip()
        if "-" not in raw:
            raise ValueError(f"time window must look like '16:00-20:00', got {text!r}")
        start_s, _, end_s = raw.partition("-")
        return cls(_parse_hhmm(start_s), _parse_hhmm(end_s))

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def _parse_hhmm(text: str) -> dt.time:
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {text!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"expected HH:MM, got {text!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{text!r} is not a valid time of day")
    return dt.time(hour, minute)


def _seconds_of_day(moment: dt.time) -> int:
    return moment.hour * 3600 + moment.minute * 60 + moment.second


# ------------------------------------------------------------------------- policy


@dataclass(frozen=True, slots=True)
class ChildPolicy:
    """Everything the engine needs to know about one child's rules.

    Built from the `policy` table plus the child's row. Frozen so a decision can
    never mutate the rules it was decided from.
    """

    child_id: int
    display_name: str = ""
    age_band: AgeBand = AgeBand.YOUNG
    locale: str = "tr"

    weekday_quota_min: int = 60
    weekend_quota_min: int = 120
    weekday_windows: tuple[TimeWindow, ...] = ()
    weekend_windows: tuple[TimeWindow, ...] = ()

    end_action: EndAction = EndAction.POWEROFF
    warn_minutes: tuple[int, ...] = (10, 5, 1)
    lock_countdown_seconds: int = 120

    break_enabled: bool = False
    break_work_min: int = 40
    break_rest_min: int = 5

    earn_enabled: bool = False
    earn_ratio: float = 1.0
    earn_cap_min: int = 60
    earn_min_session_min: int = 10
    earn_idle_pause_min: int = 3
    earn_spendable_general: bool = False

    category_limits_min: dict[Category, int] = field(default_factory=dict)

    voice_enabled: bool = True
    """Whether Zio and Sio speak for this child.

    Per child, unlike `config.voice.enabled`, which is the machine-level switch
    in /etc/biokids/config.toml. The wizard has always written this column and
    for a long time nothing read it, so a parent who unticked "Sesler" because
    the baby was asleep got the warnings spoken anyway."""

    filter_mode: FilterMode = FilterMode.ALLOWLIST
    ai_enabled: bool = False
    ai_block_enabled: bool = False
    ai_timeout_min: int = 30
    assistant_enabled: bool = False
    assistant_voice: bool = False

    enabled: bool = True
    """A disabled policy means BioKids does not manage this child at all."""

    def quota_minutes(self, day: dt.date) -> int:
        return (
            self.weekend_quota_min
            if Weekday.for_date(day) is Weekday.WEEKEND
            else self.weekday_quota_min
        )

    def windows(self, day: dt.date) -> tuple[TimeWindow, ...]:
        return (
            self.weekend_windows
            if Weekday.for_date(day) is Weekday.WEEKEND
            else self.weekday_windows
        )

    def window_open(self, moment: dt.datetime) -> bool:
        """No windows configured means the whole day is allowed (D-42)."""
        windows = self.windows(moment.date())
        if not windows:
            return True
        return any(w.contains(moment.time()) for w in windows)

    def seconds_until_window_opens(self, moment: dt.datetime) -> int | None:
        """How long until the next window of *this* day opens, or None.

        None means no window is still ahead today - which is what makes the
        difference between "wait until four o'clock" and "that is all for
        today". A wrapping window (20:00-06:00) counts as ahead only by its
        start, because its tail belongs to tomorrow's date.
        """
        windows = self.windows(moment.date())
        if not windows:
            return None
        now_s = _seconds_of_day(moment.time())
        starts = [_seconds_of_day(w.start) for w in windows]
        ahead = [start - now_s for start in starts if start > now_s]
        return min(ahead) if ahead else None

    def seconds_until_window_closes(self, moment: dt.datetime) -> int | None:
        """None when no window constrains the child right now."""
        windows = self.windows(moment.date())
        if not windows:
            return None
        open_now = [w for w in windows if w.contains(moment.time())]
        if not open_now:
            return 0
        return max(w.seconds_until_close(moment.time()) for w in open_now)


# -------------------------------------------------------------------- ledger view


@dataclass(frozen=True, slots=True)
class LedgerState:
    """A snapshot of one child's counters for one calendar day, in seconds.

    The engine reads this; only `TimeLedger` produces it.
    """

    child_id: int
    day: dt.date
    general_used_s: int = 0
    earned_s: int = 0
    granted_s: int = 0
    """Minutes a parent added by hand today (D-47). Extra allowance, not negative
    usage: a grant must be worth ten minutes whether the child has used fifty
    minutes or two."""
    category_used_s: dict[Category, int] = field(default_factory=dict)
    penalty_remaining_s: int = 0
    break_remaining_s: int = 0
    seconds_since_break_s: int = 0
    learning_streak_s: int = 0

    def category_used(self, category: Category) -> int:
        return self.category_used_s.get(category, 0)


@dataclass(frozen=True, slots=True)
class ActivityContext:
    """What the child is doing at the moment of evaluation.

    Every field is optional: the engine must return something sensible for a
    child who has just logged in and has nothing on screen yet.
    """

    app_slug: str | None = None
    category: Category | None = None
    app_allowed: bool = True
    url: str | None = None
    url_blocked: bool = False
    ai_score: int = 0
    ai_block_threshold: int = 101
    """101 means "no AI score can ever block", which is the default for Phase 0."""

    session_active: bool = True
    """False when the child has no active, unlocked session on the active seat."""


# ----------------------------------------------------------------------- decision


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    """What Zio or Sio should say, as an event key plus substitutions (VOICE.md §8)."""

    event: str
    vars: dict[str, str | int] = field(default_factory=dict)
    priority: str = "normal"
    expression: str = "neutral"
    persona: str = "auto"


@dataclass(frozen=True, slots=True)
class Decision:
    """The engine's answer. `ARCHITECTURE.md` §4 defines this shape exactly."""

    allow: bool
    action: Action
    reason_code: str
    speak: SpeechRequest | None = None
    remaining_seconds: int = 0

    @property
    def remaining_minutes(self) -> int:
        """Rounded up: a child with 61 seconds left has "2 minutes", not "1"."""
        return -(-self.remaining_seconds // 60)
