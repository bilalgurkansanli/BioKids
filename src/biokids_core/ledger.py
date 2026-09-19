"""The time ledger.

This is the hardest correctness problem in BioKids, so the rules it follows are
written down before the code that follows them.

**Nothing is counted from the wall clock.** Every interval is the difference
between two `CLOCK_BOOTTIME` readings, minus whatever part of that interval the
machine spent suspended. Moving the system clock changes nothing here; it only
changes which calendar day the seconds are filed under, and a jump larger than
ten minutes is reported to the parent rather than acted on.

**A tick counts only when all of these hold** (`ARCHITECTURE.md` §3.2):

* the child has a logind session and it is `Active`
* the session is not locked
* the seat is the active seat
* suspend is not in progress
* no forced break and no penalty is running

**Aggregation is per Linux user, not per session.** Two sessions for one child —
a fast user switch, a leftover SSH login — are one child as far as the ledger is
concerned. The caller passes a single `active` flag it has already reduced over
every session, which is why "counted once, not twice" is a property of the
interface rather than something this module has to defend.

**An interval longer than the clamp is not usage.** If the daemon was stopped,
the process was frozen, or the machine hibernated in a way the suspend
subtraction missed, the gap is recorded as a gap and clamped. Crediting an
unexplained four-hour gap as four hours of screen time would be the single
worst bug this file could have.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core.clock import Clock, ClockReading
from biokids_core.config import Config, get_config
from biokids_core.db.models import BootEpoch, LedgerDay, UsageTick
from biokids_core.types import Category, ChildPolicy, LedgerState

log = logging.getLogger(__name__)

CLAMP_FACTOR: Final = 3
"""An interval may be at most this many tick lengths. Anything longer is a gap."""

MIN_CLAMP_SECONDS: Final = 60
"""Never clamp below a minute, however short the configured tick is."""


@dataclass(frozen=True, slots=True)
class TickInput:
    """What the daemon knows about one child at one moment."""

    child_id: int
    active: bool
    """True only when the child has an active, unlocked session on the active seat."""
    idle: bool = False
    """No input for a while. Still consumes quota; does not accrue earned time."""
    category: Category | None = None
    app_slug: str = ""
    policy: ChildPolicy | None = None


@dataclass(frozen=True, slots=True)
class TickResult:
    """What one tick did. Returned so the daemon can log and publish without re-reading."""

    child_id: int
    day: dt.date
    counted_seconds: int
    elapsed_seconds: int
    suspended_seconds: int
    gap_seconds: int = 0
    """Seconds dropped by the clamp. Non-zero means something stopped the daemon."""
    earned_seconds: int = 0
    break_started: bool = False
    break_finished: bool = False
    penalty_finished: bool = False
    day_rolled_over: bool = False
    state: LedgerState | None = None


@dataclass
class ClockAnomaly:
    """A wall-clock jump. Logged and surfaced; never applied to the ledger."""

    delta_seconds: float
    wall_before: dt.datetime
    wall_after: dt.datetime

    @property
    def direction(self) -> str:
        return "forward" if self.delta_seconds > 0 else "backward"


@dataclass
class _ChildRuntime:
    """Per-child in-memory state between ticks. Never the source of truth."""

    last_reading: ClockReading | None = None
    dirty: bool = False
    pending_category_seconds: dict[tuple[Category, str], int] = field(default_factory=dict)


class TimeLedger:
    """Four counters per child per day, driven by an injected clock.

    The ledger owns writing; the policy engine only reads a `LedgerState`. That
    split is deliberate — the engine has no memory and no I/O, so every rule it
    applies can be tested without a database, and every number it applies them to
    can be tested without rules.
    """

    def __init__(
        self,
        session: Session,
        clock: Clock,
        config: Config | None = None,
    ) -> None:
        self._session = session
        self._clock = clock
        self._config = config or get_config()
        self._runtime: dict[int, _ChildRuntime] = {}
        self._days: dict[tuple[int, dt.date], LedgerDay] = {}
        self._boot_id: str | None = None
        self._last_persist_boot: float = 0.0
        self.anomalies: list[ClockAnomaly] = []

    # ------------------------------------------------------------- lifecycle

    @property
    def clamp_seconds(self) -> int:
        return max(MIN_CLAMP_SECONDS, self._config.daemon.tick_seconds * CLAMP_FACTOR)

    def begin(self) -> ClockReading:
        """Establish the boot epoch. Call once, before the first tick.

        A `boot_id` we have not seen means `CLOCK_BOOTTIME` restarted at zero.
        Nothing needs to be recomputed — the totals live in `ledger_day` and are
        read back per day — but the epoch is recorded so the audit log can show
        that the machine was rebooted, and so a *repeated* `boot_id` with a
        *lower* boot offset can be spotted as the anomaly it is.
        """
        reading = self._clock.read()
        self._boot_id = reading.boot_id
        self._last_persist_boot = reading.boot

        epoch = self._session.scalar(
            select(BootEpoch).where(BootEpoch.boot_id == reading.boot_id)
        )
        if epoch is None:
            epoch = BootEpoch(
                boot_id=reading.boot_id,
                first_seen_wall=reading.wall,
                last_seen_wall=reading.wall,
                last_boot_offset=reading.boot,
            )
            self._session.add(epoch)
            log.info("new boot epoch %s", reading.boot_id)
        else:
            if reading.boot + 1.0 < epoch.last_boot_offset:
                log.warning(
                    "boot offset went backwards within epoch %s: %.1f < %.1f",
                    reading.boot_id,
                    reading.boot,
                    epoch.last_boot_offset,
                )
            epoch.last_seen_wall = reading.wall
            epoch.last_boot_offset = max(epoch.last_boot_offset, reading.boot)
        self._session.flush()
        return reading

    # ------------------------------------------------------------------ tick

    def tick(self, inputs: list[TickInput]) -> list[TickResult]:
        """Advance every child by one interval. One clock reading for all of them."""
        reading = self._clock.read()
        results = [self._tick_child(item, reading) for item in inputs]
        self._maybe_persist(reading)
        return results

    def _tick_child(self, item: TickInput, reading: ClockReading) -> TickResult:
        runtime = self._runtime.setdefault(item.child_id, _ChildRuntime())
        previous = runtime.last_reading
        runtime.last_reading = reading

        day = reading.wall.date()
        row = self._day_row(item.child_id, day)

        if previous is None:
            # First tick for this child in this process. There is no interval to
            # attribute yet, and guessing one would be inventing usage.
            return TickResult(
                child_id=item.child_id,
                day=day,
                counted_seconds=0,
                elapsed_seconds=0,
                suspended_seconds=0,
                state=self.state(item.child_id, day),
            )

        if previous.boot_id != reading.boot_id:
            # A reboot happened between two ticks of a single process, which in
            # practice only happens in tests. Treat it as a fresh start: the
            # totals are already in the database.
            log.info("boot epoch changed mid-run: %s -> %s", previous.boot_id, reading.boot_id)
            self.begin()
            return TickResult(
                child_id=item.child_id,
                day=day,
                counted_seconds=0,
                elapsed_seconds=0,
                suspended_seconds=0,
                state=self.state(item.child_id, day),
            )

        boot_delta = reading.boot - previous.boot
        awake = reading.awake_since(previous)
        suspended = max(0.0, boot_delta - awake)

        self._check_clock_jump(previous, reading, awake)

        clamp = self.clamp_seconds
        gap = 0
        if awake > clamp:
            gap = int(awake - clamp)
            log.warning(
                "clamping a %.0f s interval for child %s to %s s (gap of %s s)",
                awake,
                item.child_id,
                clamp,
                gap,
            )
            awake = float(clamp)

        elapsed = int(awake)
        day_rolled = previous.wall.date() != day

        result = self._apply(item, row, elapsed, day)
        return TickResult(
            child_id=item.child_id,
            day=day,
            counted_seconds=result.counted,
            elapsed_seconds=elapsed,
            suspended_seconds=int(suspended),
            gap_seconds=gap,
            earned_seconds=result.earned,
            break_started=result.break_started,
            break_finished=result.break_finished,
            penalty_finished=result.penalty_finished,
            day_rolled_over=day_rolled,
            state=self.state(item.child_id, day),
        )

    # ------------------------------------------------------------ accounting

    @dataclass
    class _Applied:
        counted: int = 0
        earned: int = 0
        break_started: bool = False
        break_finished: bool = False
        penalty_finished: bool = False

    def _apply(
        self, item: TickInput, row: LedgerDay, elapsed: int, day: dt.date
    ) -> TimeLedger._Applied:
        out = TimeLedger._Applied()
        if elapsed <= 0:
            return out
        runtime = self._runtime[item.child_id]

        # A penalty and a forced break both run down in real time, whether or not
        # the child is sitting at the machine. A punishment you can pause by
        # logging out is not a punishment, and a break you can skip by walking
        # away defeats the point of a break.
        if row.penalty_remaining_s > 0:
            before = row.penalty_remaining_s
            row.penalty_remaining_s = max(0, before - elapsed)
            out.penalty_finished = before > 0 and row.penalty_remaining_s == 0
            runtime.dirty = True
            return out

        if row.break_remaining_s > 0:
            before = row.break_remaining_s
            row.break_remaining_s = max(0, before - elapsed)
            if before > 0 and row.break_remaining_s == 0:
                out.break_finished = True
                row.seconds_since_break_s = 0
            runtime.dirty = True
            return out

        if not item.active:
            # Not at the machine, or locked, or switched away. The streak of
            # continuous learning ends here; time itself does not accumulate.
            row.learning_streak_s = 0
            row.learning_credited_s = 0
            runtime.dirty = True
            return out

        # --- this is the only place general screen time is ever added ---
        row.general_s += elapsed
        row.seconds_since_break_s += elapsed
        out.counted = elapsed
        runtime.dirty = True

        category = item.category
        if category is not None:
            key = (category, item.app_slug)
            runtime.pending_category_seconds[key] = (
                runtime.pending_category_seconds.get(key, 0) + elapsed
            )

        policy = item.policy
        if (
            policy is not None
            and policy.break_enabled
            and row.seconds_since_break_s >= policy.break_work_min * 60
        ):
            row.break_remaining_s = policy.break_rest_min * 60
            out.break_started = True

        out.earned = self._accrue_earned(item, row, elapsed)
        return out

    def _accrue_earned(self, item: TickInput, row: LedgerDay, elapsed: int) -> int:
        """Turn continuous, non-idle learning time into bonus minutes (D-45, §3.4).

        The guards matter more than the formula. A child who opens and closes an
        educational app twenty times must earn nothing, so a streak shorter than
        `earn_min_session_min` pays out nothing at all; and a child who walks
        away with the app open must earn nothing, so idleness ends the streak.
        """
        policy = item.policy
        if policy is None or not policy.earn_enabled:
            return 0

        if item.category is not Category.LEARNING:
            row.learning_streak_s = 0
            row.learning_credited_s = 0
            return 0

        if item.idle:
            # Idle inside a learning app pauses accrual but does not destroy the
            # streak — a child reading a page on screen is not cheating.
            return 0

        row.learning_streak_s += elapsed
        threshold = policy.earn_min_session_min * 60
        if row.learning_streak_s < threshold:
            return 0

        uncredited = row.learning_streak_s - row.learning_credited_s
        if uncredited <= 0:
            return 0

        cap = policy.earn_cap_min * 60
        headroom = max(0, cap - row.earned_s)
        if headroom <= 0:
            row.learning_credited_s = row.learning_streak_s
            return 0

        granted = min(headroom, int(uncredited * policy.earn_ratio))
        if granted <= 0:
            return 0
        row.earned_s += granted
        row.learning_credited_s = row.learning_streak_s
        return granted

    # --------------------------------------------------------- clock hygiene

    def _check_clock_jump(
        self, previous: ClockReading, reading: ClockReading, awake: float
    ) -> None:
        """Compare how much time passed with what the wall clock claims.

        The comparison is against the *boot* delta, not the awake delta: a
        suspended machine legitimately sees the wall clock advance while
        `CLOCK_MONOTONIC` stands still, and calling that tampering would raise an
        alert every time a laptop lid closed.
        """
        threshold = self._config.daemon.clock_jump_threshold_seconds
        boot_delta = reading.boot - previous.boot
        wall_delta = reading.wall_delta(previous)
        drift = wall_delta - boot_delta
        if abs(drift) <= threshold:
            return
        anomaly = ClockAnomaly(
            delta_seconds=drift,
            wall_before=previous.wall,
            wall_after=reading.wall,
        )
        self.anomalies.append(anomaly)
        log.warning(
            "wall clock jumped %s by %.0f s (%s -> %s); the ledger is unaffected",
            anomaly.direction,
            abs(drift),
            previous.wall,
            reading.wall,
        )
        _ = awake  # the awake delta is what we counted; kept for readability

    def take_anomalies(self) -> list[ClockAnomaly]:
        """Hand the pending clock anomalies to the daemon, which audits them."""
        found, self.anomalies = self.anomalies, []
        return found

    # ------------------------------------------------------- parent actions

    def grant(self, child_id: int, seconds: int, day: dt.date | None = None) -> LedgerState:
        """The "+10 dk" button (D-47). Extra *allowance*, not negative usage.

        The tempting implementation is to subtract from `general_s`, which reads
        naturally and is wrong: a child who has used two minutes of a two-hour
        quota would get two minutes back out of a ten-minute grant, because usage
        cannot go below zero. A grant is worth what the parent said it was worth
        whatever the child has done today, so it raises the ceiling instead.
        """
        day = day or self._clock.read().wall.date()
        row = self._day_row(child_id, day)
        row.granted_s += seconds
        self._mark_dirty(child_id)
        self.persist()
        return self.state(child_id, day)

    def start_penalty(self, child_id: int, seconds: int, day: dt.date | None = None) -> None:
        day = day or self._clock.read().wall.date()
        row = self._day_row(child_id, day)
        row.penalty_remaining_s = max(row.penalty_remaining_s, seconds)
        self._mark_dirty(child_id)
        self.persist()

    def clear_penalty(self, child_id: int, day: dt.date | None = None) -> None:
        day = day or self._clock.read().wall.date()
        row = self._day_row(child_id, day)
        row.penalty_remaining_s = 0
        self._mark_dirty(child_id)
        self.persist()

    def start_break(self, child_id: int, seconds: int, day: dt.date | None = None) -> None:
        day = day or self._clock.read().wall.date()
        row = self._day_row(child_id, day)
        row.break_remaining_s = seconds
        self._mark_dirty(child_id)
        self.persist()

    def end_break(self, child_id: int, day: dt.date | None = None) -> None:
        day = day or self._clock.read().wall.date()
        row = self._day_row(child_id, day)
        row.break_remaining_s = 0
        row.seconds_since_break_s = 0
        self._mark_dirty(child_id)
        self.persist()

    def reset_day(self, child_id: int, day: dt.date | None = None) -> None:
        """Wipe today's counters for one child. Only reachable from the CLI."""
        day = day or self._clock.read().wall.date()
        row = self._day_row(child_id, day)
        row.general_s = 0
        row.earned_s = 0
        row.penalty_remaining_s = 0
        row.break_remaining_s = 0
        row.seconds_since_break_s = 0
        row.learning_streak_s = 0
        row.learning_credited_s = 0
        row.granted_s = 0
        row.warned = set()
        row.end_sequence_started_at = None
        self._mark_dirty(child_id)
        self.persist()

    # --------------------------------------------------- warning bookkeeping

    def already_warned(self, child_id: int, day: dt.date, threshold: int) -> bool:
        return threshold in self._day_row(child_id, day).warned

    def mark_warned(self, child_id: int, day: dt.date, threshold: int) -> None:
        row = self._day_row(child_id, day)
        row.warned = row.warned | {threshold}
        self._mark_dirty(child_id)

    def clear_warnings(self, child_id: int, day: dt.date) -> None:
        row = self._day_row(child_id, day)
        row.warned = set()
        self._mark_dirty(child_id)

    def rearm_warnings(self, child_id: int, day: dt.date, remaining_s: int) -> set[int]:
        """Forget warnings that are now in the future again.

        A parent presses "+10 dk" after the ten-minute warning has been spoken.
        The child now has twenty minutes, and when they get back down to ten they
        should hear it again — otherwise the extra time arrives with no warning
        at the end of it. A threshold stays remembered only while it is behind
        us: `threshold * 60 >= remaining`.
        """
        row = self._day_row(child_id, day)
        kept = {t for t in row.warned if t * 60 >= remaining_s}
        if kept != row.warned:
            row.warned = kept
            self._mark_dirty(child_id)
        return kept

    def end_sequence_started_at(self, child_id: int, day: dt.date) -> float | None:
        return self._day_row(child_id, day).end_sequence_started_at

    def start_end_sequence(self, child_id: int, day: dt.date, at_boot: float) -> None:
        row = self._day_row(child_id, day)
        if row.end_sequence_started_at is None:
            row.end_sequence_started_at = at_boot
            self._mark_dirty(child_id)
            self.persist()

    def clear_end_sequence(self, child_id: int, day: dt.date) -> None:
        row = self._day_row(child_id, day)
        row.end_sequence_started_at = None
        self._mark_dirty(child_id)

    # ------------------------------------------------------------- reading

    def state(self, child_id: int, day: dt.date | None = None) -> LedgerState:
        day = day or self._clock.read().wall.date()
        row = self._day_row(child_id, day)
        return LedgerState(
            child_id=child_id,
            day=day,
            general_used_s=row.general_s,
            earned_s=row.earned_s,
            granted_s=row.granted_s,
            category_used_s=self._category_seconds(child_id, day),
            penalty_remaining_s=row.penalty_remaining_s,
            break_remaining_s=row.break_remaining_s,
            seconds_since_break_s=row.seconds_since_break_s,
            learning_streak_s=row.learning_streak_s,
        )

    def _category_seconds(self, child_id: int, day: dt.date) -> dict[Category, int]:
        totals: dict[Category, int] = {}
        rows = self._session.scalars(
            select(UsageTick).where(UsageTick.child_id == child_id, UsageTick.day == day)
        ).all()
        for row in rows:
            try:
                category = Category(row.category)
            except ValueError:
                continue
            totals[category] = totals.get(category, 0) + row.seconds
        pending = self._runtime.get(child_id)
        if pending is not None:
            for (category, _slug), seconds in pending.pending_category_seconds.items():
                totals[category] = totals.get(category, 0) + seconds
        return totals

    # ---------------------------------------------------------- persistence

    def _day_row(self, child_id: int, day: dt.date) -> LedgerDay:
        cached = self._days.get((child_id, day))
        if cached is not None:
            return cached
        row = self._session.get(LedgerDay, {"child_id": child_id, "day": day})
        if row is None:
            row = LedgerDay(child_id=child_id, day=day)
            self._session.add(row)
            self._session.flush()
        self._days[(child_id, day)] = row
        return row

    def _mark_dirty(self, child_id: int) -> None:
        self._runtime.setdefault(child_id, _ChildRuntime()).dirty = True

    def _maybe_persist(self, reading: ClockReading) -> None:
        interval = self._config.daemon.persist_seconds
        if reading.boot - self._last_persist_boot >= interval:
            self.persist(reading)

    def persist(self, reading: ClockReading | None = None) -> None:
        """Write everything down. Called every 30 s and on every state change.

        A hard power cut therefore costs at most half a minute of accounting,
        which is the promise in `ARCHITECTURE.md` §3.3.
        """
        reading = reading or self._clock.read()
        self._flush_category_seconds(reading.wall.date())
        if self._boot_id:
            epoch = self._session.scalar(
                select(BootEpoch).where(BootEpoch.boot_id == self._boot_id)
            )
            if epoch is not None:
                epoch.last_seen_wall = reading.wall
                epoch.last_boot_offset = max(epoch.last_boot_offset, reading.boot)
        self._session.commit()
        self._last_persist_boot = reading.boot
        for runtime in self._runtime.values():
            runtime.dirty = False

    def _flush_category_seconds(self, day: dt.date) -> None:
        for child_id, runtime in self._runtime.items():
            if not runtime.pending_category_seconds:
                continue
            for (category, slug), seconds in runtime.pending_category_seconds.items():
                row = self._session.get(
                    UsageTick,
                    {
                        "child_id": child_id,
                        "day": day,
                        "category": category.value,
                        "app_slug": slug,
                    },
                )
                if row is None:
                    row = UsageTick(
                        child_id=child_id,
                        day=day,
                        category=category.value,
                        app_slug=slug,
                        seconds=0,
                    )
                    self._session.add(row)
                row.seconds += seconds
            runtime.pending_category_seconds.clear()

    def forget(self, child_id: int) -> None:
        """Drop in-memory state for a child, e.g. after they were deleted."""
        self._runtime.pop(child_id, None)
        for key in [k for k in self._days if k[0] == child_id]:
            self._days.pop(key, None)
