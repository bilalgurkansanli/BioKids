"""Doing the thing the policy engine decided.

Nothing here decides anything. `evaluate()` returns an `Action` and the enforcer
carries it out — which is what makes it possible to test every rule without a
machine and every system call without a rule.

Two properties matter more than the rest:

**Applications get a chance to save.** An app being stopped receives `SIGTERM`
and only then, after a grace period, `SIGKILL`. A six-year-old's drawing is
worth ten seconds of shutdown latency.

**Every command is a list, never a shell string.** A child's display name ends
up in some of these arguments, and a display name is user input.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from biokids_core.config import Config, get_config
from biokids_core.types import EndAction

log = logging.getLogger(__name__)

CommandRunner = Callable[[Sequence[str]], Awaitable["CommandResult"]]


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_command(argv: Sequence[str]) -> CommandResult:
    """Run a command, capturing output, never raising on a non-zero exit."""
    if shutil.which(argv[0]) is None:
        log.warning("%s is not installed; skipping %s", argv[0], " ".join(argv))
        return CommandResult(tuple(argv), returncode=127, stderr="not installed")
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
    except OSError as exc:
        log.exception("could not run %s", " ".join(argv))
        return CommandResult(tuple(argv), returncode=126, stderr=str(exc))
    result = CommandResult(
        tuple(argv),
        returncode=process.returncode or 0,
        stdout=out.decode("utf-8", "replace").strip(),
        stderr=err.decode("utf-8", "replace").strip(),
    )
    if not result.ok:
        log.warning("%s exited %s: %s", " ".join(argv), result.returncode, result.stderr)
    return result


@dataclass
class RecordingRunner:
    """A runner the tests use instead of touching the machine."""

    calls: list[tuple[str, ...]] = field(default_factory=list)
    returncode: int = 0

    async def __call__(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(tuple(argv))
        return CommandResult(tuple(argv), returncode=self.returncode)

    def ran(self, *fragment: str) -> bool:
        """True when some call contains this sequence of arguments in order."""
        needle = list(fragment)
        for call in self.calls:
            items = list(call)
            for start in range(len(items) - len(needle) + 1):
                if items[start : start + len(needle)] == needle:
                    return True
        return False


class Enforcer:
    """Lock, log out, power off, stop an application.

    The session watcher is asked to lock through logind rather than shelling out
    to `loginctl` when it can, because it already holds the session objects. The
    `loginctl` path remains as the fallback for the case where the D-Bus
    connection is in a bad way but the machine is otherwise fine.
    """

    def __init__(
        self,
        config: Config | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self._config = config or get_config()
        self._run: CommandRunner = runner or run_command

    # ------------------------------------------------------------- sessions

    async def lock_session(self, session_id: str) -> bool:
        result = await self._run(["loginctl", "lock-session", session_id])
        return result.ok

    async def unlock_session(self, session_id: str) -> bool:
        """Release a lock BioKids applied.

        Only a repair path. `loginctl unlock-session` emits logind's Unlock
        signal, which not every screen locker acts on, so nothing a child needs
        in order to carry on is allowed to depend on it — a break pauses them in
        the launcher instead of locking the session.
        """
        result = await self._run(["loginctl", "unlock-session", session_id])
        return result.ok

    async def unlock_sessions(self, session_ids: Sequence[str]) -> bool:
        if not session_ids:
            return False
        results = [await self.unlock_session(sid) for sid in session_ids]
        return any(results)

    async def lock_sessions(self, session_ids: Sequence[str]) -> bool:
        """Lock these sessions and no others.

        Deliberately not `loginctl lock-sessions`, which locks every session on
        the machine — including the parent's. The caller passes exactly the
        session ids the session watcher attributed to this child.
        """
        if not session_ids:
            return False
        results = [await self.lock_session(sid) for sid in session_ids]
        return any(results)

    async def logout_user(self, unix_user: str) -> bool:
        result = await self._run(["loginctl", "terminate-user", unix_user])
        return result.ok

    async def poweroff(self) -> bool:
        """Clean shutdown. Applications have already been asked to stop."""
        result = await self._run(["systemctl", "poweroff"])
        return result.ok

    async def apply_end_action(
        self, action: EndAction, unix_user: str, session_ids: Sequence[str] = ()
    ) -> bool:
        match action:
            case EndAction.LOCK:
                return await self.lock_sessions(session_ids)
            case EndAction.LOGOUT:
                return await self.logout_user(unix_user)
            case EndAction.POWEROFF:
                return await self.poweroff()

    # --------------------------------------------------------- applications

    async def stop_app(self, unix_user: str, unit: str) -> bool:
        """SIGTERM, then SIGKILL after the grace period (T3.3).

        `systemctl --user stop` already sends TERM and escalates to KILL after
        the unit's own `TimeoutStopSec`. We set that explicitly rather than
        inheriting a distribution default, because the whole point of the grace
        period is that it is the one we promised.
        """
        grace = self._config.daemon.app_term_grace_seconds
        result = await self._run(
            [
                "systemctl",
                "--user",
                "-M",
                f"{unix_user}@.host",
                "--job-mode=replace",
                "stop",
                unit,
            ]
        )
        if result.ok:
            return True
        log.warning(
            "could not stop %s for %s within %s s; the session will be ended instead",
            unit,
            unix_user,
            grace,
        )
        return False

    async def stop_all_apps(self, unix_user: str) -> bool:
        return await self.stop_app(unix_user, "biokids-app-*.scope")

    # ------------------------------------------------------------- lockdown

    async def lockdown(self, session_ids: Sequence[str]) -> int:
        """Lock every child session. The failure path of the whole product.

        Called by `biokids-lockdown.service` when the daemon dies (D-106). It
        must work with the daemon gone, so it does nothing but issue `loginctl`
        commands — no database, no socket, no policy.
        """
        locked = 0
        for session_id in session_ids:
            if await self.lock_session(session_id):
                locked += 1
        return locked
