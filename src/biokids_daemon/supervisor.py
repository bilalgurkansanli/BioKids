"""The main loop.

    tick -> update ledger -> evaluate -> act -> publish

and nothing else. The loop contains no policy logic of its own (T3.4): it wires
four tested things together. If you find yourself adding an `if` about quotas
here, it belongs in `biokids_core.policy`.

The one piece of genuine state the loop owns is the **end-of-time sequence**
(`ARCHITECTURE.md` §3.5). The policy engine can say "quota exhausted" but not
"the lock screen has been up for 118 seconds", because that is history, not a
rule. So the loop remembers when the lock went up — in the database, not in
memory, so a daemon restart mid-countdown resumes rather than restarting the two
minutes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from biokids_core import repo
from biokids_core.clock import Clock, SystemClock
from biokids_core.config import Config, get_config
from biokids_core.db.models import AccessRequest
from biokids_core.db.session import Database
from biokids_core.ipc import EventKind
from biokids_core.ledger import TickInput, TimeLedger
from biokids_core.policy import evaluate
from biokids_core.types import (
    Action,
    ActivityContext,
    Category,
    ChildPolicy,
    Decision,
    EndAction,
    Reason,
)
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer
from biokids_daemon.sessions import SessionWatcher, UserPresence

log = logging.getLogger(__name__)

ACTIVITY_TTL_SECONDS = 90
"""How long a focus report from the shell is trusted. Past this the child is
treated as having nothing in particular on screen, which is the conservative
reading: category limits stop applying, the general quota does not."""


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
    activity_reported_at: float = 0.0

    last_decision: Decision | None = None
    last_active: bool = False
    end_action_applied: bool = False
    """Set once the configured end action has fired, so it fires exactly once."""

    def activity_fresh(self, now_boot: float) -> bool:
        return (
            bool(self.app_slug)
            and (now_boot - self.activity_reported_at) <= ACTIVITY_TTL_SECONDS
        )


@dataclass
class Supervisor:
    """The daemon's brain-stem. Owns nothing clever, forgets nothing important."""

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

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._session = self.database.factory()
        self._ledger = TimeLedger(self._session, self.clock, self.config)
        reading = self._ledger.begin()
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

    def reload(self) -> None:
        """Re-read children and policies. Called at start and on a `reload` op.

        A policy change from the panel must take effect within one tick, and the
        panel is a separate process writing the same database, so the daemon
        cannot cache indefinitely. It also must not re-query on every tick — that
        is a database round trip per child per ten seconds forever.
        """
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
                runtime.end_action_applied = previous.end_action_applied
            fresh[child.id] = runtime
            by_user[child.unix_user] = child.id

        for gone in set(self.children) - set(fresh):
            self.ledger.forget(gone)

        self.children = fresh
        self.by_user = by_user
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
                self._on_earned(runtime, result.earned_seconds)
            if result.break_started:
                self.bus.publish(EventKind.BREAK_STARTED, child_id=runtime.child_id)
            if result.break_finished:
                self.bus.publish(EventKind.BREAK_FINISHED, child_id=runtime.child_id)
            if result.penalty_finished:
                self.bus.publish(EventKind.TIMEOUT_FINISHED, child_id=runtime.child_id)

            state = result.state or self.ledger.state(runtime.child_id, result.day)
            decision = evaluate(
                runtime.policy,
                state,
                ActivityContext(
                    app_slug=runtime.app_slug or None,
                    category=runtime.category,
                    app_allowed=True,
                    session_active=seen.active,
                ),
                reading.wall,
            )
            decisions.append(decision)
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

        match decision.action:
            case Action.WARN:
                self._speak_warning(runtime, decision, day)
            case Action.LOCK:
                await self._run_end_sequence(runtime, presence, decision, day, now_boot)
            case Action.TIMEOUT:
                self.bus.publish(
                    EventKind.TIMEOUT_STARTED,
                    child_id=runtime.child_id,
                    reason_code=decision.reason_code,
                )
                await self._lock(runtime, presence)
            case Action.BLOCK:
                self.bus.publish(
                    EventKind.BLOCKED,
                    child_id=runtime.child_id,
                    reason_code=decision.reason_code,
                    app=runtime.app_slug,
                )
            case Action.NONE:
                self._clear_end_sequence(runtime, day, decision.remaining_seconds)
            case _:
                pass

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

    async def _run_end_sequence(
        self,
        runtime: ChildRuntime,
        presence: UserPresence,
        decision: Decision,
        day: dt.date,
        now_boot: float,
    ) -> None:
        """Lock, count down, then do what the parent chose (`ARCHITECTURE.md` §3.5).

        A break or a penalty locks too, but it must never reach the end action —
        the machine does not shut down because a child owes five minutes of rest.
        """
        policy = runtime.policy
        terminal = decision.reason_code in {Reason.QUOTA_EXHAUSTED, Reason.WINDOW_CLOSED}

        started = self.ledger.end_sequence_started_at(runtime.child_id, day)
        if started is None:
            self.ledger.start_end_sequence(runtime.child_id, day, now_boot)
            started = now_boot
            runtime.end_action_applied = False
            await self._lock(runtime, presence)
            if decision.speak is not None:
                self._publish_speech(runtime, decision)
            if terminal:
                repo.audit(
                    self.session,
                    actor="daemon",
                    action="quota_exhausted",
                    target=f"child:{runtime.child_id}",
                    detail={"reason": decision.reason_code},
                )

        countdown = (
            policy.lock_countdown_seconds
            if policy is not None
            else self.config.daemon.lock_countdown_seconds
        )
        elapsed = max(0, int(now_boot - started))
        self.bus.publish(
            EventKind.END_SEQUENCE,
            child_id=runtime.child_id,
            reason_code=decision.reason_code,
            countdown_seconds=max(0, countdown - elapsed),
            end_action=(policy.end_action.value if policy else EndAction.LOCK.value),
        )

        if not terminal or runtime.end_action_applied or elapsed < countdown:
            return

        runtime.end_action_applied = True
        action = policy.end_action if policy is not None else EndAction.LOCK
        # Applications first, so an unsaved drawing gets its SIGTERM before the
        # machine starts going down.
        await self.enforcer.stop_all_apps(runtime.unix_user)
        ok = await self.enforcer.apply_end_action(
            action, runtime.unix_user, presence.lockable_session_ids
        )
        repo.audit(
            self.session,
            actor="daemon",
            action=action.value if action is not EndAction.LOCK else "session_locked",
            target=f"child:{runtime.child_id}",
            detail={"reason": decision.reason_code, "ok": ok},
            severity="warning",
        )
        log.info("end action %s for %s: %s", action.value, runtime.unix_user, ok)

    def _clear_end_sequence(self, runtime: ChildRuntime, day: dt.date, remaining: int) -> None:
        if self.ledger.end_sequence_started_at(runtime.child_id, day) is not None:
            self.ledger.clear_end_sequence(runtime.child_id, day)
            runtime.end_action_applied = False
            self.ledger.rearm_warnings(runtime.child_id, day, remaining)
            self.bus.publish(EventKind.END_SEQUENCE, child_id=runtime.child_id, cancelled=True)

    async def _lock(self, runtime: ChildRuntime, presence: UserPresence) -> None:
        if not presence.lockable_session_ids:
            return
        await self.enforcer.lock_sessions(presence.lockable_session_ids)

    def _on_earned(self, runtime: ChildRuntime, seconds: int) -> None:
        minutes = seconds // 60
        self.bus.publish(
            EventKind.EARNED, child_id=runtime.child_id, seconds=seconds, minutes=minutes
        )
        self.bus.publish(
            EventKind.SPEAK,
            child_id=runtime.child_id,
            event="earned",
            locale=runtime.locale,
            vars={"name": runtime.display_name, "minutes": minutes},
            priority="normal",
            expression="happy",
        )

    def _publish_speech(self, runtime: ChildRuntime, decision: Decision) -> None:
        """Ask for a line. The daemon never speaks; the shell relays to the voice
        service, which runs inside the child's session where the audio is."""
        speak = decision.speak
        if speak is None:
            return
        policy = runtime.policy
        if policy is not None and not self.config.voice.enabled:
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

    # ------------------------------------------------------- operations

    def status(self) -> dict[str, Any]:
        """Everything the Today screen needs, for every child, in one call."""
        reading = self.clock.read()
        day = reading.wall.date()
        children: list[dict[str, Any]] = []

        for runtime in self.children.values():
            presence = self.watcher.presence(runtime.unix_user)
            state = self.ledger.state(runtime.child_id, day)
            decision = evaluate(
                runtime.policy,
                state,
                ActivityContext(
                    app_slug=runtime.app_slug or None,
                    category=runtime.category,
                    session_active=presence.active,
                ),
                reading.wall,
            )
            policy = runtime.policy
            children.append(
                {
                    "child_id": runtime.child_id,
                    "unix_user": runtime.unix_user,
                    "display_name": runtime.display_name,
                    "avatar": runtime.avatar,
                    "locale": runtime.locale,
                    "age_band": runtime.age_band,
                    "managed": policy is not None,
                    "online": presence.active,
                    "present": presence.present,
                    "idle": presence.idle,
                    "app": runtime.app_name or runtime.app_slug,
                    "app_slug": runtime.app_slug,
                    "category": runtime.category.value if runtime.category else "",
                    "remaining_seconds": decision.remaining_seconds,
                    "remaining_minutes": decision.remaining_minutes,
                    "used_seconds": state.general_used_s,
                    "quota_seconds": (policy.quota_minutes(day) * 60) if policy else 0,
                    "earned_seconds": state.earned_s,
                    "granted_seconds": state.granted_s,
                    "penalty_seconds": state.penalty_remaining_s,
                    "break_seconds": state.break_remaining_s,
                    "allow": decision.allow,
                    "action": decision.action.value,
                    "reason_code": decision.reason_code,
                    "end_action": policy.end_action.value if policy else "",
                }
            )

        return {
            "ticks": self.ticks,
            "day": day.isoformat(),
            "children": children,
        }

    def shell_state(self, child_id: int) -> dict[str, Any]:
        """Everything the child's launcher draws, in one call.

        The launcher runs as the child and cannot read the database — it is 0600
        root — so this is where identity, tiles and remaining time come from. It
        is also why the launcher can be trusted to render whatever it is given:
        it has no way to invent a number.
        """
        runtime = self._require(child_id)
        reading = self.clock.read()
        day = reading.wall.date()
        presence = self.watcher.presence(runtime.unix_user)
        state = self.ledger.state(child_id, day)
        policy = runtime.policy

        decision = evaluate(
            policy,
            state,
            ActivityContext(
                app_slug=runtime.app_slug or None,
                category=runtime.category,
                session_active=True,
            ),
            reading.wall,
        )

        child = repo.get_child(self.session, child_id)
        tiles: list[dict[str, Any]] = []
        if child is not None:
            for link, app in repo.list_child_apps(self.session, child_id):
                if app.kind == "__gone__":
                    continue
                tiles.append(
                    {
                        "slug": app.slug,
                        "name": app.name(runtime.locale),
                        "description": app.description(runtime.locale),
                        "icon": app.icon,
                        "kind": app.kind,
                        "url": app.url,
                        "exec": app.exec_cmd,
                        "category": repo.child_app_category(link, app).value,
                    }
                )

        quota = policy.quota_minutes(day) * 60 if policy else 0
        return {
            "child_id": child_id,
            "display_name": runtime.display_name,
            "avatar": runtime.avatar,
            "locale": runtime.locale,
            "age_band": runtime.age_band,
            "managed": policy is not None,
            "supervision_indicator": child.supervision_indicator if child else "badge",
            "first_run_done": bool(child.first_run_done) if child else True,
            "tiles": tiles,
            "remaining_seconds": decision.remaining_seconds,
            "remaining_minutes": decision.remaining_minutes,
            "quota_seconds": quota + state.granted_s,
            "used_seconds": state.general_used_s,
            "earned_seconds": state.earned_s,
            "break_seconds": state.break_remaining_s,
            "penalty_seconds": state.penalty_remaining_s,
            "allow": decision.allow,
            "action": decision.action.value,
            "reason_code": decision.reason_code,
            "idle": presence.idle,
            "countdown_seconds": self._countdown_remaining(runtime, day, now_boot=reading.boot),
        }

    def _countdown_remaining(self, runtime: ChildRuntime, day: dt.date, now_boot: float) -> int:
        """Seconds left on the lock screen before the end action, or zero."""
        started = self.ledger.end_sequence_started_at(runtime.child_id, day)
        if started is None:
            return 0
        total = (
            runtime.policy.lock_countdown_seconds
            if runtime.policy is not None
            else self.config.daemon.lock_countdown_seconds
        )
        return max(0, total - int(now_boot - started))

    def mark_first_run_done(self, child_id: int) -> None:
        """The child has read the supervision explanation (D-111)."""
        child = repo.get_child(self.session, child_id)
        if child is None or child.first_run_done:
            return
        child.first_run_done = True
        repo.audit(
            self.session,
            actor=f"child:{child.unix_user}",
            action="first_run_explained",
            target=f"child:{child_id}",
        )
        self.session.commit()

    async def child_requested_shutdown(self, child_id: int) -> bool:
        """The shutdown tile (D-100). Refused while a sibling is still logged in."""
        runtime = self._require(child_id)
        others = [
            other
            for other in self.children.values()
            if other.child_id != child_id and self.watcher.presence(other.unix_user).present
        ]
        if others:
            log.info(
                "%s asked to shut down, but %s is still logged in",
                runtime.unix_user,
                ", ".join(o.unix_user for o in others),
            )
            return False

        repo.audit(
            self.session,
            actor=f"child:{runtime.unix_user}",
            action="poweroff",
            target=f"child:{child_id}",
            detail={"by": "child"},
        )
        self.session.commit()
        await self.enforcer.stop_all_apps(runtime.unix_user)
        self.ledger.persist()
        return await self.enforcer.poweroff()

    def file_access_request(self, child_id: int, url: str) -> int:
        """ "Anneme sor" (D-65). Turns the filter from a wall into a conversation."""
        runtime = self._require(child_id)
        row = AccessRequest(child_id=child_id, url=url, status="pending")
        self.session.add(row)
        repo.audit(
            self.session,
            actor=f"child:{runtime.unix_user}",
            action="access_requested",
            target=f"child:{child_id}",
            detail={"url": url},
        )
        self.session.commit()
        self.bus.publish(
            EventKind.ACCESS_REQUEST, child_id=child_id, url=url, request_id=row.id
        )
        return int(row.id)

    def grant_time(self, child_id: int, seconds: int, actor: str = "parent") -> dict[str, Any]:
        runtime = self._require(child_id)
        day = self.clock.read().wall.date()
        state = self.ledger.grant(child_id, seconds, day)

        decision = evaluate(
            runtime.policy,
            state,
            ActivityContext(session_active=True),
            self.clock.read().wall,
        )
        # The extension pushed the end of the day back, so the lock screen comes
        # down and the warnings that are now in the future can fire again.
        self._clear_end_sequence(runtime, day, decision.remaining_seconds)
        self.ledger.rearm_warnings(child_id, day, decision.remaining_seconds)
        self.ledger.persist()

        repo.audit(
            self.session,
            actor=actor,
            action="time_granted",
            target=f"child:{child_id}",
            detail={"seconds": seconds, "remaining_seconds": decision.remaining_seconds},
        )
        self.session.commit()
        self.bus.publish(
            EventKind.TICK,
            child_id=child_id,
            remaining_seconds=decision.remaining_seconds,
            remaining_minutes=decision.remaining_minutes,
            used_seconds=state.general_used_s,
            granted=seconds,
        )
        return {"child_id": child_id, "remaining_seconds": decision.remaining_seconds}

    async def lock_now(self, child_id: int, actor: str = "parent") -> bool:
        runtime = self._require(child_id)
        presence = self.watcher.presence(runtime.unix_user)
        ok = await self.enforcer.lock_sessions(presence.lockable_session_ids)
        repo.audit(
            self.session,
            actor=actor,
            action="session_locked",
            target=f"child:{child_id}",
            detail={"ok": ok, "sessions": list(presence.lockable_session_ids)},
        )
        self.session.commit()
        return ok

    async def logout_now(self, child_id: int, actor: str = "parent") -> bool:
        runtime = self._require(child_id)
        await self.enforcer.stop_all_apps(runtime.unix_user)
        ok = await self.enforcer.logout_user(runtime.unix_user)
        repo.audit(
            self.session,
            actor=actor,
            action="logout",
            target=f"child:{child_id}",
            detail={"ok": ok},
        )
        self.session.commit()
        return ok

    async def poweroff_now(self, actor: str = "parent") -> bool:
        for runtime in self.children.values():
            await self.enforcer.stop_all_apps(runtime.unix_user)
        repo.audit(self.session, actor=actor, action="poweroff", severity="warning")
        self.session.commit()
        self.ledger.persist()
        return await self.enforcer.poweroff()

    def start_timeout(self, child_id: int, minutes: int, actor: str = "parent") -> None:
        self._require(child_id)
        self.ledger.start_penalty(child_id, minutes * 60)
        repo.audit(
            self.session,
            actor=actor,
            action="timeout_started",
            target=f"child:{child_id}",
            detail={"minutes": minutes},
        )
        self.session.commit()
        self.bus.publish(EventKind.TIMEOUT_STARTED, child_id=child_id, minutes=minutes)

    def clear_timeout(self, child_id: int, actor: str = "parent") -> None:
        self._require(child_id)
        self.ledger.clear_penalty(child_id)
        repo.audit(
            self.session,
            actor=actor,
            action="timeout_cleared",
            target=f"child:{child_id}",
        )
        self.session.commit()
        self.bus.publish(EventKind.TIMEOUT_FINISHED, child_id=child_id)

    def end_break(self, child_id: int, actor: str = "parent") -> None:
        self._require(child_id)
        self.ledger.end_break(child_id)
        repo.audit(self.session, actor=actor, action="break_ended", target=f"child:{child_id}")
        self.session.commit()
        self.bus.publish(EventKind.BREAK_FINISHED, child_id=child_id)

    def report_activity(
        self, child_id: int, app_slug: str, app_name: str = "", category: str = ""
    ) -> None:
        """The shell says what has focus. Trusted for `ACTIVITY_TTL_SECONDS`."""
        runtime = self._require(child_id)
        runtime.app_slug = app_slug
        runtime.app_name = app_name or app_slug
        try:
            runtime.category = Category(category) if category else None
        except ValueError:
            runtime.category = None
        runtime.activity_reported_at = self.clock.read().boot

    def request_launch(self, child_id: int, app_slug: str) -> Decision:
        """May this child start this application, right now?"""
        runtime = self._require(child_id)
        reading = self.clock.read()
        state = self.ledger.state(child_id, reading.wall.date())
        allowed = repo.app_is_allowed(self.session, child_id, app_slug)
        link_category: Category | None = None
        for link, app in repo.list_child_apps(self.session, child_id):
            if link.app_slug == app_slug:
                link_category = repo.child_app_category(link, app)
                break
        return evaluate(
            runtime.policy,
            state,
            ActivityContext(
                app_slug=app_slug,
                category=link_category,
                app_allowed=allowed,
                session_active=self.watcher.presence(runtime.unix_user).active,
            ),
            reading.wall,
        )

    def _require(self, child_id: int) -> ChildRuntime:
        runtime = self.children.get(child_id)
        if runtime is None:
            raise KeyError(f"no such child: {child_id}")
        return runtime


async def run_forever(supervisor: Supervisor, stop_event: asyncio.Event) -> None:
    """Tick until asked to stop, and never die because one tick threw.

    A crash here is caught by systemd and escalates to the lockdown service
    (D-106), which is the right answer for a broken daemon. It is the wrong
    answer for one transient D-Bus hiccup, so a single failing tick is logged
    and the loop continues; the failure becomes visible in the audit log rather
    than in a locked screen a parent has to explain.
    """
    interval = supervisor.config.daemon.tick_seconds
    while not stop_event.is_set():
        try:
            await supervisor.tick()
        except Exception:  # one bad tick must not stop enforcement
            log.exception("tick failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue
