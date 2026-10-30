"""The supervisor's state, and the primitives more than one of its parts uses.

`Supervisor` is composed of mixins, one module each. They all inherit
`SupervisorBase`, which holds the fields and the helpers they share: the
database session, the ledger, speaking, locking and unlocking.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from biokids_core import repo
from biokids_core.clock import Clock, SystemClock
from biokids_core.config import Config, get_config
from biokids_core.db.session import Database
from biokids_core.ipc import EventKind
from biokids_core.ledger import TimeLedger
from biokids_core.types import Action, Category, ChildPolicy, Decision, Reason, SpeechRequest
from biokids_daemon.bus import EventBus
from biokids_daemon.dnsfilter import DnsFilter, FilterHealth
from biokids_daemon.enforcer import LOCK_ATTEMPTS_BEFORE_LOGOUT, Enforcer
from biokids_daemon.live import LiveView
from biokids_daemon.notifier import Notifier
from biokids_daemon.sessions import SessionWatcher, UserPresence

# The supervisor's parts log under one name.
log = logging.getLogger("biokids_daemon.supervisor")

ACTIVITY_TTL_SECONDS = 90
"""How long a focus report from the shell is trusted. Past this the child is
treated as having nothing in particular on screen: category limits stop
applying, the general quota does not. The launcher reports the app in front
every `biokids_shell.focus.FOCUS_REPORT_S` (D-228), so only a launcher that has
stopped reporting - or the launcher itself in front - lets a report run out."""


@dataclass
class ChildRuntime:
    """Everything the loop tracks about one child between ticks."""

    child_id: int
    unix_user: str
    display_name: str
    locale: str
    avatar: str = "fox"
    age_band: str = "4-7"
    policy: ChildPolicy | None = None

    app_slug: str = ""
    app_name: str = ""
    category: Category | None = None
    """Looked up by the daemon from the child's own apps, never taken from the
    report: the launcher says only which app is in front (D-228)."""
    app_unit: str = ""
    """The `biokids-app-*.scope` the app in front runs in, when the launcher
    could say: what is stopped when the app's kind of time runs out."""
    activity_reported_at: float = 0.0
    blocked_unit: str = ""
    """The last scope stopped for a limit. A report the launcher sampled just
    before the stop still names it, and is not taken for the app being back."""

    last_decision: Decision | None = None
    last_active: bool = False

    end_action_sessions: frozenset[str] = frozenset()
    """Which session ids the last end action was applied against. Ids rather than
    a boolean, so a child who logs back in (a fresh id) is enforced against again."""
    end_action_failures: int = 0
    end_action_next_attempt: float = 0.0
    """When a failed end action is retried, in BOOTTIME seconds."""

    paused_reason: str = ""
    """The break or timeout currently in force, so the child's applications are
    stopped once on the way in rather than on every tick."""

    lock_attempts: int = 0
    """Consecutive ticks the end-of-day lock was asked for and logind still says
    the screen is unlocked. LOCK_ATTEMPTS_BEFORE_LOGOUT of them end the session."""
    locked_for_filter: bool = False
    """BioKids locked this child because the DNS filter was open, and unlocks
    them when it is closed again."""

    earned_pending_s: int = 0
    """Earned seconds not yet announced; the celebration is in whole minutes."""
    visits_seen: dict[str, float] = field(default_factory=dict)
    """Host -> BOOTTIME of its last recorded visit, so a site that is open all
    afternoon is one line in the history, not one per page (D-187)."""
    searches_seen: dict[str, float] = field(default_factory=dict)
    """Refused searches already told to the parent, by their words (D-222)."""
    earned_announced_at: float = 0.0
    """When the last celebration went out, in BOOTTIME seconds. Zero means never."""
    together_until: float = 0.0
    """Birlikte modu (D-235): until when, in BOOTTIME seconds, a parent is at the
    computer with the child and nothing is counted. Zero is not together. Never
    written down: a daemon that restarts counts again, which is the safe side."""
    together_started_at: float = 0.0
    together_warned: bool = False
    """The "five more minutes together" line was said for this stretch."""

    def together_active(self, now_boot: float) -> bool:
        return now_boot < self.together_until

    def together_left(self, now_boot: float) -> int:
        return (
            max(0, int(self.together_until - now_boot)) if self.together_active(now_boot) else 0
        )

    def activity_fresh(self, now_boot: float) -> bool:
        return (
            bool(self.app_slug)
            and (now_boot - self.activity_reported_at) <= ACTIVITY_TTL_SECONDS
        )


@dataclass
class SupervisorBase:
    """The supervisor's fields, and the helpers more than one of its parts needs."""

    database: Database
    bus: EventBus
    watcher: SessionWatcher
    enforcer: Enforcer
    clock: Clock = field(default_factory=SystemClock)
    config: Config = field(default_factory=get_config)

    children: dict[int, ChildRuntime] = field(default_factory=dict)
    by_user: dict[str, int] = field(default_factory=dict)
    ticks: int = 0
    started: bool = False

    _session: Session | None = None
    _ledger: TimeLedger | None = None
    _dns_filter: DnsFilter | None = None
    filter_health: FilterHealth | None = None
    _filter_checked_at: float | None = None
    _notifier: Notifier | None = None
    _notified_at: float | None = None
    _pruned_at: float | None = None
    _assistant_checked_at: float | None = None
    _voice_checked_at: float | None = None
    assistant_ready: dict[str, bool] = field(default_factory=dict)
    """What the assistant service last said it can do: `chat` (a model is
    downloaded) and `voice` (speech recognition too). Empty when it is not
    installed or did not answer, and then the launcher shows no "Sor Bana"
    tile: a tile that can only say "I'm resting" is a broken toy (D-198)."""
    live: LiveView = field(default_factory=LiveView)
    """Who is watching which child's screen, and the latest picture (D-253)."""
    _afterwards: set[asyncio.Task[None]] = field(default_factory=set)
    """Work answered for before it was done (D-209): switching off, restarting,
    ending a child's session. Held here so a task is not collected half-way, and
    so stop() can cancel what is left before the database closes under it."""

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("supervisor.start() has not been called")
        return self._session

    @property
    def ledger(self) -> TimeLedger:
        if self._ledger is None:
            raise RuntimeError("supervisor.start() has not been called")
        return self._ledger

    def reload(self) -> None:  # pragma: no cover - replaced by Supervisor.reload
        """Re-read children and policies. `Supervisor` defines it; declared here
        so that the parts can call it."""
        raise NotImplementedError

    def _require(self, child_id: int) -> ChildRuntime:
        runtime = self.children.get(child_id)
        if runtime is None:
            raise KeyError(f"no such child: {child_id}")
        return runtime

    # ------------------------------------------------------------ afterwards

    def _after_answering(self, work: Coroutine[Any, Any, None], name: str) -> None:
        """Do `work` once the caller has been answered (D-209).

        For what takes longer than anyone waits for an answer: every open
        application gets its grace period before the machine goes, ten seconds
        by default, and the launcher and the panel give up on the daemon after
        five. A failure is logged here; the work itself decides what to do
        about one, because only it knows what "fail safe" means for it.
        """
        task = asyncio.create_task(work, name=name)
        self._afterwards.add(task)
        task.add_done_callback(self._afterwards_done)

    def _afterwards_done(self, task: asyncio.Task[None]) -> None:
        self._afterwards.discard(task)
        if task.cancelled():
            log.warning(
                "%s was cancelled: the daemon stopped before it was done", task.get_name()
            )
            return
        error = task.exception()
        if error is not None:
            log.error("%s failed: %s", task.get_name(), error, exc_info=error)

    def _under_way(self, *names: str) -> bool:
        """True while work by one of these names has been answered for and not done."""
        return any(task.get_name() in names for task in self._afterwards)

    async def _cancel_afterwards(self) -> None:
        """Cancel what has not happened yet, and wait until it has stopped.

        Cancelled rather than waited for: a daemon stopped for an upgrade must
        not switch the machine off half-way through it. The child's session goes
        on, and a restarted daemon supervises it as before.
        """
        pending = [task for task in self._afterwards if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._afterwards.clear()

    # ---------------------------------------------------------------- speech

    def _decided(self, runtime: ChildRuntime, decision: Decision, now_boot: float) -> Decision:
        """The engine's decision, or "allowed" while a parent is with the child.

        Birlikte modu (D-235) is a parent's word, not a rule of the engine, so
        it is applied here, to every decision the daemon acts on or shows: the
        tick, the launcher's state, Today, and a tap on a tile.
        """
        if not runtime.together_active(now_boot):
            return decision
        return Decision(
            allow=True,
            action=Action.NONE,
            reason_code=Reason.TOGETHER,
            remaining_seconds=decision.remaining_seconds,
        )

    def _publish_speech(self, runtime: ChildRuntime, decision: Decision) -> None:
        """Ask for a line. The daemon never speaks; the shell relays to the voice
        service, which runs inside the child's session where the audio is."""
        if decision.speak is not None:
            self._speak(runtime, decision.speak)

    def _speak(self, runtime: ChildRuntime, speak: SpeechRequest) -> None:
        """The one place a spoken line leaves the daemon, so that `voice.enabled`
        and the per-child switch govern every line."""
        if not self.config.voice.enabled:
            return
        if runtime.policy is not None and not runtime.policy.voice_enabled:
            # The per-child switch: the card still shows, the sound stops.
            return
        self.bus.publish(
            EventKind.SPEAK,
            child_id=runtime.child_id,
            event=speak.event,
            locale=runtime.locale,
            vars=dict(speak.vars),
            priority=speak.priority,
            expression=speak.expression,
            persona=speak.persona,
        )

    # ------------------------------------------------------ locks and sessions

    def _others_logged_in(self, runtime: ChildRuntime) -> tuple[str, ...]:
        """Which other human users have a session right now.

        Everyone with a `user` session on a seat who is not this child. Another
        child counts: ending Elif's day must not log Deniz out mid-drawing
        either. A login with no seat does not: an ssh session is a grown-up at
        another machine, not a person at this one, and counting it kept the
        computer on when the child pressed "Kapat" (D-209).
        """
        others: list[str] = []
        for unix_user, presence in self.watcher.all_presence.items():
            if unix_user == runtime.unix_user:
                continue
            if any(
                session.seat and session.session_class == "user" and session.is_person
                for session in presence.sessions
            ):
                others.append(unix_user)
        return tuple(sorted(others))

    async def _unlock(self, runtime: ChildRuntime) -> None:
        """Let the child back into a session BioKids locked.

        Child accounts have no password (D-99), so a lock nobody lifts is a child
        who cannot get back in at all.
        """
        presence = self.watcher.presence(runtime.unix_user)
        # Every session with a screen, not only those logind calls locked:
        # light-locker never sets LockedHint (D-155). Unlocking a session that is
        # not locked is a no-op.
        locked = list(presence.lockable_session_ids)
        if not locked:
            return
        ok = await self.enforcer.unlock_sessions(tuple(locked))
        log.info("unlocked %s session(s) for %s: %s", len(locked), runtime.unix_user, ok)
        repo.audit(
            self.session,
            actor="daemon",
            action="session_unlocked",
            target=f"child:{runtime.child_id}",
            detail={"sessions": len(locked), "ok": ok},
        )

    async def _lock(
        self, runtime: ChildRuntime, presence: UserPresence, *, only_unlocked: bool = False
    ) -> None:
        targets = (
            presence.relockable_session_ids if only_unlocked else presence.lockable_session_ids
        )
        if not targets:
            runtime.lock_attempts = 0
            return
        await self.enforcer.lock_sessions(targets)
        if not only_unlocked:
            return
        # Whether the last lock took is read from this tick's snapshot. Not from
        # LockedHint alone: light-locker never sets it (D-155) and instead makes
        # the session inactive. A session still active and unlocked means no
        # screen locker answered `loginctl lock-session`; ending it is the
        # fail-safe.
        on_screen = [
            s.session_id
            for s in presence.sessions
            if s.session_id in targets and s.active and not s.locked
        ]
        if not on_screen:
            runtime.lock_attempts = 0
            return
        runtime.lock_attempts += 1
        if runtime.lock_attempts <= LOCK_ATTEMPTS_BEFORE_LOGOUT:
            return
        targets = tuple(on_screen)
        ended = [sid for sid in targets if await self.enforcer.terminate_session(sid)]
        runtime.lock_attempts = 0
        log.error(
            "%s's screen did not lock after %s attempts; ended %s",
            runtime.unix_user,
            LOCK_ATTEMPTS_BEFORE_LOGOUT,
            ", ".join(ended) or "nothing",
        )
        repo.audit(
            self.session,
            actor="daemon",
            action="lock_failed",
            target=f"child:{runtime.child_id}",
            detail={"sessions": list(targets), "ended": ended},
            severity="alert",
        )
