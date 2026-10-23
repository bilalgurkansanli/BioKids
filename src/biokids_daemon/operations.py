"""What the panel and the launcher ask the daemon to do.

The parent's buttons (more time, lock, unlock, log out, power off, timeout, end
a break) and the launcher's questions (may this app start, what has focus, shut
down, the first-run explanation was read). A parent's action is audited under
the actor the panel names.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
from collections.abc import Awaitable, Iterable, Iterator
from typing import Any

from sqlalchemy import func, select

from biokids_core import notes, repo
from biokids_core.db.models import AiAlert, SharedRecording, utcnow
from biokids_core.ipc import EventKind
from biokids_core.policy import bedtime_in, evaluate, finish_up_offer
from biokids_core.repo import SEARCH_ALERT_APP
from biokids_core.search_filter import MAX_SEARCH_WORDS
from biokids_core.types import (
    ActivityContext,
    Category,
    Decision,
    ShutdownOutcome,
    SpeechRequest,
    is_app_scope,
)
from biokids_daemon.base import ChildRuntime, log
from biokids_daemon.end_sequence import EndSequenceMixin

VISIT_REPEAT_SECONDS = 600
SEARCH_REPEAT_SECONDS = 600
"""The same refused search again within this long is the same search: a child
pressing Enter three times is one thing for the parent to read, not three."""
SEARCH_ALERTS_PER_DAY = 20
"""At most this many refused searches a day per child reach the panel. A child
who found the wall and keeps testing it has been noticed after the first few."""
"""The same host again within ten minutes is the same visit."""

SHARED_RECORDING_MAX_BYTES = 700 * 1024
"""Sesim's longest recording at its shared rate is about 640 KB (D-240)."""
SHARED_RECORDINGS_PER_DAY = 10
"""A day's recordings a child may send; the button is a child's to press."""
POWER_GRACE_SECONDS = 6.0
"""The least time between answering "switch off" and doing it (D-209).

The answer is a page - the child's goodbye, the parent's "Bilgisayar
kapatılıyor." - and a page needs a moment on the screen, and a 4-7 child's
goodbye a moment to be said aloud, before systemd takes the session down under
it. The applications are being stopped meanwhile, so with one open this is
seldom any extra wait."""

LOGOUT_GRACE_SECONDS = 10.0
"""The same for a child's session that ends instead (D-209): the sentence that
says why the computer stays on is three times the goodbye's, and a 4-7 child
should hear it to the end before the session goes."""

REFRESH_BEFORE_ANSWER_S = 2.0
"""How long logind gets to say who is logged in before the child is answered.
The launcher gives up after five seconds; a slower logind is answered from the
last tick's picture, and the work asks again before it acts."""

POWER_TASKS = ("poweroff", "reboot")
"""The names the machine-wide work runs under, one at a time: a second "Kapat"
while the first is under way gets the same answer and starts nothing more."""


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

    async def child_requested_shutdown(self, child_id: int) -> ShutdownOutcome:
        """The shutdown tile (D-100): the computer switches off, or the child's session ends.

        Off when nobody else is logged in. While anyone else is - anyone, not
        just a sibling: a parent who switched user so the child could play must
        not lose their open session with the machine - only this child's own
        session ends, so "Kapat" still closes everything the child has open and
        nothing of anyone else's.

        Decided and audited here, done after the answer (D-209). Every open
        application gets its grace period first, and the launcher waits five
        seconds for an answer: with an app open the answer came too late, and
        the child was put back on the home screen as if "Evet, kapat" had done
        nothing.
        """
        runtime = self._require(child_id)
        # Ask logind now rather than trusting the last tick's snapshot: this is
        # rare and irreversible, and ten seconds is long enough for a parent to
        # have just sat down at their own account.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.watcher.refresh(), REFRESH_BEFORE_ANSWER_S)
        others = self._others_logged_in(runtime)
        if others:
            name = f"logout:{runtime.unix_user}"
            if not self._under_way(name):
                self._audit_session_instead(runtime, others)
                self._after_answering(
                    self._end_childs_session(runtime, _grace_deadline(LOGOUT_GRACE_SECONDS)),
                    name,
                )
            return ShutdownOutcome.LOGOUT

        if self._under_way(*POWER_TASKS):
            # A second tap, or the parent pressed "Kapat" too: once is enough.
            return ShutdownOutcome.POWEROFF
        repo.audit(
            self.session,
            actor=f"child:{runtime.unix_user}",
            action="poweroff",
            target=f"child:{child_id}",
            detail={"by": "child"},
        )
        self.session.commit()
        self._after_answering(self._child_poweroff(runtime, _grace_deadline()), "poweroff")
        return ShutdownOutcome.POWEROFF

    async def child_switch_user(self, child_id: int) -> bool:
        """ "Oturumu değiştir" on the child's screen (D-227): this child's session
        ends - their apps first get their grace period, as for "Kapat" - and the
        login screen comes back for whoever is next.

        Ended, not left open behind the greeter: a child cannot unlock a session
        of their own without a password (D-155), so one left open would keep
        them out of the computer until a parent came.
        """
        runtime = self._require(child_id)
        name = f"logout:{runtime.unix_user}"
        if not self._under_way(name):
            repo.audit(
                self.session,
                actor=f"child:{runtime.unix_user}",
                action="switch_user",
                target=f"child:{child_id}",
                detail={"by": "child"},
            )
            self.session.commit()
            self._after_answering(
                self._end_childs_session(runtime, _grace_deadline(LOGOUT_GRACE_SECONDS)), name
            )
        return True

    async def switch_to_greeter(self, actor: str) -> bool:
        """The login screen over the session at the machine (D-227), from the
        parent's panel. Their session stays open behind it; LightDM asks for
        their password when they come back."""
        ok = await self.enforcer.switch_to_greeter()
        repo.audit(
            self.session,
            actor=actor,
            action="switch_user",
            detail={"by": "parent", "ok": ok},
        )
        self.session.commit()
        return ok

    def _audit_session_instead(self, runtime: ChildRuntime, others: tuple[str, ...]) -> None:
        log.info(
            "%s asked to shut down, but %s %s logged in; ending only %s's session",
            runtime.unix_user,
            ", ".join(others),
            "is" if len(others) == 1 else "are",
            runtime.unix_user,
        )
        with self._recorded_if_possible("a session ended instead of the computer"):
            repo.audit(
                self.session,
                actor=f"child:{runtime.unix_user}",
                action="logout",
                target=f"child:{runtime.child_id}",
                detail={"by": "child", "instead_of": "poweroff", "others": list(others)},
            )

    async def _child_poweroff(self, runtime: ChildRuntime, deadline: float) -> None:
        """The child's applications, the day's minutes, then the computer.

        Who else is logged in is asked once more just before: D-100's promise is
        about the moment the machine goes, and a parent may have switched to
        their own account while the applications were closing.
        """
        await self._close_apps((runtime,))
        await _until(deadline)
        with contextlib.suppress(Exception):
            await self.watcher.refresh()
        others = self._others_logged_in(runtime)
        if others:
            self._audit_session_instead(runtime, others)
            await self._logout_or_lock(runtime)
            return
        if await _attempt(self.enforcer.poweroff(), "switching the computer off"):
            return
        # Principle 3: the child was told the computer is switching off, and is
        # not left in front of an open session that nothing is ending.
        log.error(
            "the computer did not switch off when %s asked; ending their session instead",
            runtime.unix_user,
        )
        ended = await self._logout_or_lock(runtime)
        with self._recorded_if_possible("the shutdown that did not happen"):
            repo.audit(
                self.session,
                actor="daemon",
                action="poweroff_failed",
                target=f"child:{runtime.child_id}",
                detail={"by": "child", "session_ended": ended},
                severity="alert",
            )

    async def _end_childs_session(self, runtime: ChildRuntime, deadline: float) -> None:
        """Someone else is using the computer: close what this child has open."""
        await self._close_apps((runtime,))
        await _until(deadline)
        await self._logout_or_lock(runtime)

    async def _close_apps(self, runtimes: Iterable[ChildRuntime]) -> None:
        """Applications first, so an unsaved drawing gets its SIGTERM and its grace
        period; then the day's minutes are written down. Neither failing stops
        what comes after: ending the session or the machine ends the apps too."""
        for runtime in list(runtimes):
            await _attempt(
                self.enforcer.stop_all_apps(runtime.unix_user),
                f"stopping {runtime.unix_user}'s applications",
            )
        try:
            self.ledger.persist()
        except Exception:
            log.exception("could not write the day's minutes down first")

    async def _logout_or_lock(self, runtime: ChildRuntime) -> bool:
        """End this child's session - this child's alone - or at least lock it.

        `loginctl terminate-user`, the path EndAction.LOGOUT takes, and it ends
        sessions by user name, so nobody else's. When logind will not end it the
        screen is locked instead, and the parent is told: a child who pressed
        "Kapat" is never left at an open session nobody is ending (principle 3).
        """
        user = runtime.unix_user
        if await _attempt(self.enforcer.logout_user(user), f"ending {user}'s session"):
            return True
        with contextlib.suppress(Exception):
            await self.watcher.refresh()
        sessions = self.watcher.presence(user).lockable_session_ids
        locked = await _attempt(
            self.enforcer.lock_sessions(sessions), f"locking {user}'s screen"
        )
        log.error(
            "%s's session did not end; %s",
            user,
            "the screen is locked instead" if locked else "nor could the screen be locked",
        )
        with self._recorded_if_possible("the session that did not end"):
            repo.audit(
                self.session,
                actor="daemon",
                action="logout_failed",
                target=f"child:{runtime.child_id}",
                detail={"locked": locked, "sessions": list(sessions)},
                severity="alert",
            )
        return False

    @contextlib.contextmanager
    def _recorded_if_possible(self, what: str) -> Iterator[None]:
        """For the audit rows of work done after answering: a database that will
        not take one is logged, and never stops the switching off or the lock."""
        try:
            yield
            self.session.commit()
        except Exception:
            log.exception("could not record %s in the audit log", what)
            with contextlib.suppress(Exception):
                self.session.rollback()

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

    async def day_changed(self, child_id: int) -> dict[str, Any]:
        """A parent made today special, or took it back (D-215).

        The rules are re-read as for any change on the time screen, and then one
        thing more, which a plain reload does not do: a child whose day is now
        longer - "+60 today", tapped after the time ran out - is let back in,
        as "+10 dk" does (D-144). A lock nobody lifts leaves the child at the
        prompt of an account with no password (D-99). Only a lock the end of the
        day put on: never one the DNS filter holds (D-172).

        A day that got shorter needs nothing here: the next tick ends it, with
        the warnings and the countdown it would always have had.
        """
        self.reload()
        runtime = self._require(child_id)
        reading = self.clock.read()
        day = reading.wall.date()
        state = self.ledger.state(child_id, day)
        decision = evaluate(
            runtime.policy, state, ActivityContext(session_active=True), reading.wall
        )
        if decision.allow and decision.remaining_seconds > 0:
            ended = self.ledger.has_end_sequence(child_id, day)
            self._clear_end_sequence(runtime, day, decision.remaining_seconds)
            if ended and not runtime.locked_for_filter:
                await self._unlock(runtime)
        self.ledger.rearm_warnings(child_id, day, decision.remaining_seconds)
        self.ledger.persist()
        self.session.commit()
        self.bus.publish(
            EventKind.TICK,
            child_id=child_id,
            remaining_seconds=decision.remaining_seconds,
            remaining_minutes=decision.remaining_minutes,
            used_seconds=state.general_used_s,
            online=self.watcher.presence(runtime.unix_user).active,
            bedtime_seconds=bedtime_in(runtime.policy, state, reading.wall),
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
        """The parent's "Kapat" on Today: answered at once, done afterwards (D-209).

        Every child's applications get their grace period first, and that used
        to happen inside the panel's request: with an app open the panel gave
        up after its five seconds and told the parent the service was not
        answering, while the machine went down anyway.
        """
        return self._power_after_answering(actor, restart=False)

    async def reboot_now(self, actor: str = "parent") -> bool:
        """Restart the machine, which LightDM needs once after BioKids is
        installed before a child can log in by their picture (D-155, D-195).
        Answered at once and done afterwards, like poweroff_now (D-209)."""
        return self._power_after_answering(actor, restart=True)

    def _power_after_answering(self, actor: str, *, restart: bool) -> bool:
        wanted = "reboot" if restart else "poweroff"
        if self._under_way(wanted):
            log.info("%s asked again while the computer is already going down", actor)
            return True
        if self._under_way(*POWER_TASKS):
            # The other one is under way: a restart asked for while a child's
            # "Kapat" is closing apps would otherwise be answered "restarting"
            # and power off - and a parent on the phone cannot switch it back on.
            log.info("%s asked for a %s while the other is under way; not done", actor, wanted)
            return False
        repo.audit(
            self.session,
            actor=actor,
            action="reboot" if restart else "poweroff",
            severity="warning",
        )
        self.session.commit()
        self._after_answering(
            self._power(restart, _grace_deadline()), "reboot" if restart else "poweroff"
        )
        return True

    async def _power(self, restart: bool, deadline: float) -> None:
        """Every child's applications, the minutes, then the machine.

        If the machine does not go, every child's screen is locked and the
        parent is told: they meant nobody to be using it, and a lock is the one
        thing they can lift again from Today ("Kilidi aç").
        """
        await self._close_apps(self.children.values())
        await _until(deadline)
        command = self.enforcer.reboot() if restart else self.enforcer.poweroff()
        if await _attempt(command, "restarting" if restart else "switching off"):
            return
        log.error(
            "the computer did not %s; locking every child's screen",
            "restart" if restart else "switch off",
        )
        with contextlib.suppress(Exception):
            await self.watcher.refresh()
        locked: list[str] = []
        for runtime in list(self.children.values()):
            sessions = self.watcher.presence(runtime.unix_user).lockable_session_ids
            if sessions and await _attempt(
                self.enforcer.lock_sessions(sessions), f"locking {runtime.unix_user}'s screen"
            ):
                locked.append(runtime.unix_user)
        with self._recorded_if_possible("the shutdown that did not happen"):
            repo.audit(
                self.session,
                actor="daemon",
                action="reboot_failed" if restart else "poweroff_failed",
                detail={"by": "parent", "locked": locked},
                severity="alert",
            )

    async def start_together(
        self, child_id: int, minutes: int, actor: str = "parent"
    ) -> dict[str, Any]:
        """Birlikte modu (D-235): a parent is at the computer with the child.

        For `minutes` nothing is counted - not the day, not a kind of app - and
        nothing ends the time: no warning, no lock, no break. Using a computer
        together is the screen time most worth having, and should cost the
        child nothing. Started only from the panel, by a parent; it ends by
        itself, or with a tap, or when the daemon restarts. A child already
        locked out is let back in.
        """
        runtime = self._require(child_id)
        reading = self.clock.read()
        if not runtime.together_active(reading.boot):
            runtime.together_started_at = reading.boot
        runtime.together_until = reading.boot + max(1, minutes) * 60
        runtime.together_warned = False
        day = reading.wall.date()
        self._clear_end_sequence(runtime, day, runtime.together_left(reading.boot))
        runtime.paused_reason = ""
        await self._unlock(runtime)
        repo.audit(
            self.session,
            actor=actor,
            action="together_started",
            target=f"child:{child_id}",
            detail={"minutes": minutes},
        )
        self.session.commit()
        self._speak(
            runtime,
            SpeechRequest(
                event="together_start",
                vars={"name": runtime.display_name, "minutes": minutes},
                expression="happy",
            ),
        )
        self.bus.publish(EventKind.POLICY_CHANGED, child_id=child_id, together=True)
        return {"child_id": child_id, "together_seconds": runtime.together_left(reading.boot)}

    def stop_together(self, child_id: int, actor: str = "parent") -> None:
        runtime = self._require(child_id)
        if runtime.together_until:
            self._end_together(runtime, self.clock.read().boot, actor=actor)

    def _end_together(self, runtime: ChildRuntime, now_boot: float, *, actor: str) -> None:
        """Back to counting: said once, audited with how long it lasted."""
        minutes = (
            max(0, int(min(now_boot, runtime.together_until) - runtime.together_started_at))
            // 60
        )
        runtime.together_until = 0.0
        runtime.together_started_at = 0.0
        runtime.together_warned = False
        repo.audit(
            self.session,
            actor=actor,
            action="together_ended",
            target=f"child:{runtime.child_id}",
            detail={"minutes": minutes},
        )
        self.session.commit()
        self._speak(
            runtime,
            SpeechRequest(
                event="together_end", vars={"name": runtime.display_name}, expression="neutral"
            ),
        )
        self.bus.publish(EventKind.POLICY_CHANGED, child_id=runtime.child_id, together=False)

    def send_note(
        self,
        child_id: int,
        kind: str,
        text: str = "",
        *,
        from_name: str = "",
        parent_id: int | None = None,
        actor: str = "parent",
    ) -> int:
        """A parent's "Aferin" or note (D-233): kept, then shown to the child.

        Stored and told in one step, so the child's launcher sees it live when
        the child is at the computer, and finds it waiting in its state when
        they are not. Zio says the "well done"; a note is read by the launcher,
        which knows when it is on the screen. Raises ValueError for a note that
        is not one; the audit row has its kind, never its words.
        """
        runtime = self._require(child_id)
        note = notes.add(
            self.session, child_id, kind, text, parent_id=parent_id, from_name=from_name
        )
        repo.audit(
            self.session,
            actor=actor,
            action="praise_sent" if kind == "praise" else "note_sent",
            target=f"child:{child_id}",
        )
        self.session.commit()
        self.bus.publish(EventKind.PARENT_NOTE, child_id=child_id, note=notes.as_card(note))
        if kind == "praise":
            self._speak(
                runtime,
                SpeechRequest(
                    event="praise",
                    vars={"name": runtime.display_name, "parent": note.from_name},
                    expression="happy",
                ),
            )
        return note.id

    def share_recording(self, child_id: int, audio_b64: str, seconds: float) -> bool:
        """A recording the child sent from Sesim (D-240), for the parent's panel.

        Only what the child chose, only a short WAV, and at most a handful a
        day: a button a child can press is a button a child can press forty
        times. The audit row says one was sent, never what is in it.
        """
        self._require(child_id)
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except (ValueError, TypeError):
            return False
        if not (44 < len(audio) <= SHARED_RECORDING_MAX_BYTES) or audio[:4] != b"RIFF":
            return False
        if audio[8:12] != b"WAVE":
            return False
        since = utcnow() - dt.timedelta(days=1)
        sent = self.session.scalar(
            select(func.count())
            .select_from(SharedRecording)
            .where(SharedRecording.child_id == child_id, SharedRecording.created_at >= since)
        )
        if (sent or 0) >= SHARED_RECORDINGS_PER_DAY:
            return False
        self.session.add(
            SharedRecording(
                child_id=child_id, seconds=max(0.0, min(float(seconds), 60.0)), audio=audio
            )
        )
        repo.audit(
            self.session,
            actor="child",
            action="recording_shared",
            target=f"child:{child_id}",
        )
        self.session.commit()
        return True

    def note_seen(self, child_id: int, note_id: int) -> bool:
        """The child's screen showed the note; the panel says "okundu"."""
        self._require(child_id)
        seen = notes.mark_seen(self.session, child_id, note_id)
        self.session.commit()
        return seen

    def finish_up(self, child_id: int) -> int:
        """The last round the child asked for (D-230): seconds given, 0 for none.

        Whether one is due is decided here, by the policy engine, never by the
        launcher that asks: the parent's minutes, once today, only while the
        day's quota is what is about to end it. Zio says so, the ring moves at
        once, and the parent reads it on Today and in the audit log.
        """
        runtime = self._require(child_id)
        reading = self.clock.read()
        if runtime.together_active(reading.boot):
            # Nothing is counted while a parent is here (D-235): a last round
            # now would only spend tomorrow's... today's, for nothing.
            return 0
        day = reading.wall.date()
        fresh = runtime.activity_fresh(reading.boot)
        offer = finish_up_offer(
            runtime.policy,
            self.ledger.state(child_id, day),
            ActivityContext(
                app_slug=runtime.app_slug if fresh else None,
                category=runtime.category if fresh else None,
                session_active=True,
            ),
            reading.wall,
        )
        if offer <= 0:
            return 0
        state = self.ledger.finish_up(child_id, offer, day)
        decision = evaluate(
            runtime.policy, state, ActivityContext(session_active=True), reading.wall
        )
        # The one-minute warning is ahead of the clock again, and says so.
        self.ledger.rearm_warnings(child_id, day, decision.remaining_seconds)
        self.ledger.persist()
        repo.audit(
            self.session,
            actor=f"child:{runtime.unix_user}",
            action="finish_up_used",
            target=f"child:{child_id}",
            detail={"seconds": offer},
        )
        self.session.commit()
        self._speak(
            runtime,
            SpeechRequest(
                event="finish_up",
                vars={"name": runtime.display_name, "minutes": offer // 60},
                expression="happy",
            ),
        )
        self.bus.publish(
            EventKind.TICK,
            child_id=child_id,
            remaining_seconds=decision.remaining_seconds,
            remaining_minutes=decision.remaining_minutes,
            used_seconds=state.general_used_s,
            finish_up=offer,
        )
        return offer

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
        self, child_id: int, app_slug: str, app_name: str = "", *, unit: str = ""
    ) -> None:
        """The shell says which app is in front. Trusted for `ACTIVITY_TTL_SECONDS`.

        Its kind is looked up here, in the child's own apps with the parent's
        override (D-228): the launcher runs as the child, and a category it
        named would be the child's word for what they are doing. An app the
        child does not have counts against no kind. `unit` is the app's scope,
        kept only when it is one, so a limit can stop it.
        """
        runtime = self._require(child_id)
        unit = unit if is_app_scope(unit) else ""
        if unit and unit == runtime.blocked_unit:
            return
        found = repo.child_app(self.session, child_id, app_slug) if app_slug else None
        runtime.app_slug = app_slug
        runtime.app_unit = unit
        if found is None:
            runtime.app_name = app_name or app_slug
            runtime.category = None
        else:
            link, app = found
            runtime.app_name = app.name(runtime.locale)
            runtime.category = repo.child_app_category(link, app)
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

    def record_refused_search(self, child_id: int, words: str, host: str) -> None:
        """A search the browser refused, for the parent to read (D-222).

        Kept as an alert beside the assistant's (D-165): Today says that one
        happened, Analiz shows the words, and the parent's mail says only that
        one happened - the words stay on the machine. The child's block page
        told them a grown-up will see it. The audit row names the site, never
        the words: the audit log is kept for good, the alert only as long as
        the rest of the child's activity (D-189).
        """
        runtime = self._require(child_id)
        words = " ".join(words.split())[:MAX_SEARCH_WORDS]
        if not words:
            return
        now = self.clock.read().boot
        key = words.casefold()
        last = runtime.searches_seen.get(key)
        if last is not None and now - last < SEARCH_REPEAT_SECONDS:
            return
        runtime.searches_seen[key] = now
        session = self.session
        today = session.scalar(
            select(func.count())
            .select_from(AiAlert)
            .where(
                AiAlert.child_id == child_id,
                AiAlert.app == SEARCH_ALERT_APP,
                AiAlert.ts >= utcnow() - dt.timedelta(days=1),
            )
        )
        if int(today or 0) >= SEARCH_ALERTS_PER_DAY:
            return
        session.add(
            AiAlert(
                child_id=child_id,
                score=100,
                category=SEARCH_ALERT_APP,
                reason_key="search.refused",
                confidence=1.0,
                band="notify",
                app=SEARCH_ALERT_APP,
                text=words,
            )
        )
        repo.audit(
            session,
            actor=f"child:{runtime.unix_user}",
            action="search_refused",
            target=f"child:{child_id}",
            detail={"host": host},
            severity="warning",
        )
        session.commit()
        log.info("a search was refused for child %s on %s", child_id, host or "?")

    def english_state(self, child_id: int) -> dict[str, Any]:
        """The calling child's boxes in the English corner, and their day (D-225)."""
        self._require(child_id)
        today = self.clock.read().wall.date()
        return {
            "today": today.isoformat(),
            "items": {
                item: [
                    state.box,
                    state.due.isoformat() if state.due else None,
                    state.first_day.isoformat() if state.first_day else None,
                ]
                for item, state in repo.english_progress(self.session, child_id).items()
            },
        }

    def reading_state(self, child_id: int) -> dict[str, Any]:
        """The calling child's boxes in the reading corner, and their day (D-241)."""
        self._require(child_id)
        today = self.clock.read().wall.date()
        return {
            "today": today.isoformat(),
            "items": {
                item: [
                    state.box,
                    state.due.isoformat() if state.due else None,
                    state.first_day.isoformat() if state.first_day else None,
                ]
                for item, state in repo.reading_progress(self.session, child_id).items()
            },
        }

    def reading_answer(self, child_id: int, item: str, right: bool) -> int | None:
        """One answer in a reading lesson; the item's new box, or None."""
        self._require(child_id)
        state = repo.record_reading_answer(
            self.session, child_id, item, right, self.clock.read().wall.date()
        )
        if state is None:
            return None
        self.session.commit()
        return state.box

    def english_answer(self, child_id: int, item: str, right: bool) -> int | None:
        """One answer in a lesson; the box it leaves the item in, or None for an
        id that is not the corner's."""
        self._require(child_id)
        state = repo.record_english_answer(
            self.session, child_id, item, right, self.clock.read().wall.date()
        )
        if state is None:
            return None
        self.session.commit()
        return state.box

    def request_launch(self, child_id: int, app_slug: str) -> Decision:
        """May this child start this application, right now?"""
        runtime = self._require(child_id)
        reading = self.clock.read()
        state = self.ledger.state(child_id, reading.wall.date())
        allowed = repo.app_is_allowed(self.session, child_id, app_slug)
        found = repo.child_app(self.session, child_id, app_slug)
        link_category: Category | None = (
            None if found is None else repo.child_app_category(*found)
        )
        decision = evaluate(
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
        # With a parent at the computer (D-235) the rules wait - but an app
        # the parent never gave this child is still not on their screen.
        return decision if not allowed else self._decided(runtime, decision, reading.boot)


def _grace_deadline(seconds: float = POWER_GRACE_SECONDS) -> float:
    """When the work just answered for may go ahead, on the event loop's clock.

    Not the supervisor's clock, which a test moves by hand and a guarded wall
    clock may hold still: this is a few real seconds, nothing to account."""
    return asyncio.get_running_loop().time() + seconds


async def _until(deadline: float) -> None:
    wait = deadline - asyncio.get_running_loop().time()
    if wait > 0:
        await asyncio.sleep(wait)


async def _attempt(step: Awaitable[bool], what: str) -> bool:
    """One step of work done after answering: False, logged, rather than raised,
    so that the fallback after it still runs."""
    try:
        return await step
    except Exception:
        log.exception("%s failed", what)
        return False
