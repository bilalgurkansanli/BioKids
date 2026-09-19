"""Who is actually sitting at the machine.

`ARCHITECTURE.md` §3.2 lists what a tick requires, and every item on that list is
a question about a logind session. This module answers them and reduces the
answer to one boolean per *child*, not per session: quota belongs to a Linux
user, and a fast user switch or a leftover session must never bill the same
second twice (D-40).

The D-Bus code is deliberately behind a `SessionSource` protocol. `dbus-next` is
a Linux-only dependency and the test suite runs on the developer's Windows host,
so the loop is exercised against `FakeSessionSource` and the real source is thin
enough to read in one sitting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

LOGIND_BUS = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
MANAGER_IFACE = "org.freedesktop.login1.Manager"
SESSION_IFACE = "org.freedesktop.login1.Session"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """One logind session, reduced to what enforcement cares about."""

    session_id: str
    unix_user: str
    uid: int
    seat: str
    active: bool
    """logind sets this only for the session currently in the foreground on its
    seat, so it already answers "is this the active seat?"."""
    locked: bool
    idle: bool
    state: str
    """`online` | `active` | `closing`."""
    session_class: str = "user"
    """`user` | `greeter` | `lock-screen`. Only `user` sessions consume quota."""
    session_type: str = ""
    """`x11` | `wayland` | `tty`. Recorded so an unexpected Wayland session on a
    machine we only support on X11 (D-12) is visible in the logs."""

    @property
    def counts_as_use(self) -> bool:
        return (
            self.active
            and not self.locked
            and self.state == "active"
            and self.session_class == "user"
        )


@dataclass(frozen=True, slots=True)
class UserPresence:
    """Every session one Linux user has, collapsed into one answer."""

    unix_user: str
    sessions: tuple[SessionInfo, ...] = ()

    @property
    def present(self) -> bool:
        """True when this user has a session at all, active or not."""
        return bool(self.sessions)

    @property
    def active(self) -> bool:
        """The single flag the ledger is given. Any qualifying session is enough."""
        return any(s.counts_as_use for s in self.sessions)

    @property
    def idle(self) -> bool:
        """Idle only when every session that counts is idle.

        A child with a video playing in one session and an idle terminal in
        another is not idle.
        """
        counting = [s for s in self.sessions if s.counts_as_use]
        return bool(counting) and all(s.idle for s in counting)

    @property
    def lockable_session_ids(self) -> tuple[str, ...]:
        return tuple(s.session_id for s in self.sessions if s.session_class == "user")

    @property
    def primary_session(self) -> SessionInfo | None:
        for session in self.sessions:
            if session.counts_as_use:
                return session
        return self.sessions[0] if self.sessions else None


class SessionSource(Protocol):
    """Where session facts come from. Implemented by logind and by the tests."""

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def snapshot(self) -> list[SessionInfo]: ...

    async def lock(self, session_id: str) -> bool: ...

    async def terminate(self, session_id: str) -> bool: ...


class SessionWatcher:
    """Turns a stream of session facts into per-user presence.

    Sessions that exist before the daemon starts are handled for free, because
    the state is rebuilt from a full `ListSessions` on every refresh rather than
    accumulated from signals. Signals make the daemon react faster; they are
    never the source of truth. That is a deliberate trade: a missed signal must
    not be able to leave a child unsupervised until the next reboot.
    """

    def __init__(self, source: SessionSource) -> None:
        self._source = source
        self._sessions: list[SessionInfo] = []
        self._presence: dict[str, UserPresence] = {}
        self._suspending = False

    async def start(self) -> None:
        await self._source.start()
        await self.refresh()

    async def stop(self) -> None:
        await self._source.stop()

    async def refresh(self) -> dict[str, UserPresence]:
        try:
            self._sessions = await self._source.snapshot()
        except Exception:
            log.exception("could not read logind sessions; keeping the previous snapshot")
            return self._presence

        grouped: dict[str, list[SessionInfo]] = {}
        for session in self._sessions:
            grouped.setdefault(session.unix_user, []).append(session)
        self._presence = {
            user: UserPresence(unix_user=user, sessions=tuple(sessions))
            for user, sessions in grouped.items()
        }
        return self._presence

    def presence(self, unix_user: str) -> UserPresence:
        return self._presence.get(unix_user, UserPresence(unix_user=unix_user))

    @property
    def all_presence(self) -> dict[str, UserPresence]:
        return dict(self._presence)

    @property
    def suspending(self) -> bool:
        return self._suspending

    def set_suspending(self, value: bool) -> None:
        """Set from logind's `PrepareForSleep`.

        The ledger already refuses to count suspended time — it compares
        `CLOCK_BOOTTIME` against `CLOCK_MONOTONIC` — so this flag exists to stop
        the *enforcer* acting during the moment a machine is going down, not to
        correct the accounting.
        """
        self._suspending = value

    async def lock_user(self, unix_user: str) -> int:
        locked = 0
        for session_id in self.presence(unix_user).lockable_session_ids:
            if await self._source.lock(session_id):
                locked += 1
        return locked

    async def terminate_user(self, unix_user: str) -> int:
        ended = 0
        for session_id in self.presence(unix_user).lockable_session_ids:
            if await self._source.terminate(session_id):
                ended += 1
        return ended


# --------------------------------------------------------------------- logind


class LogindSessionSource:
    """The real source: `org.freedesktop.login1` over the system bus.

    `dbus-next` is imported inside `start()` rather than at module scope so that
    the daemon package imports on a machine without it — which is every machine
    the test suite runs on.
    """

    def __init__(self) -> None:
        self._bus: Any = None
        self._manager: Any = None
        self._on_change: list[Any] = []

    async def start(self) -> None:
        from dbus_next import BusType
        from dbus_next.aio import MessageBus

        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introspection = await self._bus.introspect(LOGIND_BUS, LOGIND_PATH)
        proxy = self._bus.get_proxy_object(LOGIND_BUS, LOGIND_PATH, introspection)
        self._manager = proxy.get_interface(MANAGER_IFACE)
        log.info("connected to logind")

    async def stop(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None
            self._manager = None

    async def snapshot(self) -> list[SessionInfo]:
        if self._manager is None:
            return []
        rows = await self._manager.call_list_sessions()
        found: list[SessionInfo] = []
        for row in rows:
            session_id, uid, user_name, seat, path = (
                row[0],
                int(row[1]),
                row[2],
                row[3],
                row[4],
            )
            props = await self._session_properties(path)
            if props is None:
                continue
            found.append(
                SessionInfo(
                    session_id=str(session_id),
                    unix_user=str(user_name),
                    uid=uid,
                    seat=str(seat),
                    active=bool(props.get("Active", False)),
                    locked=bool(props.get("LockedHint", False)),
                    idle=bool(props.get("IdleHint", False)),
                    state=str(props.get("State", "online")),
                    session_class=str(props.get("Class", "user")),
                    session_type=str(props.get("Type", "")),
                )
            )
        return found

    async def _session_properties(self, path: str) -> dict[str, Any] | None:
        if self._bus is None:
            return None
        try:
            introspection = await self._bus.introspect(LOGIND_BUS, path)
            proxy = self._bus.get_proxy_object(LOGIND_BUS, path, introspection)
            properties = proxy.get_interface(PROPERTIES_IFACE)
            raw = await properties.call_get_all(SESSION_IFACE)
        except Exception:
            log.debug("session %s went away while being read", path)
            return None
        return {key: variant.value for key, variant in raw.items()}

    async def lock(self, session_id: str) -> bool:
        return await self._session_call(session_id, "call_lock")

    async def terminate(self, session_id: str) -> bool:
        return await self._session_call(session_id, "call_terminate")

    async def _session_call(self, session_id: str, method: str) -> bool:
        if self._manager is None or self._bus is None:
            return False
        try:
            path = await self._manager.call_get_session(session_id)
            introspection = await self._bus.introspect(LOGIND_BUS, path)
            proxy = self._bus.get_proxy_object(LOGIND_BUS, path, introspection)
            session = proxy.get_interface(SESSION_IFACE)
            await getattr(session, method)()
        except Exception:
            log.exception("%s failed for session %s", method, session_id)
            return False
        return True


# ----------------------------------------------------------------------- fake


@dataclass
class FakeSessionSource:
    """A session source the tests drive by hand.

    Every scenario in the ledger suite — locked, switched away, two sessions for
    one user — is expressible here, which is what lets the daemon loop be tested
    without a machine to log in to.
    """

    sessions: list[SessionInfo] = field(default_factory=list)
    locked_calls: list[str] = field(default_factory=list)
    terminated_calls: list[str] = field(default_factory=list)
    started: bool = False
    fail_snapshot: bool = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def snapshot(self) -> list[SessionInfo]:
        if self.fail_snapshot:
            raise OSError("logind is not answering")
        return list(self.sessions)

    async def lock(self, session_id: str) -> bool:
        self.locked_calls.append(session_id)
        self.sessions = [
            _with(s, locked=True) if s.session_id == session_id else s for s in self.sessions
        ]
        return True

    async def terminate(self, session_id: str) -> bool:
        self.terminated_calls.append(session_id)
        self.sessions = [s for s in self.sessions if s.session_id != session_id]
        return True

    # ------------------------------------------------------------ helpers

    def log_in(
        self,
        unix_user: str,
        *,
        session_id: str | None = None,
        uid: int = 1000,
        seat: str = "seat0",
        active: bool = True,
    ) -> SessionInfo:
        session = SessionInfo(
            session_id=session_id or f"{unix_user}-{len(self.sessions) + 1}",
            unix_user=unix_user,
            uid=uid,
            seat=seat,
            active=active,
            locked=False,
            idle=False,
            state="active" if active else "online",
            session_class="user",
            session_type="x11",
        )
        self.sessions.append(session)
        return session

    def set(self, session_id: str, **changes: object) -> None:
        self.sessions = [
            _with(s, **changes) if s.session_id == session_id else s for s in self.sessions
        ]


def _with(session: SessionInfo, **changes: object) -> SessionInfo:
    values: dict[str, Any] = {
        "session_id": session.session_id,
        "unix_user": session.unix_user,
        "uid": session.uid,
        "seat": session.seat,
        "active": session.active,
        "locked": session.locked,
        "idle": session.idle,
        "state": session.state,
        "session_class": session.session_class,
        "session_type": session.session_type,
    }
    values.update(changes)
    return SessionInfo(**values)
