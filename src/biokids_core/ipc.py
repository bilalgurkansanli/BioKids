"""The daemon IPC protocol.

Line-delimited JSON over `/run/biokids/daemon.sock`, group `biokids`, mode 0660.
Not DBus: a Unix socket is easier to test, easier to secure, and has none of
DBus' session-bus lifetime problems (`ARCHITECTURE.md` §2).

    {"op": "get_status", "id": "7"}\\n
    {"id": "7", "ok": true, "data": {...}}\\n

One request per line, one response per line, `id` echoed back. `subscribe_events`
is the exception: it acknowledges once and then streams event frames until the
client goes away, which is what drives the panel's live counters and the child
launcher's countdown ring.

This module holds the *contract* — operation names, frame shapes, framing and a
client. The server lives in `biokids_daemon.server` because only the daemon may
answer, and the vocabulary lives here because four packages speak it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

from biokids_core.errors import IpcError

MAX_LINE_BYTES: Final = 1 << 20
"""A megabyte is far more than any frame needs; anything larger is a bug or an attack."""

ENCODING: Final = "utf-8"


class Op(StrEnum):
    """Operations the daemon answers. Phase 0 implements all of these."""

    GET_STATUS = "get_status"
    """Everything the Today screen needs, for every child, in one call."""
    GET_CHILD = "get_child"
    SET_POLICY = "set_policy"
    GRANT_TIME = "grant_time"
    LOCK_NOW = "lock_now"
    POWEROFF_NOW = "poweroff_now"
    LOGOUT_NOW = "logout_now"
    START_TIMEOUT = "start_timeout"
    CLEAR_TIMEOUT = "clear_timeout"
    END_BREAK = "end_break"
    RELOAD = "reload"
    """Re-read children and policies from the database after the panel wrote."""
    PING = "ping"
    SUBSCRIBE_EVENTS = "subscribe_events"
    REPORT_ACTIVITY = "report_activity"
    """The shell tells the daemon which app has focus."""
    REQUEST_LAUNCH = "request_launch"
    """The shell asks whether a child may start an application."""
    REPORT_NAVIGATION = "report_navigation"
    """The Chromium extension reports a committed navigation (Phase 1)."""


class EventKind(StrEnum):
    """Event frames pushed to subscribers."""

    TICK = "tick"
    """Once per daemon tick, per child: remaining time and current activity."""
    WARNING = "warning"
    DECISION = "decision"
    QUOTA_EXHAUSTED = "quota_exhausted"
    END_SEQUENCE = "end_sequence"
    """The lock screen is up and the countdown to the end action is running."""
    BREAK_STARTED = "break_started"
    BREAK_FINISHED = "break_finished"
    TIMEOUT_STARTED = "timeout_started"
    TIMEOUT_FINISHED = "timeout_finished"
    EARNED = "earned"
    SESSION_CHANGED = "session_changed"
    POLICY_CHANGED = "policy_changed"
    CLOCK_JUMP = "clock_jump"
    SPEAK = "speak"
    DAEMON_STATE = "daemon_state"
    BLOCKED = "blocked"
    ACCESS_REQUEST = "access_request"


# ------------------------------------------------------------------- frames


@dataclass(frozen=True, slots=True)
class Request:
    op: str
    id: str = ""
    args: dict[str, Any] = field(default_factory=dict)

    def encode(self) -> bytes:
        return encode_frame({"op": self.op, "id": self.id, "args": self.args})

    @classmethod
    def decode(cls, raw: bytes | str) -> Request:
        payload = decode_frame(raw)
        op = payload.get("op")
        if not isinstance(op, str) or not op:
            raise IpcError("request has no op")
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            raise IpcError("args must be an object")
        return cls(op=op, id=str(payload.get("id", "")), args=args)


@dataclass(frozen=True, slots=True)
class Response:
    id: str = ""
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    """An i18n key when the failure is something a parent should read, else a
    short technical string. Never a translated sentence."""

    def encode(self) -> bytes:
        payload: dict[str, Any] = {"id": self.id, "ok": self.ok}
        if self.data:
            payload["data"] = self.data
        if self.error:
            payload["error"] = self.error
        return encode_frame(payload)

    @classmethod
    def decode(cls, raw: bytes | str) -> Response:
        payload = decode_frame(raw)
        data = payload.get("data") or {}
        return cls(
            id=str(payload.get("id", "")),
            ok=bool(payload.get("ok", False)),
            data=data if isinstance(data, dict) else {},
            error=str(payload.get("error", "")),
        )

    @classmethod
    def failure(cls, request_id: str, error: str) -> Response:
        return cls(id=request_id, ok=False, error=error)


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    child_id: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def encode(self) -> bytes:
        return encode_frame(
            {
                "event": self.kind,
                "child_id": self.child_id,
                "payload": self.payload,
                "ts": self.ts,
            }
        )

    @classmethod
    def decode(cls, raw: bytes | str) -> Event:
        payload = decode_frame(raw)
        body = payload.get("payload") or {}
        child = payload.get("child_id")
        return cls(
            kind=str(payload.get("event", "")),
            child_id=int(child) if isinstance(child, int) else None,
            payload=body if isinstance(body, dict) else {},
            ts=str(payload.get("ts", "")),
        )


# ------------------------------------------------------------------ framing


def encode_frame(payload: dict[str, Any]) -> bytes:
    """One compact JSON object, one newline. Never pretty-printed: it is a wire."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_fallback)
    return text.encode(ENCODING) + b"\n"


def decode_frame(raw: bytes | str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_LINE_BYTES:
            raise IpcError(f"frame is {len(raw)} bytes, over the {MAX_LINE_BYTES} limit")
        try:
            raw = raw.decode(ENCODING)
        except UnicodeDecodeError as exc:
            raise IpcError(f"frame is not valid {ENCODING}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise IpcError(f"frame is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise IpcError(f"frame must be a JSON object, got {type(payload).__name__}")
    return payload


def _fallback(value: Any) -> Any:
    """Serialise the value types that cross the socket: dataclasses, enums, dates."""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, StrEnum):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, set | frozenset):
        return sorted(value)
    return str(value)


# ------------------------------------------------------------------- client


class DaemonClient:
    """A blocking client. Used by the panel's request handlers and by the CLI.

    Every call opens a connection and closes it. That sounds wasteful and is
    not: a Unix socket connect is microseconds, and a long-lived connection in a
    web worker is a lifetime problem waiting to happen. `stream_events` is the
    one place a connection is held open.
    """

    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(self, op: str | Op, **args: Any) -> Response:
        request = Request(op=str(op), id=_next_id(), args=args)
        with self._connect() as conn:
            conn.sendall(request.encode())
            line = _read_line(conn)
        if line is None:
            raise IpcError("the daemon closed the connection without answering")
        return Response.decode(line)

    def available(self) -> bool:
        """True when the daemon answers a ping. The panel shows a banner when not."""
        try:
            return self.call(Op.PING).ok
        except (IpcError, OSError):
            return False

    def stream_events(self, child_id: int | None = None) -> Iterator[Event]:
        """Subscribe and yield frames until the caller stops iterating."""
        request = Request(
            op=str(Op.SUBSCRIBE_EVENTS),
            id=_next_id(),
            args={"child_id": child_id} if child_id is not None else {},
        )
        with self._connect(timeout=None) as conn:
            conn.sendall(request.encode())
            buffer = b""
            first = True
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, _, buffer = buffer.partition(b"\n")
                    if not line.strip():
                        continue
                    if first:
                        # The acknowledgement, not an event.
                        first = False
                        ack = Response.decode(line)
                        if not ack.ok:
                            raise IpcError(ack.error or "subscribe refused")
                        continue
                    yield Event.decode(line)

    def _connect(self, timeout: float | None = -1.0) -> socket.socket:
        if not hasattr(socket, "AF_UNIX"):  # pragma: no cover - Windows dev host
            raise IpcError("unix sockets are not available on this platform")
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(self.timeout if timeout == -1.0 else timeout)
        try:
            conn.connect(str(self.socket_path))
        except OSError as exc:
            conn.close()
            raise IpcError(f"cannot reach the daemon at {self.socket_path}: {exc}") from exc
        return conn


class AsyncDaemonClient:
    """The same protocol for asyncio callers — the shell and the voice service."""

    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    async def call(self, op: str | Op, **args: Any) -> Response:
        request = Request(op=str(op), id=_next_id(), args=args)
        reader, writer = await self._open()
        try:
            writer.write(request.encode())
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
        finally:
            writer.close()
            await _safe_wait_closed(writer)
        if not line:
            raise IpcError("the daemon closed the connection without answering")
        return Response.decode(line)

    async def stream_events(self, child_id: int | None = None) -> AsyncIterator[Event]:
        request = Request(
            op=str(Op.SUBSCRIBE_EVENTS),
            id=_next_id(),
            args={"child_id": child_id} if child_id is not None else {},
        )
        reader, writer = await self._open()
        try:
            writer.write(request.encode())
            await writer.drain()
            ack_line = await reader.readline()
            if not ack_line:
                return
            ack = Response.decode(ack_line)
            if not ack.ok:
                raise IpcError(ack.error or "subscribe refused")
            while True:
                line = await reader.readline()
                if not line:
                    return
                if not line.strip():
                    continue
                yield Event.decode(line)
        finally:
            writer.close()
            await _safe_wait_closed(writer)

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        # typeshed only declares open_unix_connection on POSIX, and mypy runs on
        # the Windows development host. Production is Linux (D-12).
        open_unix = cast(
            "Callable[[str], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]",
            asyncio.open_unix_connection,  # type: ignore[attr-defined]
        )
        try:
            return await asyncio.wait_for(
                open_unix(str(self.socket_path)), timeout=self.timeout
            )
        except (OSError, TimeoutError, AttributeError, NotImplementedError) as exc:
            raise IpcError(f"cannot reach the daemon at {self.socket_path}: {exc}") from exc


async def _safe_wait_closed(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):  # the peer may have gone first
        await writer.wait_closed()


def _read_line(conn: socket.socket) -> bytes | None:
    buffer = b""
    while b"\n" not in buffer:
        chunk = conn.recv(65536)
        if not chunk:
            return buffer or None
        buffer += chunk
        if len(buffer) > MAX_LINE_BYTES:
            raise IpcError("response exceeded the frame limit")
    line, _, _ = buffer.partition(b"\n")
    return line


_counter = 0


def _next_id() -> str:
    global _counter
    _counter += 1
    return str(_counter)
