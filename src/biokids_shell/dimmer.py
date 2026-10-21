"""The screen dims, gently, as an evening comes to its end (D-214).

In the last ten minutes of a day that ends at night, the brightness is lowered
in four steps to 60% of the child's own level - a room getting darker, not a
screen going off - and put back where it was the next time it can be: when the
day turns out not to be ending after all (a parent's "+10 dk", a special day),
when the launcher stops, or, if neither managed it, when the next session starts.

Presentation only. The daemon says how far away the night ending is, on every
tick; nothing here decides when a day ends, and nothing that enforces anything
waits for it. On a machine where the launcher's brightness control cannot
change anything (`device.set_brightness` finds no tool), the screen simply
stays as it is.

The child's own level is written to a file before the first step, in the
child's own state directory, because the backlight outlives the session:
systemd-backlight saves it at shutdown and restores it at boot, so a machine
switched off by the end action would otherwise wake up dimmed, for whoever uses
it next.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Final

from biokids_shell import device

log = logging.getLogger(__name__)

LEAD_SECONDS: Final = 10 * 60
"""`biokids_core.policy.BEDTIME_LEAD_SECONDS`: the daemon sends nothing earlier."""

STEPS: Final = ((450, 90), (300, 80), (150, 70))
"""(more than this many seconds left, percent of the child's level). Below the
last, FLOOR_PERCENT: 90 from ten minutes, 80 from seven and a half, 70 from five,
60 from two and a half."""

FLOOR_PERCENT: Final = 60
LOWEST_LEVEL: Final = 20
"""`device.set_brightness` will not go lower, and neither does this."""

STATE_NAME: Final = "brightness-before-bedtime"

Getter = Callable[[], Awaitable[int | None]]
Setter = Callable[[int], Awaitable[bool]]


def dim_percent(seconds_left: int | None) -> int:
    """How much of the child's own brightness to keep, `seconds_left` before
    a night ending. 100 when there is no night ending in sight."""
    if seconds_left is None or seconds_left > LEAD_SECONDS:
        return 100
    for above, percent in STEPS:
        if seconds_left > above:
            return percent
    return FLOOR_PERCENT


def state_file() -> Path:
    """`$XDG_STATE_HOME/biokids/brightness-before-bedtime`, in the child's home."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "biokids" / STATE_NAME


class BedtimeDimmer:
    """Follows the daemon's ticks; dims in steps; puts the brightness back."""

    def __init__(
        self,
        saved: Path | None = None,
        *,
        get: Getter = device.get_brightness,
        set_level: Setter = device.set_brightness,
    ) -> None:
        self._saved = saved or state_file()
        self._get = get
        self._set = set_level
        self._base: int | None = None
        """The child's own level, while the screen is dimmed by us."""
        self._applied: int | None = None
        self._unavailable = False
        """Set once a step could not be applied: not tried again every tick."""

    @property
    def dimmed(self) -> bool:
        return self._base is not None

    async def on_tick(self, payload: Mapping[str, Any]) -> None:
        """One tick's `bedtime_seconds`: dim a step further, or put it back.

        A tick without the field - the one a parent's grant publishes - says
        nothing about bedtime and changes nothing. A session that is not on
        the screen (`online` false) is not dimmed: the backlight belongs to
        whoever is in front of it.
        """
        if "bedtime_seconds" not in payload:
            return
        raw = payload.get("bedtime_seconds")
        seconds = raw if isinstance(raw, int) and not isinstance(raw, bool) else None
        percent = dim_percent(seconds)
        if percent >= 100:
            if self.dimmed:
                await self.restore()
            return
        if self._unavailable or not payload.get("online", True):
            return
        if self._base is None:
            # A level an earlier evening could not put back is the child's
            # own; what the screen reads now may be that evening's dimming.
            own = self._recall()
            if own is None:
                own = await self._get()
            self._base = own if own is not None else 100
            self._remember(self._base)
        level = max(LOWEST_LEVEL, round(self._base * percent / 100))
        if level == self._applied:
            return
        if await self._set(level):
            self._applied = level
        elif self._applied is None:
            log.info("the screen cannot be dimmed for bedtime on this machine")
            # Nothing changed, so there is nothing to put back.
            self._forget()
            self._unavailable = True
        else:
            # A later step that did not take: the earlier one still stands,
            # and the next tick tries again.
            log.info("a bedtime dimming step did not take")

    async def restore(self) -> None:
        """Put the child's own brightness back, from memory or from the file.

        Tried once. A failure keeps the file, so the next session starts by
        trying again, and is otherwise quiet: a slightly dim screen is not
        worth a child-facing error.
        """
        base = self._base if self._base is not None else self._recall()
        self._base = None
        self._applied = None
        if base is None:
            return
        if await self._set(base):
            self._forget()
            log.info("brightness put back to %s%% after bedtime", base)
        else:
            log.info("brightness could not be put back yet; trying again next session")

    # ------------------------------------------------------------- the file

    def _remember(self, level: int) -> None:
        try:
            self._saved.parent.mkdir(parents=True, exist_ok=True)
            self._saved.write_text(f"{level}\n", encoding="utf-8")
        except OSError as exc:
            log.info("could not keep the brightness for the next session: %s", exc)

    def _recall(self) -> int | None:
        try:
            text = self._saved.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not text.isdigit():
            self._forget()
            return None
        return device.clamp(int(text), low=LOWEST_LEVEL)

    def _forget(self) -> None:
        self._base = None
        self._applied = None
        try:
            self._saved.unlink(missing_ok=True)
        except OSError as exc:
            log.info("could not remove %s: %s", self._saved, exc)
