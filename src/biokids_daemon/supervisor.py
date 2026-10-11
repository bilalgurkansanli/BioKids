"""The main loop.

    tick -> update ledger -> evaluate -> act -> publish

and nothing else. The loop contains no policy logic of its own (T3.4): it wires
four tested things together. If you find yourself adding an `if` about quotas
here, it belongs in `biokids_core.policy`.

`Supervisor` is assembled from one mixin per concern:

    base.py             shared state; speaking, locking and unlocking
    filtering.py        the kiosk browser policy and the DNS filter (D-167, D-172)
    end_sequence.py     lock, countdown, end action (ARCHITECTURE.md §3.5)
    operations.py       what the panel and the launcher ask the daemon to do
    access_requests.py  "Anneme sor" (D-65, D-173)
    views.py            status() for the panel, shell_state() for the launcher
    loop.py             run_forever, and the fail-safe when ticks keep failing

This module keeps the lifecycle, the tick, and what a tick does with each
decision. The names other modules import from here are re-exported; a constant
is monkeypatched in the module that defines it, because that is where it is read.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from biokids_core import repo, retention
from biokids_core.clock import GuardedClock
from biokids_core.ipc import EventKind
from biokids_core.ledger import TickInput, TimeLedger
from biokids_core.policy import evaluate
from biokids_core.types import Action, ActivityContext, Decision, SpeechRequest
from biokids_daemon.access_requests import (
    REQUEST_EXPIRY,
    REQUEST_KINDS,
    REQUESTS_PER_HOUR,
    AccessRequestsMixin,
)
from biokids_daemon.base import ACTIVITY_TTL_SECONDS, ChildRuntime, SupervisorBase
from biokids_daemon.end_sequence import (
    END_ACTION_BACKOFF_SECONDS,
    END_ACTION_GIVE_UP_AFTER,
    TERMINAL_LOCK,
    EndSequenceMixin,
)
from biokids_daemon.filtering import (
    FILTER_CHECK_SECONDS,
    FILTER_SETTLE_PAUSE,
    FILTER_SETTLE_SECONDS,
    FILTER_SETTLE_TRIES,
    FilteringMixin,
    web_tile_pattern,
)
from biokids_daemon.loop import (
    TICK_FAILURES_BEFORE_GIVING_UP,
    TICK_TIMEOUT_SECONDS,
    run_forever,
)
from biokids_daemon.notifier import Notifier
from biokids_daemon.operations import OperationsMixin
from biokids_daemon.sessions import UserPresence
from biokids_daemon.views import ViewsMixin

__all__ = [
    "ACTIVITY_TTL_SECONDS",
    "EARN_SPEECH_INTERVAL",
    "END_ACTION_BACKOFF_SECONDS",
    "END_ACTION_GIVE_UP_AFTER",
    "FILTER_CHECK_SECONDS",
    "FILTER_SETTLE_PAUSE",
    "FILTER_SETTLE_SECONDS",
    "FILTER_SETTLE_TRIES",
    "REQUESTS_PER_HOUR",
    "REQUEST_EXPIRY",
    "REQUEST_KINDS",
    "TERMINAL_LOCK",
    "TICK_FAILURES_BEFORE_GIVING_UP",
    "TICK_TIMEOUT_SECONDS",
    "ChildRuntime",
    "Supervisor",
    "run_forever",
    "web_tile_pattern",
]

log = logging.getLogger(__name__)

ASSISTANT_CHECK_SECONDS = 300
"""How often a ready assistant service is asked whether it still is (D-198)."""
ASSISTANT_RETRY_SECONDS = 20
"""How often one that is not ready is asked again. At boot the daemon asks before
biokids-ai has opened its socket, and at five minutes a child who had just logged
in found no "Sor Bana" tile until the next check."""
PRUNE_EVERY_SECONDS = 24 * 3600
PRUNE_FIRST_AFTER_SECONDS = 300

EARN_SPEECH_INTERVAL = 300
"""How rarely the earning celebration may fire - banner and spoken line
together. Interrupting a child every ten seconds to praise them for earning zero
minutes is how a reward becomes a nuisance, and D-45 asks for a small one."""


@dataclass
class Supervisor(
    ViewsMixin,
    AccessRequestsMixin,
    OperationsMixin,
    EndSequenceMixin,
    FilteringMixin,
    SupervisorBase,
):
    """The daemon's brain-stem. Owns nothing clever, forgets nothing important."""

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._session = self.database.factory()
        self._ledger = TimeLedger(self._session, self.clock, self.config)
        reading = self._ledger.begin()
        # reload() also writes the kiosk policy and the DNS filter.
        self.reload()
        await self.watcher.start()
        self.started = True

        repo.audit(
            self._session,
            actor="daemon",
            action="daemon_started",
            detail={"boot_id": reading.boot_id, "children": len(self.children)},
        )
        self._session.commit()
        self.bus.publish(EventKind.DAEMON_STATE, state="started", children=len(self.children))
        log.info("supervisor started with %s child/children", len(self.children))

    async def stop(self) -> None:
        self.started = False
        if self._ledger is not None:
            self._ledger.persist()
        if self._session is not None:
            repo.audit(self._session, actor="daemon", action="daemon_stopped")
            self._session.commit()
            self._session.close()
            self._session = None
        await self.watcher.stop()
        self.bus.publish(EventKind.DAEMON_STATE, state="stopped")

    def reload(self) -> None:
        """Re-read children and policies. Called at start and on a `reload` op.

        The panel writes the same database from another process, so a policy
        change has to be re-read to take effect within one tick - but not on
        every tick. A reload that arrives before `start()` is ignored: the panel
        may finish the wizard a moment before the daemon is ready, and `start()`
        reads the policies itself.
        """
        if self._session is None:
            log.info("reload arrived before the daemon finished starting; ignoring")
            return
        self.session.expire_all()
        today = self.clock.read().wall.date()
        fresh: dict[int, ChildRuntime] = {}
        by_user: dict[str, int] = {}

        for child in repo.list_children(self.session):
            previous = self.children.get(child.id)
            runtime = ChildRuntime(
                child_id=child.id,
                unix_user=child.unix_user,
                display_name=child.display_name,
                locale=child.locale,
                avatar=child.avatar,
                age_band=child.band(today).value,
                policy=repo.load_policy(self.session, child, today=today),
            )
            if previous is not None:
                runtime.app_slug = previous.app_slug
                runtime.app_name = previous.app_name
                runtime.category = previous.category
                runtime.activity_reported_at = previous.activity_reported_at
                runtime.last_decision = previous.last_decision
                runtime.last_active = previous.last_active
                runtime.end_action_sessions = previous.end_action_sessions
                runtime.end_action_failures = previous.end_action_failures
                runtime.end_action_next_attempt = previous.end_action_next_attempt
                runtime.paused_reason = previous.paused_reason
                runtime.earned_pending_s = previous.earned_pending_s
                runtime.earned_announced_at = previous.earned_announced_at
            fresh[child.id] = runtime
            by_user[child.unix_user] = child.id

        for gone in set(self.children) - set(fresh):
            self.ledger.forget(gone)

        self.children = fresh
        self.by_user = by_user
        self._write_kiosk_policy()
        self._apply_dns_filter()
        self.bus.publish(EventKind.POLICY_CHANGED, children=len(fresh))

    # ----------------------------------------------------------------- tick

    async def tick(self) -> list[Decision]:
        """One pass. Everything that happens to a child happens here."""
        await self.watcher.refresh()
        reading = self.clock.read()
        self.ticks += 1

        inputs: list[TickInput] = []
        presence: dict[int, UserPresence] = {}
        for runtime in self.children.values():
            seen = self.watcher.presence(runtime.unix_user)
            presence[runtime.child_id] = seen
            inputs.append(
                TickInput(
                    child_id=runtime.child_id,
                    active=seen.active and not self.watcher.suspending,
                    idle=seen.idle,
                    category=runtime.category if runtime.activity_fresh(reading.boot) else None,
                    app_slug=runtime.app_slug if runtime.activity_fresh(reading.boot) else "",
                    policy=runtime.policy,
                )
            )

        results = self.ledger.tick(inputs)
        self._audit_anomalies()

        decisions: list[Decision] = []
        for result in results:
            runtime = self.children[result.child_id]
            seen = presence[runtime.child_id]

            if result.gap_seconds:
                repo.record_event(
                    self.session,
                    kind="usage_gap",
                    child_id=runtime.child_id,
                    ts_boot=reading.boot,
                    payload={"gap_seconds": result.gap_seconds},
                )
            if result.earned_seconds:
                self._on_earned(runtime, result.earned_seconds, reading.boot)
            if result.break_started:
                self.bus.publish(EventKind.BREAK_STARTED, child_id=runtime.child_id)
            if result.break_finished:
                self.bus.publish(EventKind.BREAK_FINISHED, child_id=runtime.child_id)
            if result.penalty_finished:
                self.bus.publish(EventKind.TIMEOUT_FINISHED, child_id=runtime.child_id)

            state = result.state or self.ledger.state(runtime.child_id, result.day)
            # The same freshness test the ledger got, so the engine does not keep
            # applying the limit of an app the child has already left.
            fresh = runtime.activity_fresh(reading.boot)
            decision = evaluate(
                runtime.policy,
                state,
                ActivityContext(
                    app_slug=(runtime.app_slug or None) if fresh else None,
                    category=runtime.category if fresh else None,
                    app_allowed=True,
                    session_active=seen.active,
                ),
                reading.wall,
            )
            decisions.append(decision)

            # Re-armed every tick: a grant, a quota edit, a widened window or a
            # finished break can each push the end of the day back, and the
            # warnings now in the future must fire again. The ledger only marks
            # the child dirty when the set actually changes.
            self.ledger.rearm_warnings(runtime.child_id, result.day, decision.remaining_seconds)
            await self._act(runtime, seen, decision, result.day, reading.boot)
            self._publish_tick(runtime, seen, decision, state)
            runtime.last_decision = decision
            runtime.last_active = seen.active

        self.ledger.persist(reading)
        return decisions

    # ------------------------------------------------------------- actions

    async def _act(
        self,
        runtime: ChildRuntime,
        presence: UserPresence,
        decision: Decision,
        day: dt.date,
        now_boot: float,
    ) -> None:
        if self.watcher.suspending:
            # The machine is going to sleep. Locking a session mid-suspend
            # achieves nothing and races the suspend itself.
            return

        terminal = decision.action is Action.LOCK and decision.reason_code in TERMINAL_LOCK

        # Anything that is not the end of the day closes the countdown, not just
        # an outright `NONE`.
        if not terminal:
            self._clear_end_sequence(runtime, day, decision.remaining_seconds)
        if decision.action not in (Action.LOCK, Action.TIMEOUT) or terminal:
            runtime.paused_reason = ""

        match decision.action:
            case Action.WARN:
                self._speak_warning(runtime, decision, day)
            case Action.LOCK if terminal:
                await self._run_end_sequence(runtime, presence, decision, day, now_boot)
            case Action.LOCK | Action.TIMEOUT:
                await self._enter_pause(runtime, decision)
            case Action.BLOCK:
                self.bus.publish(
                    EventKind.BLOCKED,
                    child_id=runtime.child_id,
                    reason_code=decision.reason_code,
                    app=runtime.app_slug,
                )
            case _:
                pass

    async def _enter_pause(self, runtime: ChildRuntime, decision: Decision) -> None:
        """A break or a timeout: pause the child, do not lock them out.

        The session is deliberately not locked: `loginctl lock-session` hands a
        four-year-old a password prompt nothing in BioKids can unlock. The
        launcher draws the calm full-screen timer (UX.md §2.5); what has to
        happen here is stopping the applications, once, because the launcher
        would otherwise render behind a fullscreen game.
        """
        if runtime.paused_reason == decision.reason_code:
            return
        runtime.paused_reason = decision.reason_code

        kind = (
            EventKind.TIMEOUT_STARTED
            if decision.action is Action.TIMEOUT
            else EventKind.BREAK_STARTED
        )
        self.bus.publish(kind, child_id=runtime.child_id, reason_code=decision.reason_code)
        if decision.speak is not None:
            self._publish_speech(runtime, decision)
        await self.enforcer.stop_all_apps(runtime.unix_user)

    def _speak_warning(self, runtime: ChildRuntime, decision: Decision, day: dt.date) -> None:
        """Say each threshold once. The engine has no memory; the ledger does."""
        if decision.speak is None:
            return
        threshold = int(decision.speak.vars.get("minutes", 0) or 0)
        if threshold and self.ledger.already_warned(runtime.child_id, day, threshold):
            return
        if threshold:
            self.ledger.mark_warned(runtime.child_id, day, threshold)

        self.bus.publish(
            EventKind.WARNING,
            child_id=runtime.child_id,
            reason_code=decision.reason_code,
            remaining_seconds=decision.remaining_seconds,
            minutes=threshold,
        )
        self._publish_speech(runtime, decision)
        repo.audit(
            self.session,
            actor="daemon",
            action="warning_spoken",
            target=f"child:{runtime.child_id}",
            detail={"minutes": threshold, "remaining_seconds": decision.remaining_seconds},
        )

    def _on_earned(self, runtime: ChildRuntime, seconds: int, now_boot: float) -> None:
        """Celebrate earned minutes - once they are minutes, and not every tick.

        `seconds` is this tick's payout, usually ten seconds. Payouts accumulate
        and are announced in whole minutes, no more often than
        EARN_SPEECH_INTERVAL, as one celebration: banner and voice together.
        """
        runtime.earned_pending_s += seconds
        minutes = runtime.earned_pending_s // 60
        if minutes < 1:
            return
        # The first celebration is never throttled: it is the payout that clears
        # the streak threshold, the one the child is actually waiting for.
        since = now_boot - runtime.earned_announced_at
        if runtime.earned_announced_at and since < EARN_SPEECH_INTERVAL:
            return
        runtime.earned_pending_s -= minutes * 60
        runtime.earned_announced_at = now_boot

        # The banner, which the child sees whether or not the voice is on: a
        # parent turning off the sound is not turning off the reward.
        self.bus.publish(
            EventKind.EARNED,
            child_id=runtime.child_id,
            seconds=minutes * 60,
            minutes=minutes,
        )
        self._speak(
            runtime,
            SpeechRequest(
                event="earned",
                vars={"name": runtime.display_name, "minutes": minutes},
                priority="normal",
                expression="happy",
            ),
        )

    def _publish_tick(
        self,
        runtime: ChildRuntime,
        presence: UserPresence,
        decision: Decision,
        state: Any,
    ) -> None:
        self.bus.publish(
            EventKind.TICK,
            child_id=runtime.child_id,
            remaining_seconds=decision.remaining_seconds,
            remaining_minutes=decision.remaining_minutes,
            used_seconds=state.general_used_s,
            allow=decision.allow,
            action=decision.action.value,
            reason_code=decision.reason_code,
            online=presence.active,
            app=runtime.app_name or runtime.app_slug,
        )

    def _audit_anomalies(self) -> None:
        if isinstance(self.clock, GuardedClock):
            for correction in self.clock.take_corrections():
                repo.audit(
                    self.session,
                    actor="system",
                    action="clock_jump",
                    detail={
                        "delta_seconds": round(correction.delta_seconds),
                        "direction": "forward" if correction.delta_seconds > 0 else "back",
                        # Not trusted: the day and the time windows carry on from
                        # the old clock until NTP or a restart confirms the new one.
                        "ignored": not correction.trusted,
                    },
                    severity="warning",
                )
                log.warning(
                    "the wall clock moved %+d s; %s",
                    round(correction.delta_seconds),
                    "NTP confirms it"
                    if correction.trusted
                    else "ignored until NTP confirms it",
                )
        for anomaly in self.ledger.take_anomalies():
            repo.audit(
                self.session,
                actor="system",
                action="clock_jump",
                detail={
                    "delta_seconds": round(anomaly.delta_seconds),
                    "direction": anomaly.direction,
                    "from": anomaly.wall_before.isoformat(timespec="seconds"),
                    "to": anomaly.wall_after.isoformat(timespec="seconds"),
                },
                severity="warning",
            )
            self.bus.publish(
                EventKind.CLOCK_JUMP,
                delta_seconds=round(anomaly.delta_seconds),
                direction=anomaly.direction,
            )
            log.warning(
                "system clock moved %s by %s s; the ledger is unaffected",
                anomaly.direction,
                round(abs(anomaly.delta_seconds)),
            )

    # ------------------------------------------------------- parent mail

    async def housekeeping(self, now_boot: float) -> None:
        """Once a day, delete what is past its retention window (D-189). The
        first run is a few minutes after start, not during it."""
        if self._pruned_at is None:
            self._pruned_at = now_boot - PRUNE_EVERY_SECONDS + PRUNE_FIRST_AFTER_SECONDS
            return
        if now_boot - self._pruned_at < PRUNE_EVERY_SECONDS:
            return
        self._pruned_at = now_boot
        try:
            retention.prune(self.session, self.config)
            self.session.commit()
        except Exception:
            log.exception("old records could not be deleted; trying again tomorrow")
            with contextlib.suppress(Exception):
                self.session.rollback()

    async def refresh_assistant(self, now_boot: float) -> None:
        """Ask the assistant service what it can do, every few minutes (D-198).

        Its socket exists only where biokids-ai is installed; without it there
        is nothing to ask. The answer decides whether the launcher shows the
        "Sor Bana" tile at all.
        """
        checked = self._assistant_checked_at
        ready = bool(self.assistant_ready.get("chat"))
        wait = ASSISTANT_CHECK_SECONDS if ready else ASSISTANT_RETRY_SECONDS
        if checked is not None and now_boot - checked < wait:
            return
        self._assistant_checked_at = now_boot
        from biokids_daemon.assistant import AI_SOCKET_NAME, AssistantGate

        if not (Path(self.config.paths.ai_run_dir) / AI_SOCKET_NAME).exists():
            self.assistant_ready = {}
            return
        status = await AssistantGate(self, self.bus, self.config).status()
        self.assistant_ready = (
            {"chat": bool(status.get("ready")), "voice": bool(status.get("voice"))}
            if status.get("running")
            else {}
        )

    async def notify_parent(self, now_boot: float) -> None:
        """Mail what the parent should know, once a minute (D-177). Never raises:
        a mail server that is down is not a reason to stop supervising."""
        if (
            self._notified_at is not None
            and now_boot - self._notified_at < FILTER_CHECK_SECONDS
        ):
            return
        self._notified_at = now_boot
        if self._notifier is None:
            self._notifier = Notifier(self.config)
        try:
            await self._notifier.run(self.session, self.clock.read().wall, now_boot)
        except Exception:
            log.exception("the parent's mail could not be prepared")
            with contextlib.suppress(Exception):
                self.session.rollback()
