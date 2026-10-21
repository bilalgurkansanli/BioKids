"""Çıkartma albümüm: one sticker for each day a child spent time learning (D-211).

The owner asked for a reward for learning that is never screen time. Earned
minutes (ARCHITECTURE.md §3.4) already pay learning in time, which a parent may
switch off; a sticker pays it in nothing at all - a picture in an album the
child keeps, which they can show a grown-up and count.

The rule, and everything it is computed from:

- A day earns its sticker when the child spent at least `GOAL_SECONDS` - a
  quarter of an hour - in apps of the learning category that day. Those are the
  seconds the ledger already files under `learning` in `usage_tick`, the same
  ones the parent's History and Insights show; a tile the parent moved to
  another category counts where the parent put it.
- The day is the ledger's: the machine's local calendar day, so ten minutes
  before midnight and ten after are two days of ten minutes, not a sticker.
- Computed when it is read, never stored: the past days from `usage_tick`, today
  from the ledger, which knows the seconds not yet written. The album therefore
  holds the days `usage_tick` keeps, `retention.usage_days` (400 by default,
  D-189); a sticker older than that leaves the album with the minutes it was
  earned with.
- Each date has its own sticker, chosen from the date alone: the same all day,
  the same for siblings, a different one tomorrow. Consecutive days come from
  different themed sets, and no sticker comes back for `len(STICKERS)` days, so
  an album that grows looks varied.

Nothing here leaves the machine (principle 1), and nothing is hidden from the
child (principle 2): the album page says what earns a sticker and lists the
child's own tiles that do.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biokids_core.db.models import UsageTick
from biokids_core.types import Category

GOAL_SECONDS: Final = 15 * 60
"""A quarter of an hour of learning apps on one day earns that day's sticker.
Long enough to be more than opening an app to collect it, short enough for a
four-year-old's attention."""

DAYS_PER_WEEK: Final = 7


@dataclass(frozen=True, slots=True)
class Sticker:
    """One sticker: a picture, and a name in the catalogues (`album.sticker.*`)."""

    slug: str
    emoji: str
    theme: str

    @property
    def key(self) -> str:
        """The catalogue key of its name, which Zio says when it is earned."""
        return f"album.sticker.{self.slug}"


def _set(theme: str, *stickers: tuple[str, str]) -> tuple[Sticker, ...]:
    return tuple(Sticker(slug, emoji, theme) for slug, emoji in stickers)


STICKERS: Final[tuple[Sticker, ...]] = (
    *_set(
        "animals",
        ("fox", "🦊"),
        ("panda", "🐼"),
        ("lion", "🦁"),
        ("koala", "🐨"),
        ("giraffe", "🦒"),
        ("elephant", "🐘"),
        ("hedgehog", "🦔"),
        ("owl", "🦉"),
    ),
    *_set(
        "sea",
        ("dolphin", "🐬"),
        ("whale", "🐳"),
        ("octopus", "🐙"),
        ("crab", "🦀"),
        ("fish", "🐠"),
        ("seal", "🦭"),
        ("seashell", "🐚"),
        ("turtle", "🐢"),
    ),
    *_set(
        "sky",
        ("rocket", "🚀"),
        ("planet", "🪐"),
        ("moon", "🌙"),
        ("star", "🌟"),
        ("comet", "☄️"),
        ("rainbow", "🌈"),
        ("sun", "☀️"),
        ("saucer", "🛸"),
    ),
    *_set(
        "nature",
        ("sunflower", "🌻"),
        ("tulip", "🌷"),
        ("mushroom", "🍄"),
        ("cactus", "🌵"),
        ("clover", "🍀"),
        ("tree", "🌳"),
        ("ladybird", "🐞"),
        ("butterfly", "🦋"),
    ),
    *_set(
        "food",
        ("apple", "🍎"),
        ("strawberry", "🍓"),
        ("watermelon", "🍉"),
        ("banana", "🍌"),
        ("cherries", "🍒"),
        ("carrot", "🥕"),
        ("cupcake", "🧁"),
        ("lemon", "🍋"),
    ),
    *_set(
        "vehicles",
        ("train", "🚂"),
        ("bicycle", "🚲"),
        ("sailboat", "⛵"),
        ("helicopter", "🚁"),
        ("fire-engine", "🚒"),
        ("tractor", "🚜"),
        ("plane", "✈️"),
        ("bus", "🚌"),
    ),
    *_set(
        "play",
        ("balloon", "🎈"),
        ("kite", "🪁"),
        ("crayon", "🖍️"),
        ("drum", "🥁"),
        ("guitar", "🎸"),
        ("teddy", "🧸"),
        ("ball", "⚽"),
        ("trumpet", "🎺"),
    ),
    *_set(
        "magic",
        ("unicorn", "🦄"),
        ("wand", "🪄"),
        ("dinosaur", "🦕"),
        ("crown", "👑"),
        ("castle", "🏰"),
        ("present", "🎁"),
        ("trophy", "🏆"),
        ("sparkles", "✨"),
    ),
)
"""Eight sets of eight, every one cheerful, none from an emoji newer than
Unicode 13, which every font a Debian 13 machine has can draw."""

STEP: Final = 9
"""How far through `STICKERS` the next day's sticker is: one set further and one
place along, so neighbouring days never share a set. Coprime with the length
(tests/test_album.py), so every sticker comes once before any comes twice."""


def sticker_for(day: dt.date) -> Sticker:
    """The sticker a day earns. The date alone decides it."""
    return STICKERS[(day.toordinal() * STEP) % len(STICKERS)]


def week_of(day: dt.date) -> list[dt.date]:
    """Monday to Sunday around `day`: the week the album page shows."""
    monday = day - dt.timedelta(days=day.weekday())
    return [monday + dt.timedelta(days=n) for n in range(DAYS_PER_WEEK)]


def earned_days(
    seconds_by_day: Mapping[dt.date, int], goal: int = GOAL_SECONDS
) -> list[dt.date]:
    """The days with at least `goal` seconds of learning, newest first."""
    return sorted(
        (day for day, seconds in seconds_by_day.items() if seconds >= goal), reverse=True
    )


def learning_seconds(
    session: Session,
    child_id: int,
    *,
    before: dt.date | None = None,
    since: dt.date | None = None,
) -> dict[dt.date, int]:
    """Seconds in learning apps per day, from `usage_tick`; one indexed query.

    `before` leaves out that day and every later one: today is the ledger's,
    which also knows the seconds it has not written yet.
    """
    query = (
        select(UsageTick.day, func.sum(UsageTick.seconds))
        .where(UsageTick.child_id == child_id, UsageTick.category == Category.LEARNING.value)
        .group_by(UsageTick.day)
    )
    if before is not None:
        query = query.where(UsageTick.day < before)
    if since is not None:
        query = query.where(UsageTick.day >= since)
    return {day: int(seconds or 0) for day, seconds in session.execute(query).all()}


def for_shell(
    session: Session, child_id: int, today: dt.date, today_seconds: int
) -> dict[str, Any]:
    """What the launcher's album needs, for the daemon's shell state.

    Dates only: which sticker a date earns is the launcher's to work out, with
    `sticker_for`, from the same module. `today` is the ledger's day, so the
    launcher's week is the one the minutes were counted in, whatever the
    launcher's own clock says.
    """
    seconds = learning_seconds(session, child_id, before=today)
    seconds[today] = max(0, int(today_seconds))
    return {
        "today": today.isoformat(),
        "days": [day.isoformat() for day in earned_days(seconds)],
        "today_seconds": seconds[today],
        "goal_seconds": GOAL_SECONDS,
    }


# ------------------------------------------------------------- the launcher


@dataclass(frozen=True, slots=True)
class Earned:
    day: dt.date
    sticker: Sticker


@dataclass(frozen=True, slots=True)
class WeekDay:
    day: dt.date
    sticker: Sticker | None
    """The sticker it earned, or None."""
    today: bool
    future: bool


@dataclass(frozen=True, slots=True)
class Album:
    """The album as the launcher draws it, from the daemon's shell state."""

    today: dt.date
    earned: tuple[Earned, ...]
    """Every sticker in the album, newest first."""
    today_seconds: int
    goal_seconds: int = GOAL_SECONDS

    @property
    def count(self) -> int:
        return len(self.earned)

    @property
    def earned_today(self) -> bool:
        return bool(self.earned) and self.earned[0].day == self.today

    @property
    def today_sticker(self) -> Sticker:
        return sticker_for(self.today)

    @property
    def week(self) -> tuple[WeekDay, ...]:
        mine = {item.day: item.sticker for item in self.earned}
        return tuple(
            WeekDay(
                day=day, sticker=mine.get(day), today=day == self.today, future=day > self.today
            )
            for day in week_of(self.today)
        )

    @property
    def week_count(self) -> int:
        return sum(1 for day in self.week if day.sticker is not None)

    @property
    def minutes_left(self) -> int:
        """Whole minutes still to go today, rounded up: 30 seconds is "1 more"."""
        return max(0, math.ceil((self.goal_seconds - self.today_seconds) / 60))

    @property
    def goal_minutes(self) -> int:
        return math.ceil(self.goal_seconds / 60)

    @property
    def progress(self) -> float:
        """Today's share of the goal, 0 to 1, for the week strip's circle."""
        if self.goal_seconds <= 0:
            return 1.0
        return max(0.0, min(1.0, self.today_seconds / self.goal_seconds))


def from_state(state: Mapping[str, Any] | None) -> Album | None:
    """The album in the daemon's shell state, or None when there is none.

    None for a daemon that does not send one yet - during an upgrade the old
    daemon may answer the new launcher - and for anything malformed: the album
    is never a reason for the home screen to fail.
    """
    raw = (state or {}).get("album")
    if not isinstance(raw, Mapping):
        return None
    try:
        today = dt.date.fromisoformat(str(raw["today"]))
        goal = int(raw.get("goal_seconds", GOAL_SECONDS))
        today_seconds = max(0, int(raw.get("today_seconds", 0)))
        days = _dates(raw.get("days", []))
    except (KeyError, TypeError, ValueError):
        return None
    if goal <= 0:
        return None
    # Never a sticker from a day still to come, and today's from the same
    # seconds the page shows, so the two cannot disagree.
    days = {day for day in days if day < today}
    if today_seconds >= goal:
        days.add(today)
    earned = tuple(Earned(day, sticker_for(day)) for day in sorted(days, reverse=True))
    return Album(today=today, earned=earned, today_seconds=today_seconds, goal_seconds=goal)


def _dates(values: Iterable[Any]) -> set[dt.date]:
    if isinstance(values, (str, bytes)):
        raise TypeError("days must be a list")
    return {dt.date.fromisoformat(str(value)) for value in values}


def stickers_in(seconds_by_day: Mapping[dt.date, int], days: Iterable[dt.date]) -> int:
    """How many of `days` earned a sticker: what the parent's Insights counts."""
    wanted = set(days)
    return sum(1 for day in earned_days(seconds_by_day) if day in wanted)


__all__ = [
    "GOAL_SECONDS",
    "STICKERS",
    "Album",
    "Earned",
    "Sticker",
    "WeekDay",
    "earned_days",
    "for_shell",
    "from_state",
    "learning_seconds",
    "sticker_for",
    "stickers_in",
    "week_of",
]
