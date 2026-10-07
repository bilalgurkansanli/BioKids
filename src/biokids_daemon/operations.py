"""What the panel and the launcher ask the daemon to do.

The parent's buttons (more time, lock, unlock, log out, power off, timeout, end
a break) and the launcher's questions (may this app start, what has focus, shut
down, the first-run explanation was read). A parent's action is audited under
the actor the panel names.
"""

from __future__ import annotations

import contextlib
from typing import Any

from biokids_core import repo
from biokids_core.ipc import EventKind
from biokids_core.policy import evaluate
from biokids_core.types import ActivityContext, Category, Decision
from biokids_daemon.base import log
from biokids_daemon.end_sequence import EndSequenceMixin

VISIT_REPEAT_SECONDS = 600
"""The same host again within ten minutes is the same visit."""


class OperationsMixin(EndSequenceMixin):
    """The panel's and the launcher's operations, one method each."""

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
        """The shutdown tile (D-100). Refused while anyone else is still logged in.

        Anyone, not just a sibling: a parent who switched user so the child
        could play must not lose their open session with the machine.
        """
        runtime = self._require(child_id)
        # Ask logind now rather than trusting the last tick's snapshot: this is
        # rare and irreversible, and ten seconds is long enough for a parent to
        # have just sat down at their own account.
        with contextlib.suppress(Exception):
            await self.watcher.refresh()
        others = self._others_logged_in(runtime)
        if others:
            log.info(
                "%s asked to shut down, but %s is still logged in",
                runtime.unix_user,
                ", ".join(others),
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

    async def unlock_now(self, child_id: int, actor: str = "parent") -> bool:
        """The "Kilidi aç" button: back in, without changing the day's time.

        "+10 dk" would give time the parent may not mean to give. If the day is
        over or a window is closed, the launcher still shows that; only the lock
        comes down.
        """
        runtime = self._require(child_id)
        await self._unlock(runtime)
        runtime.locked_for_filter = False
        repo.audit(
            self.session,
            actor=actor,
            action="session_unlock_requested",
            target=f"child:{child_id}",
        )
        self.session.commit()
        return True

    async def grant_time(
        self, child_id: int, seconds: int, actor: str = "parent"
    ) -> dict[str, Any]:
        """Extend the day, and let the child back in if the screen was already locked.

        Async because of that second half: `_clear_end_sequence` clears only the
        daemon's own bookkeeping, and a lock nobody lifts leaves the child at
        the prompt of an account with no password (D-99).
        """
        runtime = self._require(child_id)
        day = self.clock.read().wall.date()
        state = self.ledger.grant(child_id, seconds, day)

        decision = evaluate(
            runtime.policy,
            state,
            ActivityContext(session_active=True),
            self.clock.read().wall,
        )
        # The extension pushed the end of the day back, so the countdown ends
        # and the warnings that are now in the future can fire again.
        self._clear_end_sequence(runtime, day, decision.remaining_seconds)
        if decision.allow and decision.remaining_seconds > 0:
            # Only when the grant actually bought time: a grant that still
            # leaves the window closed must not open the screen.
            await self._unlock(runtime)
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

    async def reboot_now(self, actor: str = "parent") -> bool:
        """Restart the machine, which LightDM needs once after BioKids is
        installed before a child can log in by their picture (D-155, D-195)."""
        for runtime in self.children.values():
            await self.enforcer.stop_all_apps(runtime.unix_user)
        repo.audit(self.session, actor=actor, action="reboot", severity="warning")
        self.session.commit()
        self.ledger.persist()
        return await self.enforcer.reboot()

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

    def record_visit(self, child_id: int, host: str) -> None:
        """A site the child opened, by host name only (D-187). Kept for
        `retention.activity_days` and shown to the parent in the history."""
        runtime = self._require(child_id)
        now = self.clock.read().boot
        last = runtime.visits_seen.get(host)
        if last is not None and now - last < VISIT_REPEAT_SECONDS:
            return
        runtime.visits_seen[host] = now
        repo.record_event(
            self.session, kind="visit", child_id=child_id, ts_boot=now, payload={"host": host}
        )
        self.session.commit()

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
