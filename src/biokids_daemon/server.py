"""The IPC server on `/run/biokids/daemon.sock`.

Access control is the socket's own file mode: `root:biokids`, 0660. Anything that
can open it is already privileged enough to be trusted with these operations,
and the panel — the only writer — runs as root. The child's session has no
`biokids` group membership and cannot connect at all.

There is no authentication token on this socket, deliberately. A token stored on
the same filesystem, readable by anything that could open the socket anyway,
would be a comforting decoration rather than a control.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from biokids_core import natural_voice
from biokids_core.config import Config, get_config
from biokids_core.errors import IpcError
from biokids_core.ipc import Event, EventKind, Op, Request, Response, decode_frame
from biokids_daemon.bus import EventBus
from biokids_daemon.supervisor import Supervisor

if TYPE_CHECKING:  # pragma: no cover - the daemon only ever runs on Linux
    from asyncio import AbstractServer

# `grp`, `os.chown` and `asyncio.start_unix_server` exist only on POSIX, and
# typeshed says so. `mypy --strict` runs on the Windows host this project is
# developed from, so they are reached indirectly rather than being spelled out.
# Production is Linux (D-12) and these always resolve there.
_grp: Any
try:  # pragma: no cover - an import-time platform check
    import grp as _grp_module

    _grp = _grp_module
except ImportError:  # pragma: no cover - the Windows development host
    _grp = None

log = logging.getLogger(__name__)

MAX_GRANT_SECONDS = 12 * 3600
TOGETHER_MAX_MINUTES = 4 * 60
"""Birlikte modu (D-235) is an afternoon at most; a longer one is a typo."""
"""The most one grant may add. A day has no use for more."""
MAX_TIMEOUT_MINUTES = 24 * 60


async def cancel_connections(tasks: set[asyncio.Task[None]]) -> None:
    """Cancel every connection handler and wait for it to actually be gone.

    Both socket servers use this. A handler that is cancelled but not awaited is
    a handler that may still be running when the event loop closes, which is how
    a clean shutdown turns into a traceback in the child's journal.
    """
    pending = [task for task in list(tasks) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    tasks.clear()


Handler = Callable[[Request], Awaitable[Response]]


class DaemonServer:
    """Serves the operations of `ARCHITECTURE.md` §2 over a unix socket."""

    def __init__(
        self,
        supervisor: Supervisor,
        bus: EventBus,
        config: Config | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.bus = bus
        self.config = config or get_config()
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()
        # When a parent last pressed the assistant's "Download": progress
        # written before it belongs to an earlier download (D-254).
        self._ai_setup_started = 0.0

    @property
    def socket_path(self) -> Path:
        return self.config.paths.daemon_socket

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # A stale socket from a killed daemon would make bind fail; removing it
        # is safe because only one daemon may run and systemd guarantees that.
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

        start_unix_server = cast(
            "Callable[..., Awaitable[AbstractServer]]",
            asyncio.start_unix_server,  # type: ignore[attr-defined, unused-ignore]
        )
        self._server = await start_unix_server(self._serve, path=str(path))
        self._apply_permissions(path)
        log.info("listening on %s", path)

    def _apply_permissions(self, path: Path) -> None:
        """root:biokids 0660. A failure here is a security failure, not a nit."""
        mode = self.config.daemon.socket_mode
        try:
            path.chmod(mode)
        except OSError:
            log.exception("could not set mode %o on %s", mode, path)
        if _grp is None:  # pragma: no cover - never true on a real install
            return
        try:
            gid = int(_grp.getgrnam(self.config.daemon.socket_group).gr_gid)
            chown = cast("Callable[[Path, int, int], None]", os.chown)  # type: ignore[attr-defined, unused-ignore]
            chown(path, 0, gid)
        except KeyError:
            log.warning(
                "group %s does not exist; the socket is owned by the current group. "
                "The postinst creates this group, so this means a partial install.",
                self.config.daemon.socket_group,
            )
        except OSError:
            log.exception("could not set the group on %s", path)

    async def stop(self) -> None:
        """Stop listening, drop the subscribers, then wait.

        The order is load-bearing. Since CPython 3.12 `wait_closed()` waits for
        every connection handler task as well as the listener, and a subscribed
        panel parks in `_stream` until something cancels it - so cancelling
        after the await meant cancelling code that was never reached, and the
        daemon hung until systemd lost patience and killed it.
        """
        if self._server is not None:
            self._server.close()
        await cancel_connections(self._connections)
        if self._server is not None:
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()

    # ----------------------------------------------------------- connection

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                if not line.strip():
                    continue
                response, subscribe = await self._dispatch(line)
                writer.write(response.encode())
                await writer.drain()
                if subscribe is not None:
                    await self._stream(writer, subscribe)
                    return
        except (ConnectionResetError, BrokenPipeError):
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # a bad client must not stop the daemon
            log.exception("connection failed")
        finally:
            if task is not None:
                self._connections.discard(task)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _dispatch(self, line: bytes) -> tuple[Response, int | None]:
        """Returns the response and, for a subscribe, the child filter."""
        try:
            request = Request.decode(line)
        except IpcError as exc:
            log.warning("malformed request: %s", exc)
            return Response.failure("", "protocol.malformed"), None

        if request.op == Op.SUBSCRIBE_EVENTS:
            raw = request.args.get("child_id")
            child_id = int(raw) if isinstance(raw, int) else None
            return Response(id=request.id, ok=True, data={"subscribed": True}), child_id

        handler = self._handlers().get(request.op)
        if handler is None:
            return Response.failure(request.id, f"unknown op: {request.op}"), None
        try:
            return await handler(request), None
        except KeyError as exc:
            return Response.failure(request.id, f"not found: {exc}"), None
        except Exception:  # report, never crash the loop
            log.exception("op %s failed", request.op)
            return Response.failure(request.id, "error.unknown"), None

    async def _stream(self, writer: asyncio.StreamWriter, child_id: int | None) -> None:
        """Push events until the client goes away.

        The replay buffer is sent first so a panel that reconnects mid-countdown
        shows the countdown rather than an empty card.
        """
        subscriber = self.bus.subscribe(child_id=child_id, label="ipc")
        try:
            for event in self.bus.replay(child_id):
                writer.write(event.encode())
            await writer.drain()
            while True:
                pushed: Event = await subscriber.queue.get()
                subscriber.queue.task_done()
                writer.write(pushed.encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        finally:
            self.bus.unsubscribe(subscriber)

    # -------------------------------------------------------------- handlers

    def _handlers(self) -> dict[str, Handler]:
        return {
            Op.PING: self._ping,
            Op.GET_STATUS: self._get_status,
            Op.GET_CHILD: self._get_child,
            Op.GRANT_TIME: self._grant_time,
            Op.SEND_NOTE: self._send_note,
            Op.TOGETHER: self._together,
            Op.LOCK_NOW: self._lock_now,
            Op.LOGOUT_NOW: self._logout_now,
            Op.POWEROFF_NOW: self._poweroff_now,
            Op.REBOOT_NOW: self._reboot_now,
            Op.SWITCH_TO_GREETER: self._switch_to_greeter,
            Op.START_TIMEOUT: self._start_timeout,
            Op.CLEAR_TIMEOUT: self._clear_timeout,
            Op.END_BREAK: self._end_break,
            Op.RELOAD: self._reload,
            Op.SET_POLICY: self._set_policy,
            Op.REPORT_ACTIVITY: self._report_activity,
            Op.REQUEST_LAUNCH: self._request_launch,
            Op.ASSISTANT_STATUS: self._assistant_status,
            Op.LIVE_VIEW: self._live_view,
            Op.ASSISTANT_SETUP: self._assistant_setup,
            Op.REMOTE_SETUP: self._remote_setup,
            Op.VOICE_SETUP: self._voice_setup,
            Op.DECIDE_REQUEST: self._decide_request,
            Op.UNLOCK_NOW: self._unlock_now,
            Op.DAY_CHANGED: self._day_changed,
        }

    async def _ping(self, request: Request) -> Response:
        return Response(
            id=request.id,
            ok=True,
            data={"ticks": self.supervisor.ticks, "started": self.supervisor.started},
        )

    async def _get_status(self, request: Request) -> Response:
        return Response(id=request.id, ok=True, data=self.supervisor.status())

    async def _get_child(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        status = self.supervisor.status()
        for child in status["children"]:
            if child["child_id"] == child_id:
                return Response(id=request.id, ok=True, data=child)
        return Response.failure(request.id, "error.not_found")

    async def _grant_time(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        seconds = _int_arg(request, "seconds", default=600)
        if not 0 < seconds <= MAX_GRANT_SECONDS:
            # Negative seconds took time away under the name of a grant, and a
            # huge number overflowed nothing but switched the day's limit off.
            return Response.failure(request.id, "error.invalid_input")
        actor = str(request.args.get("actor", "parent"))
        data = await self.supervisor.grant_time(child_id, seconds, actor)
        return Response(id=request.id, ok=True, data=data)

    async def _together(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        minutes = _int_arg(request, "minutes", default=60)
        actor = str(request.args.get("actor", "parent"))
        if minutes <= 0:
            self.supervisor.stop_together(child_id, actor)
            return Response(id=request.id, ok=True, data={"child_id": child_id})
        if minutes > TOGETHER_MAX_MINUTES:
            return Response.failure(request.id, "error.invalid_input")
        data = await self.supervisor.start_together(child_id, minutes, actor)
        return Response(id=request.id, ok=True, data=data)

    async def _send_note(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        raw_parent = request.args.get("parent_id")
        try:
            note_id = self.supervisor.send_note(
                child_id,
                str(request.args.get("kind", "")),
                str(request.args.get("text", "")),
                from_name=str(request.args.get("from_name", "")),
                parent_id=int(raw_parent) if isinstance(raw_parent, int) else None,
                actor=str(request.args.get("actor", "parent")),
            )
        except ValueError:
            return Response.failure(request.id, "error.invalid_input")
        return Response(id=request.id, ok=True, data={"note_id": note_id})

    async def _lock_now(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        actor = str(request.args.get("actor", "parent"))
        ok = await self.supervisor.lock_now(child_id, actor)
        return Response(id=request.id, ok=ok, data={"locked": ok})

    async def _logout_now(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        actor = str(request.args.get("actor", "parent"))
        ok = await self.supervisor.logout_now(child_id, actor)
        return Response(id=request.id, ok=ok, data={"logged_out": ok})

    async def _poweroff_now(self, request: Request) -> Response:
        actor = str(request.args.get("actor", "parent"))
        ok = await self.supervisor.poweroff_now(actor)
        return Response(id=request.id, ok=ok, data={"poweroff": ok})

    async def _reboot_now(self, request: Request) -> Response:
        actor = str(request.args.get("actor", "parent"))
        ok = await self.supervisor.reboot_now(actor)
        return Response(id=request.id, ok=ok, data={"reboot": ok})

    async def _switch_to_greeter(self, request: Request) -> Response:
        actor = str(request.args.get("actor", "parent"))
        ok = await self.supervisor.switch_to_greeter(actor)
        return Response(id=request.id, ok=ok, data={"switched": ok})

    async def _decide_request(self, request: Request) -> Response:
        data = await self.supervisor.decide_request(
            _int_arg(request, "request_id"),
            bool(request.args.get("approve", False)),
            _int_arg(request, "minutes", default=0),
            str(request.args.get("actor", "parent")),
        )
        if not data.get("ok"):
            return Response.failure(request.id, str(data.get("error", "error.not_found")))
        return Response(id=request.id, ok=True, data=data)

    async def _unlock_now(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        ok = await self.supervisor.unlock_now(
            child_id, str(request.args.get("actor", "parent"))
        )
        return Response(id=request.id, ok=ok, data={"unlocked": ok})

    async def _day_changed(self, request: Request) -> Response:
        data = await self.supervisor.day_changed(_int_arg(request, "child_id"))
        return Response(id=request.id, ok=True, data=data)

    async def _start_timeout(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        minutes = _int_arg(request, "minutes", default=30)
        if not 0 < minutes <= MAX_TIMEOUT_MINUTES:
            return Response.failure(request.id, "error.invalid_input")
        self.supervisor.start_timeout(
            child_id, minutes, str(request.args.get("actor", "parent"))
        )
        return Response(id=request.id, ok=True)

    async def _clear_timeout(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        self.supervisor.clear_timeout(child_id, str(request.args.get("actor", "parent")))
        return Response(id=request.id, ok=True)

    async def _end_break(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        self.supervisor.end_break(child_id, str(request.args.get("actor", "parent")))
        return Response(id=request.id, ok=True)

    async def _assistant_status(self, request: Request) -> Response:
        from biokids_daemon.assistant import AssistantGate

        gate = AssistantGate(self.supervisor, self.bus, self.config)
        data = await gate.status()
        data["download"] = download_progress(
            self.config.paths.ai_models_dir, started=self._ai_setup_started
        )
        return Response(id=request.id, ok=True, data=data)

    async def _live_view(self, request: Request) -> Response:
        """A parent's panel watching a child's screen (D-253).

        Each ask keeps the watch going and answers the latest picture; the
        child's launcher is told to take them when the watch starts, and again
        if it has been quiet. `stop` ends it at once.
        """
        child_id = _int_arg(request, "child_id")
        if child_id not in self.supervisor.children:
            return Response.failure(request.id, "error.not_found")
        live = self.supervisor.live
        if request.args.get("stop"):
            if live.stop(child_id):
                self.bus.publish(EventKind.LIVE_VIEW, child_id=child_id, watching=False, by="")
            return Response(id=request.id, ok=True, data={"watching": False})
        watch, announce = live.ask(child_id, str(request.args.get("by", ""))[:40])
        if announce:
            self.bus.publish(EventKind.LIVE_VIEW, child_id=child_id, watching=True, by=watch.by)
        online = any(
            child["child_id"] == child_id and child.get("online")
            for child in self.supervisor.status()["children"]
        )
        age = live.age(watch)
        return Response(
            id=request.id,
            ok=True,
            data={
                "watching": True,
                "started": announce and watch.at == 0.0,
                "frame": watch.frame,
                "age": None if age is None else round(age, 1),
                "online": online,
            },
        )

    async def _assistant_setup(self, request: Request) -> Response:
        """Start the model download as its own unit, with the network the
        assistant itself never has (D-165). `--no-block`: it takes minutes, and
        the panel shows its progress from the assistant's status.
        """
        ok = await run_systemctl("start", "--no-block", AI_SETUP_UNIT)
        if ok:
            self._ai_setup_started = time.time()
        return Response(id=request.id, ok=ok, error="" if ok else "assistant.setup.failed")

    async def _remote_setup(self, request: Request) -> Response:
        """Phone access in one button (D-169): its own unit, because installing a
        package and waiting half an hour for a parent to sign in to Tailscale is
        no job for the panel's sandbox or the daemon's event loop."""
        ok = await run_systemctl("start", "--no-block", REMOTE_SETUP_UNIT)
        return Response(id=request.id, ok=ok, error="" if ok else "remote.setup.failed")

    async def _voice_setup(self, request: Request) -> Response:
        """The natural voice in one button (D-196): Piper and its Turkish voice,
        fetched by their own unit - the one part of biokids-voice with a network,
        which writes nothing but the voices directory. `--no-block`, because it
        takes a minute or two and the panel follows it on a page of its own.
        """
        ok = await run_systemctl("start", "--no-block", VOICE_SETUP_UNIT)
        return Response(id=request.id, ok=ok, error="" if ok else "voices.natural.not_started")

    async def _reload(self, request: Request) -> Response:
        self.supervisor.reload()
        return Response(
            id=request.id, ok=True, data={"children": len(self.supervisor.children)}
        )

    async def _set_policy(self, request: Request) -> Response:
        """The panel has written the policy row; this just makes it live.

        Writing policy through the socket would mean two processes with two
        different ideas of the schema. The panel owns the database, the daemon
        owns enforcement, and `reload` is the seam.
        """
        self.supervisor.reload()
        return Response(id=request.id, ok=True, data={"reloaded": True})

    async def _report_activity(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        self.supervisor.report_activity(
            child_id,
            str(request.args.get("app_slug", "")),
            str(request.args.get("app_name", "")),
            unit=str(request.args.get("unit", "")),
        )
        return Response(id=request.id, ok=True)

    async def _request_launch(self, request: Request) -> Response:
        child_id = _int_arg(request, "child_id")
        app_slug = str(request.args.get("app_slug", ""))
        decision = self.supervisor.request_launch(child_id, app_slug)
        return Response(
            id=request.id,
            ok=True,
            data={
                "allow": decision.allow,
                "action": decision.action.value,
                "reason_code": decision.reason_code,
                "remaining_seconds": decision.remaining_seconds,
            },
        )


def _int_arg(request: Request, name: str, default: int | None = None) -> int:
    raw: Any = request.args.get(name, default)
    if raw is None:
        raise IpcError(f"missing argument: {name}")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise IpcError(f"{name} must be a number") from exc


__all__ = ["DaemonServer", "decode_frame"]


AI_SETUP_UNIT = "biokids-ai-setup.service"
AI_PROGRESS_FILE = "download.json"
"""Written by biokids-ai-setup as it downloads (biokids_ai.setup.PROGRESS_FILE)."""
AI_PROGRESS_STALE_S = 15 * 60
"""A download whose progress has not moved for this long has stopped."""


def download_progress(
    models_dir: Path, *, started: float = 0.0, now: float | None = None
) -> dict[str, Any]:
    """The assistant's model download as the panel shows it (D-254).

    `state` is `starting` from the press of the button until the download unit
    writes, then `running`, `done` or `failed`; {} when none was ever started.
    Read here rather than through the assistant service, which may not be
    running while its models are still on their way.
    """
    path = models_dir / AI_PROGRESS_FILE
    now = time.time() if now is None else now
    try:
        written = path.stat().st_mtime
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "starting", "percent": 0, "file": ""} if started else {}
    if not isinstance(raw, dict):
        return {}
    if started and written < started:
        return {"state": "starting", "percent": 0, "file": ""}
    state = str(raw.get("state", ""))
    if state == "running" and now - written > AI_PROGRESS_STALE_S:
        state = "failed"
    try:
        done, total = int(raw.get("done") or 0), int(raw.get("total") or 0)
    except (TypeError, ValueError):
        done, total = 0, 0
    percent = min(100, done * 100 // total) if total > 0 else 0
    return {"state": state, "percent": percent, "file": str(raw.get("file", ""))}


REMOTE_SETUP_UNIT = "biokids-remote-setup.service"
VOICE_SETUP_UNIT = natural_voice.SETUP_UNIT


async def run_systemctl(*args: str) -> bool:
    """`systemctl <args>`, True when it exits 0. Never raises."""
    try:
        process = await asyncio.create_subprocess_exec(
            "systemctl",
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await asyncio.wait_for(process.communicate(), timeout=30)
    except (OSError, TimeoutError) as exc:
        log.warning("systemctl %s could not be run: %s", " ".join(args), exc)
        return False
    if process.returncode != 0:
        log.warning(
            "systemctl %s failed: %s", " ".join(args), err.decode("utf-8", "replace").strip()
        )
        return False
    return True
