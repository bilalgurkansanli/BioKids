#!/usr/bin/env python3
"""Drive the time ledger with an injected clock, so an hour takes a second.

The end-of-time sequence is the part of BioKids a developer most needs to *see*:
warnings at ten, five and one minute, the lock at zero, the 120-second countdown,
then the configured end action. Waiting an hour to watch it once is how that code
ends up untested by hand, so this script replaces `SystemClock` with `FakeClock`
and advances time by hand, exactly as `tests/test_ledger.py` does. The difference
is that the tests assert and this prints: the point here is a timeline a human
reads and says "yes, that is what should happen".

It also plays the four scenarios that make the ledger hard, and shows that none
of them change the number of seconds counted:

    --suspend         the lid closed for two hours
    --reboot          the machine went off and came back mid-session
    --clock-forward   a child moved the system clock forward three hours
    --clock-back      ...and then moved it back

Every run builds its own throwaway SQLite database in a fresh directory, under
`BIOKIDS_PREFIX` when that is set and in the system temporary directory when it
is not, and deletes it again at the end unless `--keep` says otherwise. There is
deliberately no way to point it at an existing database: `/var/lib/biokids` is
never touched, and neither is the tree a developer is actually working in.

The exit status is the verdict: zero when the seconds counted match the quota
allowed, non-zero when they do not. Reading the timeline is the point, but a
demonstration that cannot fail is one nobody needs to watch, and this one ends
up in a loop over tick sizes sooner or later.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import logging
import math
import os
import random
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from biokids_voice.lines import LineBook

ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from biokids_core import repo  # noqa: E402
from biokids_core.clock import FakeClock  # noqa: E402
from biokids_core.config import ENV_PREFIX_DIR, Config, set_config  # noqa: E402
from biokids_core.db import Child, Database, Family  # noqa: E402
from biokids_core.i18n import translator  # noqa: E402
from biokids_core.ledger import TickInput, TimeLedger  # noqa: E402
from biokids_core.policy import evaluate  # noqa: E402
from biokids_core.types import (  # noqa: E402
    Action,
    ActivityContext,
    AgeBand,
    ChildPolicy,
    Decision,
    EndAction,
    Reason,
    TimeWindow,
)

END_SEQUENCE_REASONS: Final = frozenset({Reason.QUOTA_EXHAUSTED, Reason.WINDOW_CLOSED})
"""The two decisions that start the countdown (`ARCHITECTURE.md` §3.5). A forced
break also locks the screen, but it ends by itself and is not the end of the day."""

ENDGAME_SECONDS: Final = 120
"""Below this much time left, the timeline switches to its close-up cadence. This
is the stretch the script exists to show."""

ENDGAME_CADENCE_SECONDS: Final = 30
"""How often a row is printed during the close-up, in simulated seconds."""


# --------------------------------------------------------------------- scenarios


@dataclass(frozen=True, slots=True)
class Event:
    """Something that happens to the clock at a given tick."""

    kind: str
    minutes: int

    def description(self) -> str:
        if self.kind == "reboot":
            return f"reboot, off for {self.minutes} min"
        if self.kind.startswith("clock-"):
            return f"system clock {self.kind.removeprefix('clock-')} {self.minutes} min"
        return f"{self.kind} for {self.minutes} min"

    def apply(self, clock: FakeClock) -> str:
        """Move the fake clock and describe what a human just did."""
        seconds = self.minutes * 60
        match self.kind:
            case "suspend":
                clock.suspend(seconds)
                return (
                    f"lid closed for {_hms(seconds)}"
                    " (BOOTTIME and wall move, MONOTONIC does not)"
                )
            case "reboot":
                clock.reboot(off_for=seconds)
                return f"machine off for {_hms(seconds)}, new boot epoch {clock.boot_id}"
            case "clock-forward":
                clock.jump_wall(seconds)
                return f"system clock moved FORWARD {_hms(seconds)}"
            case "clock-back":
                clock.jump_wall(-seconds)
                return f"system clock moved BACK {_hms(seconds)}"
        raise ValueError(f"unknown scenario {self.kind!r}")  # pragma: no cover


# ------------------------------------------------------------------- presentation


class Ink:
    """ANSI colour, when the terminal is one. The text reads the same without it."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def warn(self, text: str) -> str:
        return self(text, "33;1")

    def alarm(self, text: str) -> str:
        return self(text, "31;1")

    def event(self, text: str) -> str:
        return self(text, "36")

    def dim(self, text: str) -> str:
        return self(text, "2")


VOICE_SEED: Final = 0
"""`LineBook` picks a phrasing at random. Seeded here, because two runs of the
same scenario that differ only in which of three greetings came out are two runs
a developer has to read twice."""


class Voice:
    """The sentence a child would actually hear for a speech event.

    Not the same string as the reason code. `quota.warning` is what the panel
    and the lock screen *show*; what Zio says is a variant out of
    `biokids_core/data/voice/<locale>/warn_10.txt`, and printing the first while
    calling it the second would misreport the one thing this script exists to
    demonstrate. `biokids_voice` is imported lazily because it is a separate
    binary package: a checkout with only `biokids-core` on the path still runs,
    with the status strings and a note in the header saying so.
    """

    def __init__(self, seed: int = VOICE_SEED) -> None:
        self._book: LineBook | None = None
        try:
            from biokids_voice.lines import LineBook as _LineBook
        except ImportError:
            return
        self._book = _LineBook(rng=random.Random(seed))

    @property
    def available(self) -> bool:
        return self._book is not None

    def say(self, event: str, locale: str, variables: dict[str, str | int]) -> str | None:
        """None when this machine has no wording for the event."""
        if self._book is None:
            return None
        line = self._book.render(event, locale, dict(variables))
        return line.text if line is not None else None


def _hms(seconds: float) -> str:
    total = int(seconds)
    sign = "-" if total < 0 else ""
    total = abs(total)
    return f"{sign}{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _prepare_stdout() -> None:
    """Turkish output must never be the reason a dev tool crashes on Windows."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def _configure_logging(show: bool) -> None:
    """Keep the ledger's own log off the timeline unless it was asked for.

    `TimeLedger` logs clock jumps and clamped gaps at WARNING, and those land on
    stderr out of order with this script's buffered stdout — next to a timeline
    that already reports both, they read as duplicates rather than as evidence.
    """
    logging.basicConfig(
        level=logging.DEBUG if show else logging.CRITICAL,
        format="    log: %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# ------------------------------------------------------------------- the run


@dataclass(frozen=True, slots=True)
class Row:
    """One line of the timeline.

    `status` describes the tick and is shown only when the row is printed for
    some other reason; a note, by contrast, is something that happened once and
    always earns its own line.
    """

    marker: str
    label: str
    status: str = ""


@dataclass
class Totals:
    counted: int = 0
    suspended: int = 0
    gaps: int = 0
    ticks: int = 0
    spoken: list[int] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    ended_with: str = ""
    ended_reason: str = ""
    ended_at: dt.datetime | None = None


class Simulation:
    """One simulated session, from login to the end action."""

    def __init__(
        self,
        *,
        ledger: TimeLedger,
        clock: FakeClock,
        policy: ChildPolicy,
        events: dict[int, Event],
        args: argparse.Namespace,
        ink: Ink,
        voice: Voice,
    ) -> None:
        self._ledger = ledger
        self._clock = clock
        self._policy = policy
        self._events = events
        self._args = args
        self._ink = ink
        self._voice = voice
        self._t = translator(policy.locale)
        self._start_wall = clock.wall
        self._locked = False
        self._previous_row: tuple[str, str] | None = None
        self._last_printed = 0
        self.totals = Totals()

    # The child is at the machine until the lock screen appears; after that the
    # session is locked, which is precisely why the ledger must stop counting.
    @property
    def _active(self) -> bool:
        return not self._locked

    def run(self) -> None:
        tick = self._args.tick
        requested = math.ceil(self._args.minutes * 60 / tick)
        # Room after the requested session for the countdown to expire on screen.
        budget = requested + math.ceil(self._policy.lock_countdown_seconds / tick) + 2

        self._print_header(requested)
        for step in range(budget):
            if step >= requested and not self._locked:
                break
            if self._step(step):
                break

    # ------------------------------------------------------------- one tick

    def _step(self, step: int) -> bool:
        """Advance one tick. Returns True when the session is over."""
        notes: list[str] = []
        self._clock.advance(self._args.tick)
        event = self._events.get(step)
        if event is not None:
            notes.append(self._ink.event(event.apply(self._clock)))

        inputs = [
            TickInput(child_id=self._policy.child_id, active=self._active, policy=self._policy)
        ]
        result = self._ledger.tick(inputs)[0]
        state = result.state
        assert state is not None  # a tick always returns the state it produced
        decision = evaluate(
            self._policy,
            state,
            ActivityContext(session_active=self._active),
            self._clock.wall,
        )

        self.totals.ticks += 1
        self.totals.counted += result.counted_seconds
        self.totals.suspended += result.suspended_seconds
        self.totals.gaps += result.gap_seconds

        for anomaly in self._ledger.take_anomalies():
            text = (
                f"wall clock jumped {anomaly.direction} by {_hms(abs(anomaly.delta_seconds))}"
                " -> audit_log; the ledger is unaffected"
            )
            self.totals.anomalies.append(text)
            notes.append(self._ink.alarm(text))
        if result.gap_seconds:
            notes.append(self._ink.alarm(f"clamped a gap of {_hms(result.gap_seconds)}"))
        if result.day_rolled_over:
            notes.append(self._ink.event(f"calendar day is now {result.day}"))

        row = self._classify(decision, result.day, notes)
        if self._should_print(step, decision, notes, row):
            self._print_row(row, decision, state.general_used_s, notes)
        return row.label == "END"

    def _classify(self, decision: Decision, day: dt.date, notes: list[str]) -> Row:
        """Describe this tick, and run the daemon's side of the end sequence.

        Anything appended to `notes` forces the row to be printed, so only things
        that happen *once* go there; a per-tick observation belongs in `status`,
        which is shown when the row prints anyway and is silent otherwise.
        """
        child_id = self._policy.child_id

        if decision.action is Action.WARN and decision.speak is not None:
            threshold = int(decision.speak.vars.get("minutes", 0))
            if not self._ledger.already_warned(child_id, day, threshold):
                self._ledger.mark_warned(child_id, day, threshold)
                self.totals.spoken.append(threshold)
                # The event key is what the voice service receives; the sentence
                # beside it is what the child actually hears.
                spoken = f'speaks {decision.speak.event}: "{self._speech(decision)}"'
                notes.append(self._ink.warn(spoken))
                return Row(" ! ", "WARN")
            return Row("   ", "warn", self._ink.dim("spoken once already"))

        if decision.allow:
            return Row("   ", "ok")

        if decision.reason_code not in END_SEQUENCE_REASONS:
            return Row("   ", decision.action.value, self._speech(decision))

        if not self._locked:
            self._locked = True
            self._ledger.start_end_sequence(child_id, day, self._clock.boot)
            event = decision.speak.event if decision.speak else "—"
            notes.append(
                self._ink.alarm(
                    f'speaks {event}: "{self._speech(decision)}" — screen locked, '
                    f"{self._policy.lock_countdown_seconds} s countdown starts"
                )
            )
            return Row("!! ", "LOCK")

        started = self._ledger.end_sequence_started_at(child_id, day)
        elapsed = max(0.0, self._clock.boot - (started or self._clock.boot))
        if elapsed >= self._policy.lock_countdown_seconds:
            self.totals.ended_with = self._policy.end_action.value
            self.totals.ended_reason = decision.reason_code
            self.totals.ended_at = self._clock.wall
            notes.append(
                self._ink.alarm(
                    f"countdown expired -> {self._policy.end_action.value}"
                    " (applications get SIGTERM, then SIGKILL after 10 s)"
                )
            )
            return Row("!! ", "END")

        left = int(self._policy.lock_countdown_seconds - elapsed)
        status = f"{left} s until {self._policy.end_action.value}, quota no longer running down"
        return Row(" ! ", "lock", self._ink.dim(status))

    def _speech(self, decision: Decision) -> str:
        """The line the child would hear, from the real voice line files.

        A decision with nothing to say — a blocked app, an active penalty — has
        no spoken line at all, and then the reason code's own text is the honest
        thing to show: it is what the lock screen would put in front of the
        child instead.
        """
        if decision.speak is None:
            return self._t(decision.reason_code)
        spoken = self._voice.say(decision.speak.event, self._policy.locale, decision.speak.vars)
        return (
            spoken
            if spoken is not None
            else self._t(decision.reason_code, **decision.speak.vars)
        )

    # -------------------------------------------------------------- printing

    def _should_print(self, step: int, decision: Decision, notes: list[str], row: Row) -> bool:
        """Print what changed, not every tick — 65 minutes is 390 of them.

        Rows are also spaced by simulated seconds rather than by a tick count, so
        the timeline reads the same whether the tick is 10 seconds or two minutes.
        """
        key = (row.label, decision.reason_code)
        changed = key != self._previous_row
        self._previous_row = key
        if self._args.verbose or step == 0 or notes or row.label.isupper() or changed:
            return True

        endgame = self._locked or decision.remaining_seconds <= ENDGAME_SECONDS
        cadence: int = ENDGAME_CADENCE_SECONDS if endgame else self._args.every * 60
        since: int = (self.totals.ticks - self._last_printed) * self._args.tick
        return since >= cadence

    def _print_header(self, ticks: int) -> None:
        policy = self._policy
        quota = policy.quota_minutes(self._clock.wall.date())
        warnings = ", ".join(str(m) for m in sorted(policy.warn_minutes, reverse=True))
        windows = (
            ", ".join(str(w) for w in policy.windows(self._clock.wall.date())) or "all day"
        )
        print(
            f"  child        {policy.display_name} ({policy.age_band}), locale {policy.locale}"
        )
        print(f"  quota        {quota} min/day, allowed {windows}")
        print(
            f"  warnings     {warnings} minutes left, then lock and {policy.end_action.value}"
        )
        source = (
            "biokids_voice line files"
            if self._voice.available
            else "status strings (biokids_voice is not importable)"
        )
        print(f"  spoken from  {source}")
        print(
            f"  simulation   {self._args.minutes} min of continuous use,"
            f" {self._args.tick} s ticks ({ticks} ticks), starting {self._start_wall:%a %H:%M}"
        )
        for step, event in sorted(self._events.items()):
            late = "  (after the run ends; it will not fire)" if step >= ticks else ""
            print(
                f"  scenario     +{_hms(step * self._args.tick)}  {event.description()}{late}"
            )
        print()
        print(
            f"   {'sim':>9} {'wall':>8} {'used':>8} {'left':>8}  "
            f"{'what':<5} {'reason':<18} note"
        )
        print("   " + "-" * 92)

    def _print_row(self, row: Row, decision: Decision, used: int, notes: list[str]) -> None:
        self._last_printed = self.totals.ticks
        sim = (self._clock.wall - self._start_wall).total_seconds()
        note = "  ".join([*notes, row.status]).strip()
        # Padded before colouring: escape sequences count as characters to the
        # formatter and would knock every following column out of line.
        shown = f"{row.label:<5}"
        if row.label in {"LOCK", "END"}:
            shown = self._ink.alarm(shown)
        elif row.label == "WARN":
            shown = self._ink.warn(shown)
        line = (
            f"{row.marker}+{_hms(sim):>8} {self._clock.wall:%H:%M:%S} {_hms(used):>8}"
            f" {_hms(decision.remaining_seconds):>8}  {shown} {decision.reason_code:<18} {note}"
        )
        print(line.rstrip())

    # --------------------------------------------------------------- summary

    def print_summary(self) -> bool:
        """Print the summary and report whether the ledger agreed with arithmetic."""
        totals = self.totals
        allowance = self._policy.quota_minutes(self._start_wall.date()) * 60
        print()
        print("  Summary")
        print(
            f"    simulated    {_hms((self._clock.wall - self._start_wall).total_seconds())}"
            f" of wall time in {totals.ticks} ticks"
        )
        print(
            f"    counted      {_hms(totals.counted)} of screen time,"
            f" against a {_hms(allowance)} quota"
        )
        print(f"    suspended    {_hms(totals.suspended)}, clamped gaps {_hms(totals.gaps)}")
        if totals.anomalies:
            for text in totals.anomalies:
                print(f"    clock jump   {text}")
        spoken = ", ".join(f"warn_{m}" for m in totals.spoken) or "none"
        print(f"    spoken       {spoken}")
        if totals.ended_with and totals.ended_at is not None:
            print(f"    ended        {totals.ended_with} at {totals.ended_at:%H:%M:%S}")
        else:
            print("    ended        the simulation ran out before the quota did")
        verdict, agreed = self._verdict(allowance)
        print()
        print(f"    verdict      {verdict}")
        return agreed

    def _verdict(self, allowance: int) -> tuple[str, bool]:
        """The one line worth reading when the scenarios are the point.

        The second half of the answer is whether the numbers came out right,
        because the caller turns it into an exit status. A run that counted the
        wrong hour and still exited 0 would be a demonstration that cannot fail,
        which is the same as one nobody needs to watch — and it would go
        unnoticed the moment this ends up in a shell loop.
        """
        counted = self.totals.counted
        if not self.totals.ended_with:
            return f"{_hms(counted)} of {_hms(allowance)} used; no lock yet", True
        if self.totals.ended_reason == Reason.WINDOW_CLOSED:
            return (
                f"stopped at the end of the allowed window after {_hms(counted)},"
                f" with {_hms(max(0, allowance - counted))} of quota still unspent",
                True,
            )
        drift = counted - allowance
        if abs(drift) > self._args.tick:
            return (
                f"MISMATCH: counted {_hms(counted)} against an allowance of"
                f" {_hms(allowance)} ({_hms(drift)} out)",
                False,
            )
        verdict = f"quota consumed exactly ({_hms(counted)} counted, {_hms(allowance)} allowed)"
        played = sorted({event.kind for event in self._events.values()})
        if played:
            verdict += f"; {', '.join(played)} cost nothing"
        return verdict, True


# ------------------------------------------------------------------ plumbing


def build_events(args: argparse.Namespace) -> dict[int, Event]:
    """Place the requested scenarios on consecutive ticks from `--at`.

    Consecutive rather than simultaneous so each one gets its own row: a reboot
    that happens in the same tick as a clock jump is a fine test and a terrible
    demonstration.
    """
    requested = [
        Event("suspend", args.suspend),
        Event("reboot", args.reboot),
        Event("clock-forward", args.clock_forward),
        Event("clock-back", args.clock_back),
    ]
    at = args.at if args.at is not None else max(1, args.minutes // 2)
    start = math.ceil(at * 60 / args.tick)
    return {
        start + index: event
        for index, event in enumerate(e for e in requested if e.minutes > 0)
    }


def make_scratch_root(explicit: str | None) -> Path:
    """Create a throwaway root for this run and return it.

    Always a *new* directory, never a tree that is already in use. A developer
    with `BIOKIDS_PREFIX` pointing at their working installation would otherwise
    find a child called "fakeclock" in the database they were about to demo, so
    the variable decides where the throwaway tree lives (`<prefix>/tmp/...`),
    not which database gets written to.
    """
    if explicit:
        base = Path(explicit)
    else:
        configured = os.environ.get(ENV_PREFIX_DIR)
        base = Path(configured) / "tmp" if configured else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)

    prefix = Path(tempfile.mkdtemp(prefix="biokids-fakeclock-", dir=base))
    # Anything this process reaches for later resolves against the same root.
    os.environ[ENV_PREFIX_DIR] = str(prefix)
    return prefix


def build_config(prefix: Path, tick: int) -> Config:
    """A config that exists only inside `prefix`, with the requested tick size."""
    return Config.from_mapping(
        {"daemon": {"tick_seconds": tick, "persist_seconds": max(tick, 30)}}
    ).with_prefix(prefix)


def seed_child(session: Session, args: argparse.Namespace, today: dt.date) -> Child:
    """One family, one child, one policy — the smallest thing the ledger can drive."""
    session.add(Family(locale=args.locale))
    child = Child(
        unix_user="fakeclock",
        display_name=args.name,
        birth_year=today.year - 6,
        age_band=AgeBand.YOUNG.value,
        locale=args.locale,
    )
    session.add(child)
    session.flush()

    policy_row = repo.get_or_create_policy(session, child.id)
    policy_row.weekday_quota_min = args.quota
    policy_row.weekend_quota_min = args.quota
    policy_row.end_action = args.end_action
    policy_row.lock_countdown_seconds = args.countdown
    if args.window:
        policy_row.windows = {"weekday": [args.window], "weekend": [args.window]}
    session.flush()
    return child


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a simulated child session against the real ledger and policy engine."
    )
    parser.add_argument("--minutes", type=int, default=65, help="simulated minutes of use")
    parser.add_argument("--tick", type=int, default=10, help="ledger tick size in seconds")
    parser.add_argument("--quota", type=int, default=60, help="daily quota in minutes")
    parser.add_argument("--window", default="", help="allowed window, e.g. 16:00-17:00")
    parser.add_argument(
        "--end-action",
        choices=[action.value for action in EndAction],
        default=EndAction.POWEROFF.value,
    )
    parser.add_argument(
        "--countdown", type=int, default=120, help="seconds between the lock and the end action"
    )
    parser.add_argument(
        "--suspend",
        type=int,
        nargs="?",
        const=120,
        default=0,
        metavar="MIN",
        help="suspend the machine for MIN minutes (default 120)",
    )
    parser.add_argument(
        "--reboot",
        type=int,
        nargs="?",
        const=1,
        default=0,
        metavar="MIN",
        help="reboot, off for MIN minutes (default 1)",
    )
    parser.add_argument(
        "--clock-forward",
        type=int,
        nargs="?",
        const=180,
        default=0,
        metavar="MIN",
        help="move the system clock forward MIN minutes (default 180)",
    )
    parser.add_argument(
        "--clock-back",
        type=int,
        nargs="?",
        const=60,
        default=0,
        metavar="MIN",
        help="move the system clock back MIN minutes (default 60)",
    )
    parser.add_argument(
        "--at",
        type=int,
        default=None,
        metavar="MIN",
        help="when scenarios fire (default: midway)",
    )
    parser.add_argument("--every", type=int, default=5, help="minutes between heartbeat rows")
    parser.add_argument("--verbose", action="store_true", help="one row per tick")
    parser.add_argument("--log", action="store_true", help="show the ledger's own log")
    parser.add_argument("--name", default="Elif", help="the simulated child's name")
    parser.add_argument("--locale", default="tr", choices=["tr", "en"])
    parser.add_argument(
        "--prefix",
        default=None,
        metavar="DIR",
        help="where to put the throwaway tree (default: BIOKIDS_PREFIX/tmp, or /tmp)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the temporary database")
    parser.add_argument("--no-color", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _prepare_stdout()
    args = parse_args(argv)
    _configure_logging(args.log)
    if args.tick <= 0 or args.minutes <= 0 or args.quota < 0:
        print("--minutes and --tick must be positive, --quota may not be negative")
        return 2
    if args.countdown < 0 or args.every < 0:
        print("--countdown and --every may not be negative")
        return 2
    if args.window:
        # `repo.load_policy` drops a window it cannot parse rather than refusing
        # to enforce the rest of the policy, which is right for a stored row and
        # wrong here: a mistyped --window would silently become "all day" and the
        # run would demonstrate the opposite of what was asked for.
        try:
            TimeWindow.parse(args.window)
        except ValueError as exc:
            print(f"--window: {exc}")
            return 2

    ink = Ink(
        enabled=not args.no_color and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    )
    prefix = make_scratch_root(args.prefix)
    config = build_config(prefix, args.tick)
    if prefix not in config.paths.db_path.parents:
        print(f"refusing to run: {config.paths.db_path} is not under {prefix}")
        return 2
    set_config(config)

    print("BioKids fake clock")
    print(f"  database     {config.paths.db_path}")

    clock = FakeClock(wall=_start_of_session())
    database = Database(config)
    database.create_all()
    agreed = False
    try:
        with database.session() as session:
            child = seed_child(session, args, clock.wall.date())
            policy = repo.load_policy(session, child, today=clock.wall.date())
            assert policy is not None  # we just created it
            ledger = TimeLedger(session, clock, config)
            ledger.begin()
            simulation = Simulation(
                ledger=ledger,
                clock=clock,
                policy=policy,
                events=build_events(args),
                args=args,
                ink=ink,
                voice=Voice(),
            )
            simulation.run()
            ledger.persist()
            agreed = simulation.print_summary()
    finally:
        database.dispose()
        if args.keep:
            print(f"\n  kept         {prefix}")
        else:
            shutil.rmtree(prefix, ignore_errors=True)
    # Non-zero when the ledger and the quota disagree, so this can be put in a
    # loop over tick sizes and scenarios and still be worth running.
    return 0 if agreed else 1


def _start_of_session() -> dt.datetime:
    """A Monday afternoon: a weekday quota, and far enough from midnight that a
    three-hour forward jump does not roll the calendar day on its own."""
    return dt.datetime(2026, 9, 21, 16, 0, 0)


if __name__ == "__main__":
    sys.exit(main())
