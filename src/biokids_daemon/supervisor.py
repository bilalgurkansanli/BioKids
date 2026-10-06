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
import contextlib
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biokids_core import repo
from biokids_core.catalog import enable_for_child, load_catalog
from biokids_core.clock import Clock, GuardedClock, SystemClock
from biokids_core.config import Config, get_config
from biokids_core.db.models import AccessRequest, App, FilterRule, utcnow
from biokids_core.db.session import Database
from biokids_core.errors import ConfigError
from biokids_core.ipc import EventKind
from biokids_core.ledger import TickInput, TimeLedger
from biokids_core.policy import evaluate, general_allowance_seconds
from biokids_core.types import (
    Action,
    ActivityContext,
    Category,
    ChildPolicy,
    Decision,
    EndAction,
    FilterMode,
    Reason,
    SpeechRequest,
)
from biokids_daemon import notify
from biokids_daemon.bus import EventBus
from biokids_daemon.dnsfilter import DnsFilter, FilterHealth
from biokids_daemon.enforcer import LOCK_ATTEMPTS_BEFORE_LOGOUT, Enforcer
from biokids_daemon.notifier import Notifier
from biokids_daemon.sessions import SessionWatcher, UserPresence

log = logging.getLogger(__name__)

TERMINAL_LOCK: frozenset[str] = frozenset({Reason.QUOTA_EXHAUSTED, Reason.WINDOW_CLOSED})
"""The two reasons that end the day, and the only two that may reach the end
sequence. A break also returns `Action.LOCK`, and routing it through the same
path was a real bug: it opened the countdown slot, so the next genuine
exhaustion found a stale timestamp already past the countdown and powered the
machine off with no lock screen, no two minutes and no "time's up"."""

END_ACTION_BACKOFF_SECONDS: tuple[int, ...] = (10, 20, 40, 80, 120)
"""How long to wait before retrying an end action that did not work."""

REQUEST_KINDS = ("time", "site", "app")
REQUESTS_PER_HOUR = 6
"""A child may ask this often. Asking again for the same thing while it is still
waiting is the same request, not another one."""
REQUEST_EXPIRY = dt.timedelta(hours=12)
"""A request nobody answered is gone by the next morning: "more time" asked for
yesterday is not a question about today."""

FILTER_CHECK_SECONDS = 60
"""How often the children's DNS filter is checked from the outside (D-172)."""

END_ACTION_GIVE_UP_AFTER = 3
"""After this many consecutive failures, escalate to the lockdown path (D-106)
rather than continuing to log quietly."""

ACTIVITY_TTL_SECONDS = 90
"""How long a focus report from the shell is trusted. Past this the child is
treated as having nothing in particular on screen, which is the conservative
reading: category limits stop applying, the general quota does not."""

EARN_SPEECH_INTERVAL = 300
"""How rarely the earning celebration may fire - banner and spoken line
together. Interrupting a child every ten seconds to praise them for earning zero
minutes is how a reward becomes a nuisance, and D-45 asks for a small one."""


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

    end_action_sessions: frozenset[str] = frozenset()
    """Which session ids the last end action was applied against.

    Not a boolean. A boolean fires once and then never again, so a child whose
    session was logged out and who logs straight back in — a new session id, the
    same day, the same exhausted quota — is never enforced against for the rest
    of the day. Comparing session ids re-fires exactly when there is something
    new to fire at."""
    end_action_failures: int = 0
    end_action_next_attempt: float = 0.0
    """Backoff, in BOOTTIME seconds. An end action that returned false is
    retried; "audited and then forgotten" is `continue unprotected`."""

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
    """Earned seconds not yet announced. A tick's payout is ten seconds at the
    default ratio, and `10 // 60` is the zero the child was congratulated for."""
    earned_announced_at: float = 0.0
    """When the last celebration went out, in BOOTTIME seconds. Zero means never,
    which is why the first one is never throttled."""

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
    _dns_filter: DnsFilter | None = None
    filter_health: FilterHealth | None = None
    _filter_checked_at: float | None = None
    _notifier: Notifier | None = None
    _notified_at: float | None = None

    @property
    def dns_filter(self) -> DnsFilter:
        """The children's DNS filter (D-167), its files under the data directory."""
        if self._dns_filter is None:
            self._dns_filter = DnsFilter(
                self.config.paths.data_dir / "dns", panel_port=self.config.panel.port
            )
        return self._dns_filter

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._session = self.database.factory()
        self._ledger = TimeLedger(self._session, self.clock, self.config)
        reading = self._ledger.begin()
        # Writes the kiosk policy too, because the policy carries the children's
        # web tiles and so has to follow every change to them.
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

    def _write_kiosk_policy(self) -> None:
        """Put the child's Chromium under managed policy (T5.2).

        Here rather than in the launcher because this is the only BioKids
        process that can: `/etc/chromium/policies/managed` is root-owned, and
        this unit is the one whose `ReadWritePaths` names it. The launcher runs
        as the child and can only check the result.

        One file covers every child, because Chromium reads only the
        machine-wide directory. It carries the launcher's origin and the hosts
        of every web tile enabled for any child (D-156), which is why `reload`
        rewrites it: enabling EBA in the panel has to reach the browser.

        Everything about this was already in place - the config key, the
        ReadWritePath with a comment explaining its `-` prefix, the postrm that
        removes the file on purge - except the line that writes it. So
        `chrome://policy` was empty on every machine and the kiosk was an
        ordinary browser with an ordinary Ctrl+Shift+I.
        """
        from biokids_core.browser_policy import POLICY_FILENAME, kiosk_policy

        directory = self.config.paths.chromium_policy_dir
        origin = f"{self.config.shell.host}:{self.config.shell.port}"
        path = directory / POLICY_FILENAME
        try:
            directory.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".json.tmp")
            temp.write_text(
                json.dumps(
                    kiosk_policy(origin, self._web_tile_hosts(), self._browser_mode()),
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temp.replace(path)
        except OSError as exc:
            # Loud, and not fatal. A daemon that refuses to start because
            # Chromium is not installed would be worse than a kiosk without a
            # policy, and `biokids-shell` is what pulls Chromium in.
            log.error(
                "could not write the Chromium managed policy to %s (%s). If a child "
                "uses the browser it will have NO policy: no URL allowlist, DevTools "
                "available, downloads unrestricted.",
                path,
                exc,
            )
            return
        log.info("Chromium managed policy written to %s for %s", path, origin)

    def _browser_mode(self) -> str:
        """`allowlist` if any managed child is on "selected sites only" (D-167).

        Chromium reads one policy for the whole machine, so the strictest child
        decides. Open internet is the default; a family that chose otherwise for
        one child chose it for this computer's browser.
        """
        for runtime in self.children.values():
            policy = runtime.policy
            if policy is not None and policy.filter_mode == FilterMode.ALLOWLIST:
                return "allowlist"
        return "blocklist"

    def _apply_dns_filter(self) -> None:
        """Every managed child's DNS through the filter (D-167)."""
        users = [r.unix_user for r in self.children.values() if r.policy is not None]
        hosts = {pattern.split("/", 1)[0] for pattern in self._web_tile_hosts()}
        hosts.discard(f"{self.config.shell.host}:{self.config.shell.port}")
        self.dns_filter.apply(users, sorted(hosts))

    def _web_tile_hosts(self) -> list[str]:
        """What the web tiles enabled for any child may open, `www.` dropped.

        Dropped because a host without a scheme also allows its subdomains in
        Chromium's URL filter, so `trtcocuk.net.tr` covers `www.` and anything
        the site sends a child on to; `www.trtcocuk.net.tr` would not cover the
        bare domain.

        A tile's catalog entry may name exactly what it needs instead (D-164):
        KÜRE Çocuk only its children's section, YouTube Kids also the Google
        sign-in check it makes on every visit. Deriving a path from every URL
        was tried first and broke Google Earth, which moves itself from /web to
        /static the moment it loads. The filter governs pages and frames, not
        the images and scripts a page loads, so a site's CDN needs no entry.
        """
        try:
            catalog = {entry.slug: entry for entry in load_catalog(config=self.config)}
        except ConfigError:
            # No catalog file: every tile gets its own host, which is safe.
            catalog = {}
        patterns: set[str] = set()
        for child_id in self.children:
            for _link, app in repo.list_child_apps(self.session, child_id):
                if app.kind != "web" or not app.url:
                    continue
                entry = catalog.get(app.slug)
                if entry is not None:
                    patterns.update(entry.allowed_patterns())
                else:
                    pattern = web_tile_pattern(app.url)
                    if pattern:
                        patterns.add(pattern)
        # Sites a parent said yes to when a child asked (D-173).
        patterns.update(
            rule.pattern
            for rule in self.session.scalars(
                select(FilterRule).where(
                    FilterRule.kind == "allow", FilterRule.child_id.in_(list(self.children))
                )
            )
            if rule.pattern
        )
        return sorted(patterns)

    def reload(self) -> None:
        """Re-read children and policies. Called at start and on a `reload` op.

        A policy change from the panel must take effect within one tick, and the
        panel is a separate process writing the same database, so the daemon
        cannot cache indefinitely. It also must not re-query on every tick — that
        is a database round trip per child per ten seconds forever.

        A reload that arrives while the daemon is still starting is ignored, not
        an error: the panel may finish the wizard a moment before the daemon is
        ready, and `start()` reads the policies itself anyway.
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
            # The same freshness test the ledger got. Without it the two
            # disagree: the ledger stops charging a category the child has left
            # while the engine keeps applying its limit, so `remaining_seconds`
            # stays pinned to an app that was closed twenty minutes ago.
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

            # Re-armed every tick rather than only when a parent presses
            # "+10 dk". Any of half a dozen things can put the end of the day
            # back - the quota edited on the settings screen, a window widened,
            # a break ending, the policy reloaded - and all of them used to
            # leave `warned` full, so the extra time ran out in silence and the
            # machine shut down with no warning at all. The set is cheap and the
            # ledger only marks the child dirty when it actually changes.
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
        # an outright `NONE`. A break that is followed by a warning rather than
        # by a clear tick used to leave the slot open for the rest of the day.
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

        The session is deliberately *not* locked. `loginctl lock-session` hands a
        four-year-old a password prompt, and nothing in BioKids can unlock it —
        a five-minute break would end with a child waiting for a parent. UX.md
        §2.5 asks for a calm full-screen timer, which the launcher already draws.

        What does have to happen is stopping the applications, once. The
        launcher is a Chromium window like any other, so a break that arrives
        while the child is inside a fullscreen game renders behind the game and
        enforces nothing.
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

    async def _run_end_sequence(
        self,
        runtime: ChildRuntime,
        presence: UserPresence,
        decision: Decision,
        day: dt.date,
        now_boot: float,
    ) -> None:
        """Lock, count down, then do what the parent chose (`ARCHITECTURE.md` §3.5).

        Only reached for the two reasons that end the day. A break returns
        `Action.LOCK` too and must never arrive here: it would open the countdown
        slot, and the next genuine exhaustion would find a timestamp already past
        the countdown and shut the machine down with no lock screen at all.

        Three things are re-asserted on every tick rather than done once, because
        "done once" is how a child who gets back in ends up unsupervised for the
        rest of the day:

        * the **lock**, against any session that is present and not already
          locked, so a dismissed lock comes back within one tick;
        * the **end action**, whenever a session id appears it has not been
          applied against — logind hands out a fresh id on every login;
        * a **retry**, when the last attempt returned false, with backoff.
        """
        if not presence.present:
            # Nobody to end the day for. The quota really is exhausted, but this
            # child has no session: there is no screen to lock, and the two
            # remaining end actions reach past them - poweroff takes the whole
            # machine, logout ends sessions by user name.
            #
            # Without this the sequence ran for an absent child on every tick,
            # found nothing to lock, and went on to power the machine off. The
            # mother logs in that evening and two minutes later it shuts down
            # under her; with a bedtime window it happens every evening whether
            # or not the child ever touched it. Poweroff is the default, so this
            # was the out-of-the-box behaviour.
            #
            # The countdown is not started either, so it begins when they come
            # back rather than having expired in their absence.
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
            # The backoff was dead code. `already_done` is only true once the
            # action has succeeded at least once, so an action that has never
            # worked - polkit refusing poweroff, an inhibitor holding it, an
            # empty session list - fell straight past the guard and was retried
            # every tick, writing an alert-severity audit row each time and
            # re-escalating to lockdown every thirty seconds for the rest of the
            # day. The retry schedule existed and nothing consulted it.
            return

        action = self._end_action_for(runtime, presence, policy)
        # Applications first, so an unsaved drawing gets its SIGTERM before the
        # machine starts going down.
        await self.enforcer.stop_all_apps(runtime.unix_user)
        ok = await self.enforcer.apply_end_action(
            action, runtime.unix_user, presence.lockable_session_ids
        )

        if ok:
            # `frozenset({""})` rather than an empty set for the poweroff case,
            # where there is nothing to enumerate: an empty set would read as
            # "never applied" and re-fire on the next tick.
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
            # Three failures in a row. Stop hoping and take the D-106 path: lock
            # every session this child has, loudly. A parental control that has
            # quietly given up is exactly what principle #3 exists to prevent.
            log.error(
                "the end action for %s has failed %s times; falling back to lockdown",
                runtime.unix_user,
                runtime.end_action_failures,
            )
            targets = presence.lockable_session_ids
            if not targets:
                # Nothing to lock. Recording an alert-level "lockdown" here
                # would tell a parent reading the audit log that the machine was
                # secured when nothing happened at all - and an empty session
                # list is one ordinary way to fail three times in the first
                # place, so this is the likely case rather than the odd one.
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

    def _others_logged_in(self, runtime: ChildRuntime) -> tuple[str, ...]:
        """Which other human users have a session right now.

        Everyone with a `user` session who is not this child. Another child
        counts: ending Elif's day must not log Deniz out mid-drawing either.
        """
        others: list[str] = []
        for unix_user, presence in self.watcher.all_presence.items():
            if unix_user == runtime.unix_user:
                continue
            if presence.lockable_session_ids:
                others.append(unix_user)
        return tuple(sorted(others))

    def _end_action_for(
        self,
        runtime: ChildRuntime,
        presence: UserPresence,
        policy: ChildPolicy | None,
    ) -> EndAction:
        """The end action, narrowed to what may be done to this machine right now.

        `poweroff` and `logout` are the two that reach past the child. Poweroff
        takes the whole machine; logout ends sessions by user name. Neither is
        acceptable while somebody else is sitting at the same computer - a
        parent working, or a sibling drawing - so both degrade to locking the
        child's own screen, which is the part that was ever about this child.

        The child still loses the machine. Nobody else does.
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

    async def _unlock(self, runtime: ChildRuntime) -> None:
        """Let the child back into a session BioKids locked.

        The counterpart to `_lock`, and for a long time there was none: the
        enforcer could lock and could unlock, and only one of the two was ever
        called. On an account with no password - which is every child account,
        by D-99 - a lock nobody lifts is a child who cannot get back in at all.
        """
        presence = self.watcher.presence(runtime.unix_user)
        # Every session with a screen, not only the ones logind calls locked:
        # light-locker never sets LockedHint, so on a real machine that filter
        # found nothing and the grant unlocked nobody (D-155). Unlocking a
        # session that is not locked is a no-op.
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
        # The end of the day re-asserts the lock every tick. Whether the last
        # one took is read from this tick's snapshot: a session that is still
        # on screen and unlocked. Not LockedHint alone - light-locker never sets
        # it (D-155); it locks by handing the seat to the greeter, which makes
        # the child's session inactive. If a session stays on screen, the
        # child's screen locker is not there - `loginctl lock-session` only
        # asks - and the child carries on. Ending the session is the fail-safe.
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

    def _on_earned(self, runtime: ChildRuntime, seconds: int, now_boot: float) -> None:
        """Celebrate earned minutes - once they are minutes, and not every tick.

        `seconds` is this tick's payout. The first one clears the streak
        threshold and is worth ten minutes; every one after it is a single tick,
        ten seconds at the default ratio, and `10 // 60` is zero. So the child
        was congratulated for earning zero minutes, every ten seconds, for the
        length of their homework - spoken aloud, and drawn across their screen
        as a banner, because `shell.js` renders the same event.

        The payouts therefore accumulate and are announced in whole minutes, no
        more often than EARN_SPEECH_INTERVAL, as one celebration: banner and
        voice together. D-45 asks for "a small celebration", and something that
        interrupts a child every ten seconds to praise them for nothing is not
        small, and stops reading as praise.
        """
        runtime.earned_pending_s += seconds
        minutes = runtime.earned_pending_s // 60
        if minutes < 1:
            return
        # `earned_announced_at` is zero until the first celebration, and the
        # first one must not be swallowed by the interval: it is the ten-minute
        # payout that clears the streak threshold, and it is the one the child
        # is actually waiting for.
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

    def _publish_speech(self, runtime: ChildRuntime, decision: Decision) -> None:
        """Ask for a line. The daemon never speaks; the shell relays to the voice
        service, which runs inside the child's session where the audio is."""
        if decision.speak is not None:
            self._speak(runtime, decision.speak)

    def _speak(self, runtime: ChildRuntime, speak: SpeechRequest) -> None:
        """The one place a spoken line leaves the daemon.

        Every line goes through here so that `voice.enabled` means what it says.
        The earning celebration used to publish SPEAK for itself and was
        therefore the single line a parent could not turn off.
        """
        if not self.config.voice.enabled:
            return
        if runtime.policy is not None and not runtime.policy.voice_enabled:
            # The per-child switch, which the wizard has always offered and
            # nothing ever honoured. A parent who turns the voices off for one
            # child still gets the card on screen; it is the sound that stops.
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

    # ---------------------------------------------------------- the filter

    async def check_filter(self, now_boot: float) -> None:
        """Check the children's DNS filter from the outside, once a minute (D-172).

        The filter was written, loaded and then trusted. `nft -f` failing was a
        log line; `systemctl restart nftables` - which flushes every table -
        removed the children's redirect until the daemon next reloaded, and the
        panel said "running" because dnsmasq was. So: is the redirect there, and
        does the filter refuse a name every adult list has?

        Open - the children could reach what the lists close - is principle 3:
        one repair is tried, and if the filter is still open every child at the
        machine is locked, and unlocked again when it closes. Not answering is
        already closed (no DNS, no internet) and is reported, not locked.
        """
        if self._filter_checked_at is not None and (
            now_boot - self._filter_checked_at < FILTER_CHECK_SECONDS
        ):
            return
        self._filter_checked_at = now_boot
        users = [r.unix_user for r in self.children.values() if r.policy is not None]
        if not users:
            self.filter_health = None
            return
        health = await asyncio.to_thread(self.dns_filter.verify, users)
        if health.open or not health.answering:
            log.warning("the DNS filter is not right (%s); applying it again", health)
            await asyncio.to_thread(self._apply_dns_filter)
            health = await asyncio.to_thread(self.dns_filter.verify, users)

        previous = self.filter_health
        self.filter_health = health
        if health.open:
            if previous is None or not previous.open:
                repo.audit(
                    self.session,
                    actor="daemon",
                    action="filter_failed",
                    detail={"redirect": health.redirect, "filtering": health.filtering},
                    severity="alert",
                )
            for runtime in self.children.values():
                presence = self.watcher.presence(runtime.unix_user)
                if presence.lockable_session_ids:
                    runtime.locked_for_filter = True
                    await self._lock(runtime, presence, only_unlocked=False)
        elif not health.answering:
            if previous is None or previous.answering or previous.open:
                repo.audit(
                    self.session,
                    actor="daemon",
                    action="filter_down",
                    detail={"error": self.dns_filter.last_error},
                    severity="alert",
                )
        elif previous is not None and (previous.open or not previous.answering):
            repo.audit(self.session, actor="daemon", action="filter_restored")
        if not health.open:
            for runtime in self.children.values():
                if runtime.locked_for_filter:
                    runtime.locked_for_filter = False
                    await self._unlock(runtime)
        self.session.commit()

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
            # D-167: so a parent can see that harmful sites are really being
            # stopped, and why not when they are not.
            "dns_filter": {
                # From the last outside check rather than a `systemctl` call per
                # panel poll, which ran synchronously on the event loop.
                "running": self.filter_health is None
                or (self.filter_health.answering and not self.filter_health.open),
                "open": bool(self.filter_health and self.filter_health.open),
                "error": self.dns_filter.last_error,
                "mode": self._browser_mode(),
            },
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

        # The whole allowance, earned minutes included - not the bare quota.
        # The shell draws its ring as remaining/quota and clamps to 1, so a
        # child who earned thirty bonus minutes watched a full circle that did
        # not move for half an hour: the one thing a four-year-old is meant to
        # be able to read said nothing was being used.
        quota = general_allowance_seconds(policy, state) if policy else 0
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
            "asked_for_time": self.pending_request(child_id, "time"),
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
            # Which of the three end actions is configured (D-43). The launcher
            # cannot read the policy - the database is 0600 root - so without
            # this its time-up screen told every child the computer was about to
            # shut down, including the two cases where it was not.
            "end_action": policy.end_action.value if policy else "",
            # Whether to show the assistant's tile and its microphone button
            # (D-165, D-166). The daemon re-checks both on every question.
            "assistant": {
                "enabled": bool(policy and policy.assistant_enabled),
                "voice": bool(policy and policy.assistant_enabled and policy.assistant_voice),
            },
            "countdown_seconds": self._countdown_remaining(
                runtime, day, reading.boot, reading.boot_id
            ),
        }

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

        Anyone, not just a sibling. This used to iterate `self.children`, so it
        refused when the other user was another managed child and allowed it
        when the other user was a parent: mum switches user so her son can play,
        he taps "Kapat" when he is finished, and the machine goes down with her
        session and her unsaved work still open. The code already knew this harm
        was worth refusing; it was only looking in the wrong list.
        """
        runtime = self._require(child_id)
        # Ask logind now rather than trusting the last tick's snapshot. This is
        # a rare and irreversible action taken on demand, and up to ten seconds
        # of staleness is exactly long enough for a parent to have just sat
        # down at their own account.
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

    def file_access_request(self, child_id: int, url: str, kind: str = "site") -> int:
        """ "Anneme sor" (D-65, D-173). Turns the filter from a wall into a conversation.

        Returns the request's id, or 0 when the child has asked too often. The
        same question asked twice while it waits is one request: a child who
        presses the button five times is asking once, impatiently.
        """
        runtime = self._require(child_id)
        kind = kind if kind in REQUEST_KINDS else "site"
        url = url[:1024] if kind != "time" else ""
        self._expire_requests()
        waiting = self.session.scalar(
            select(AccessRequest).where(
                AccessRequest.child_id == child_id,
                AccessRequest.status == "pending",
                AccessRequest.kind == kind,
                AccessRequest.url == url,
            )
        )
        if waiting is not None:
            return int(waiting.id)
        recent = self.session.scalar(
            select(func.count())
            .select_from(AccessRequest)
            .where(
                AccessRequest.child_id == child_id,
                AccessRequest.created_at >= utcnow() - dt.timedelta(hours=1),
            )
        )
        if int(recent or 0) >= REQUESTS_PER_HOUR:
            log.info(
                "%s asked %s times this hour; not filing another", runtime.unix_user, recent
            )
            return 0
        row = AccessRequest(child_id=child_id, url=url, kind=kind, status="pending")
        self.session.add(row)
        repo.audit(
            self.session,
            actor=f"child:{runtime.unix_user}",
            action="access_requested",
            target=f"child:{child_id}",
            detail={"url": url, "kind": kind},
        )
        self.session.commit()
        self.bus.publish(
            EventKind.ACCESS_REQUEST,
            child_id=child_id,
            url=url,
            request_kind=kind,
            request_id=row.id,
        )
        return int(row.id)

    def _expire_requests(self) -> None:
        cutoff = utcnow() - REQUEST_EXPIRY
        for row in self.session.scalars(
            select(AccessRequest).where(
                AccessRequest.status == "pending", AccessRequest.created_at < cutoff
            )
        ):
            row.status = "expired"
            row.decided_at = utcnow()

    def pending_request(self, child_id: int, kind: str) -> bool:
        return (
            self.session.scalar(
                select(AccessRequest.id).where(
                    AccessRequest.child_id == child_id,
                    AccessRequest.status == "pending",
                    AccessRequest.kind == kind,
                    AccessRequest.created_at >= utcnow() - REQUEST_EXPIRY,
                )
            )
            is not None
        )

    async def decide_request(
        self, request_id: int, approve: bool, minutes: int = 0, actor: str = "parent"
    ) -> dict[str, Any]:
        """A parent's answer to a child's question, carried out and told to the child.

        Yes to more time is a grant (which also lets a locked child back in);
        yes to a site allows its host for that child, by policy and by DNS; yes
        to an app enables it on their screen. No is no, said kindly. Either way
        the child hears it from Zio or Sio and sees it on screen, because a
        question that disappears into silence teaches a child not to ask.
        """
        row = self.session.get(AccessRequest, request_id)
        if row is None or row.status != "pending":
            return {"ok": False, "error": "error.not_found"}
        runtime = self._require(row.child_id)
        if approve and row.kind == "time" and not 0 < minutes <= 12 * 60:
            return {"ok": False, "error": "error.invalid_input"}

        row.status = "approved" if approve else "denied"
        row.decided_at = utcnow()
        row.minutes = minutes if approve and row.kind == "time" else 0
        host = web_tile_pattern(row.url) if row.url else ""
        if approve and row.kind == "site" and host:
            self.session.add(
                FilterRule(child_id=row.child_id, kind="allow", pattern=host, source="request")
            )
        if approve and row.kind == "app" and self.session.get(App, row.url) is not None:
            enable_for_child(self.session, row.child_id, [row.url], replace=False)
        repo.audit(
            self.session,
            actor=actor,
            action="request_approved" if approve else "request_denied",
            target=f"child:{row.child_id}",
            detail={"kind": row.kind, "url": row.url, "minutes": row.minutes},
        )
        self.session.commit()

        if approve and row.kind == "time":
            await self.grant_time(row.child_id, minutes * 60, actor)
        elif approve:
            self.reload()

        self.bus.publish(
            EventKind.REQUEST_DECIDED,
            child_id=row.child_id,
            approved=approve,
            request_kind=row.kind,
            minutes=row.minutes,
        )
        self._speak(
            runtime,
            SpeechRequest(
                event="request_approved" if approve else "request_denied",
                vars={"minutes": row.minutes, "name": runtime.display_name},
                priority="high",
                expression="happy" if approve else "neutral",
            ),
        )
        return {"ok": True, "status": row.status}

    async def unlock_now(self, child_id: int, actor: str = "parent") -> bool:
        """The "Kilidi aç" button: back in, without changing the day's time.

        A lock from the panel had no way back except "+10 dk", which gave time
        the parent may not have meant to give. If the day is over or a window is
        closed, the launcher still shows that; only the lock comes down.
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

        Async because of that second half. The comment below used to say "the
        lock screen comes down" and nothing brought it down: `_clear_end_sequence`
        clears the daemon's own bookkeeping, not logind's. `Enforcer.unlock_sessions`
        existed and was called from nowhere in the whole product.

        What that cost: the quota runs out, the screen locks, the child asks for
        more time, the parent presses "+10 dk" on their phone, the panel says
        "Elif icin 10 dakika eklendi" and the card shows 10:00 - and the child is
        still at the password prompt of an account that deliberately has no
        password (D-99), with nothing in BioKids able to let them in.
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
        # The extension pushed the end of the day back, so the lock screen comes
        # down and the warnings that are now in the future can fire again.
        self._clear_end_sequence(runtime, day, decision.remaining_seconds)
        if decision.allow and decision.remaining_seconds > 0:
            # Only when the grant actually bought time. A "+10 dk" that still
            # leaves the window closed must not open a screen the window is
            # supposed to keep shut.
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


TICK_TIMEOUT_SECONDS = 60
"""The longest one tick may take before it counts as failed."""

TICK_FAILURES_BEFORE_GIVING_UP = 3
"""Consecutive failed ticks before the loop stops pretending it is supervising.

Three rather than one, because a single D-Bus hiccup is not a reason to lock a
child out of a machine a parent then has to explain. Three rather than never,
because a loop that catches everything forever is a daemon that reports
`active (running)` to systemd while enforcing nothing at all."""


async def run_forever(supervisor: Supervisor, stop_event: asyncio.Event) -> None:
    """Tick until asked to stop, and survive a tick that throws - up to a point.

    One bad tick is logged and the loop continues: a transient D-Bus or SQLite
    hiccup is the wrong reason to lock a child's screen.

    `TICK_FAILURES_BEFORE_GIVING_UP` in a row is different. It means nothing is
    being accounted for and nothing is being enforced, and the process staying
    alive is what hides that: systemd sees `active (running)`, so `Restart=`
    never fires, `OnFailure=biokids-lockdown.service` never fires, and there is
    no WatchdogSec. The child sits at an unsupervised machine while the panel
    renders the last numbers that were committed.

    So the loop locks every child session itself - D-106's answer, and it needs
    no database, which matters because the database is the likeliest thing that
    just broke - and then raises, so the unit exits non-zero and systemd's own
    net catches what is left.
    """
    interval = supervisor.config.daemon.tick_seconds
    limit = max(TICK_TIMEOUT_SECONDS, 3 * interval)
    failures = 0
    while not stop_event.is_set():
        try:
            # Bounded: a tick that never returns is a failed tick, not a daemon
            # that looks alive while enforcing nothing.
            await asyncio.wait_for(supervisor.tick(), timeout=limit)
            now_boot = supervisor.clock.read().boot
            await asyncio.wait_for(supervisor.check_filter(now_boot), timeout=limit)
            failures = 0
            # Outside the failure count: mail is a convenience, enforcement is not.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(supervisor.notify_parent(now_boot), timeout=limit)
            # Only a tick that finished pings the watchdog; WatchdogSec= in
            # the unit kills a daemon whose ticks stopped finishing.
            notify.watchdog()
        except Exception as exc:  # one bad tick must not stop enforcement
            failures += 1
            log.exception("tick failed (%s in a row)", failures)
            # The daemon's one long-lived Session: a failed flush anywhere in a
            # tick left it needing a rollback, and every later tick then died
            # on PendingRollbackError until the loop gave up.
            with contextlib.suppress(Exception):
                supervisor.session.rollback()
            if failures >= TICK_FAILURES_BEFORE_GIVING_UP:
                await _give_up(supervisor, failures, exc)
                raise
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue


async def _give_up(supervisor: Supervisor, failures: int, exc: BaseException) -> None:
    """Lock every child session, say so if the database still answers, and leave.

    Nothing here may depend on the database: the commonest cause of three failed
    ticks is the database itself. The lock goes through `loginctl`, and the audit
    row is attempted afterwards and allowed to fail.
    """
    log.error(
        "%s ticks in a row failed (%s); locking every child session and exiting",
        failures,
        exc,
    )
    # Ask logind who is here rather than trusting the last snapshot: the tick
    # may well have died before it got as far as refreshing presence, and a
    # lockdown with an empty session list is the "audited and then forgotten"
    # outcome D-106 exists to prevent.
    with contextlib.suppress(Exception):
        await supervisor.watcher.refresh()
    sessions: list[str] = []
    for runtime in supervisor.children.values():
        sessions.extend(supervisor.watcher.presence(runtime.unix_user).lockable_session_ids)
    with contextlib.suppress(Exception):
        await supervisor.enforcer.lockdown(tuple(sessions))
    with contextlib.suppress(Exception):
        supervisor.bus.publish(
            EventKind.DAEMON_STATE, state="failed", failures=failures, error=str(exc)[:200]
        )
    with contextlib.suppress(Exception):
        repo.audit(
            supervisor.session,
            actor="daemon",
            action="daemon_failed",
            detail={"consecutive_tick_failures": failures, "error": str(exc)[:200]},
            severity="alert",
        )
        supervisor.session.commit()


def web_tile_pattern(url: str) -> str:
    """The host of a web tile the catalog no longer describes, without `www.`."""
    return (urlsplit(url).hostname or "").removeprefix("www.")
