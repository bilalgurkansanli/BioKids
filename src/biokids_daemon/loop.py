"""The loop that drives the supervisor, and the fail-safe when its ticks keep failing."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from biokids_core import repo
from biokids_core.ipc import EventKind
from biokids_daemon import notify
from biokids_daemon.base import log

if TYPE_CHECKING:
    from biokids_daemon.supervisor import Supervisor

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

    `TICK_FAILURES_BEFORE_GIVING_UP` in a row means nothing is being enforced,
    while systemd still sees `active (running)` and none of its own safety nets
    fire. So the loop locks every child session itself - D-106's answer, which
    needs no database, the likeliest thing to have broken - and then raises, so
    the unit exits non-zero and `OnFailure=` catches what is left.
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
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(supervisor.housekeeping(now_boot), timeout=limit)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(supervisor.refresh_assistant(now_boot), timeout=limit)
            # Only a tick that finished pings the watchdog; WatchdogSec= in
            # the unit kills a daemon whose ticks stopped finishing.
            notify.watchdog()
        except Exception as exc:  # one bad tick must not stop enforcement
            failures += 1
            log.exception("tick failed (%s in a row)", failures)
            # The daemon's one long-lived Session needs a rollback after a failed
            # flush, or every later tick fails with PendingRollbackError.
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
    # Ask logind who is here rather than trusting the last snapshot: the tick may
    # have died before refreshing presence, and a lockdown of an empty session
    # list secures nothing.
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
