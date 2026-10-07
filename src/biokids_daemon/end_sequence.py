"""The end of the day: lock, count down, then the parent's end action.

`ARCHITECTURE.md` §3.5. The policy engine can say "quota exhausted" but not "the
lock screen has been up for 118 seconds": that is history, not a rule. The
ledger keeps when the sequence started in the database, so a daemon restart
mid-countdown resumes rather than restarting the two minutes.
"""

from __future__ import annotations

import datetime as dt

from biokids_core import repo
from biokids_core.ipc import EventKind
from biokids_core.types import ChildPolicy, Decision, EndAction, Reason
from biokids_daemon.base import ChildRuntime, SupervisorBase, log
from biokids_daemon.sessions import UserPresence

TERMINAL_LOCK: frozenset[str] = frozenset({Reason.QUOTA_EXHAUSTED, Reason.WINDOW_CLOSED})
"""The two reasons that end the day, and the only two that may reach the end
sequence. A break also returns `Action.LOCK`; if it opened the countdown, the
next real exhaustion would find it already expired and skip the lock screen."""

END_ACTION_BACKOFF_SECONDS: tuple[int, ...] = (10, 20, 40, 80, 120)
"""How long to wait before retrying an end action that did not work."""

END_ACTION_GIVE_UP_AFTER = 3
"""After this many consecutive failures, escalate to the lockdown path (D-106)
rather than continuing to log quietly."""


class EndSequenceMixin(SupervisorBase):
    """The countdown and the end action, re-asserted on every tick."""

    async def _run_end_sequence(
        self,
        runtime: ChildRuntime,
        presence: UserPresence,
        decision: Decision,
        day: dt.date,
        now_boot: float,
    ) -> None:
        """Lock, count down, then do what the parent chose.

        Only reached for the reasons in TERMINAL_LOCK. Three things are
        re-asserted on every tick rather than done once, so that a child who
        gets back in is not unsupervised for the rest of the day:

        * the **lock**, against any session that is present and not already
          locked, so a dismissed lock comes back within one tick;
        * the **end action**, whenever a session id appears it has not been
          applied against - logind hands out a fresh id on every login;
        * a **retry**, when the last attempt returned false, with backoff.
        """
        if not presence.present:
            # Nobody to end the day for. Poweroff and logout reach past the
            # child, so running them now would end whoever else is using the
            # machine. The countdown starts when the child comes back.
            return

        policy = runtime.policy
        boot_id = self.clock.read().boot_id

        started_new = self.ledger.start_end_sequence(runtime.child_id, day, now_boot, boot_id)
        started = self.ledger.end_sequence_started_at(runtime.child_id, day, boot_id)
        if started is None:  # pragma: no cover - written immediately above
            started = now_boot

        if started_new:
            runtime.end_action_sessions = frozenset()
            runtime.end_action_failures = 0
            runtime.end_action_next_attempt = 0.0
            if decision.speak is not None:
                self._publish_speech(runtime, decision)
            repo.audit(
                self.session,
                actor="daemon",
                action="quota_exhausted",
                target=f"child:{runtime.child_id}",
                detail={"reason": decision.reason_code},
            )

        # The lock, every tick, but only where it would change something.
        await self._lock(runtime, presence, only_unlocked=True)

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

        if elapsed < countdown:
            return

        present = frozenset(presence.lockable_session_ids)
        unenforced = present - runtime.end_action_sessions
        retry_due = (
            runtime.end_action_failures > 0 and now_boot >= runtime.end_action_next_attempt
        )
        already_done = bool(runtime.end_action_sessions)
        if already_done and not unenforced and not retry_due:
            return
        if runtime.end_action_failures > 0 and not retry_due:
            # An action that keeps failing waits for its backoff rather than
            # retrying, and auditing an alert, on every tick.
            return

        action = self._end_action_for(runtime, presence, policy)
        # Applications first, so an unsaved drawing gets its SIGTERM before the
        # machine starts going down.
        await self.enforcer.stop_all_apps(runtime.unix_user)
        ok = await self.enforcer.apply_end_action(
            action, runtime.unix_user, presence.lockable_session_ids
        )

        if ok:
            # `{""}` rather than an empty set for poweroff, which has no sessions
            # to list: an empty set would read as "never applied" and re-fire.
            runtime.end_action_sessions = present or frozenset({""})
            runtime.end_action_failures = 0
        else:
            runtime.end_action_failures += 1
            wait = END_ACTION_BACKOFF_SECONDS[
                min(runtime.end_action_failures - 1, len(END_ACTION_BACKOFF_SECONDS) - 1)
            ]
            runtime.end_action_next_attempt = now_boot + wait

        repo.audit(
            self.session,
            actor="daemon",
            action=action.value if action is not EndAction.LOCK else "session_locked",
            target=f"child:{runtime.child_id}",
            detail={
                "reason": decision.reason_code,
                "ok": ok,
                "attempt": runtime.end_action_failures or 1,
            },
            severity="warning" if ok else "alert",
        )
        log.info("end action %s for %s: %s", action.value, runtime.unix_user, ok)

        if runtime.end_action_failures >= END_ACTION_GIVE_UP_AFTER:
            # Stop hoping and take the D-106 path: lock every session this child
            # has, loudly. A control that has quietly given up is what principle
            # 3 exists to prevent.
            log.error(
                "the end action for %s has failed %s times; falling back to lockdown",
                runtime.unix_user,
                runtime.end_action_failures,
            )
            targets = presence.lockable_session_ids
            if not targets:
                # Nothing to lock. Say that nothing was secured rather than
                # auditing a lockdown that did not happen.
                log.error(
                    "the end action for %s failed %s times and there is no session "
                    "to lock; nothing has been secured",
                    runtime.unix_user,
                    runtime.end_action_failures,
                )
                repo.audit(
                    self.session,
                    actor="daemon",
                    action="daemon_failed",
                    target=f"child:{runtime.child_id}",
                    detail={
                        "what": "end action failed and no session could be locked",
                        "attempts": runtime.end_action_failures,
                    },
                    severity="alert",
                )
                self.session.commit()
                return
            await self.enforcer.lockdown(targets)
            repo.audit(
                self.session,
                actor="daemon",
                action="lockdown",
                target=f"child:{runtime.child_id}",
                detail={"reason": "the end action kept failing", "action": action.value},
                severity="alert",
            )
            runtime.end_action_failures = 0
            runtime.end_action_next_attempt = now_boot + END_ACTION_BACKOFF_SECONDS[-1]

    def _end_action_for(
        self,
        runtime: ChildRuntime,
        presence: UserPresence,
        policy: ChildPolicy | None,
    ) -> EndAction:
        """The end action, narrowed to what may be done to this machine right now.

        Poweroff takes the whole machine and logout ends sessions by user name,
        so while anyone else is logged in - a parent, a sibling - both degrade
        to locking this child's own screen. The child still loses the machine;
        nobody else does.
        """
        chosen = policy.end_action if policy is not None else EndAction.LOCK
        if chosen is EndAction.LOCK:
            return chosen
        others = self._others_logged_in(runtime)
        if not others:
            return chosen
        log.info(
            "%s is configured for %s, but %s %s also logged in; locking instead",
            runtime.unix_user,
            chosen.value,
            ", ".join(others),
            "is" if len(others) == 1 else "are",
        )
        repo.audit(
            self.session,
            actor="daemon",
            action="end_action_narrowed",
            target=f"child:{runtime.child_id}",
            detail={
                "configured": chosen.value,
                "used": EndAction.LOCK.value,
                "others": list(others),
            },
        )
        return EndAction.LOCK

    def _clear_end_sequence(self, runtime: ChildRuntime, day: dt.date, remaining: int) -> None:
        runtime.lock_attempts = 0
        runtime.end_action_sessions = frozenset()
        runtime.end_action_failures = 0
        runtime.end_action_next_attempt = 0.0
        if not self.ledger.has_end_sequence(runtime.child_id, day):
            return
        self.ledger.clear_end_sequence(runtime.child_id, day)
        self.ledger.rearm_warnings(runtime.child_id, day, remaining)
        self.bus.publish(EventKind.END_SEQUENCE, child_id=runtime.child_id, cancelled=True)

    def _countdown_remaining(
        self, runtime: ChildRuntime, day: dt.date, now_boot: float, boot_id: str
    ) -> int:
        """Seconds left on the lock screen before the end action, or zero.

        Takes the boot id for the same reason the sequence stores one: an offset
        from a previous boot would otherwise feed the child's time-up screen a
        countdown of several hours.
        """
        started = self.ledger.end_sequence_started_at(runtime.child_id, day, boot_id)
        if started is None:
            return 0
        total = (
            runtime.policy.lock_countdown_seconds
            if runtime.policy is not None
            else self.config.daemon.lock_countdown_seconds
        )
        return max(0, total - int(now_boot - started))
