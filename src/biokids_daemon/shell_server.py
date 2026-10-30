"""The unprivileged socket the child's own session talks to.

`ARCHITECTURE.md` §2 says the launcher "asks the daemon for permission", and
`SECURITY.md` §2 says the daemon's socket is `root:biokids` 0660. Both are right
and together they leave a gap: the child is not in the `biokids` group and must
never be, because that group can grant time and power the machine off.

So there are two sockets.

`/run/biokids/daemon.sock` is the privileged one, unchanged: root, group
`biokids`, 0660, every operation.

`/run/biokids/shell.sock` is this one. It is world-connectable, which sounds
alarming and is not, because **nothing on it takes a child id as an argument**.
The daemon asks the kernel who is on the other end — `SO_PEERCRED`, which a
process cannot forge about itself — maps that uid to a child, and answers about
that child only. A uid that is not a managed child gets nothing at all.

What the child's session can do is therefore bounded by this file: read its own
remaining time and tiles, ask to start an application the parent enabled, say
what has focus, ask for the machine to be shut down, and file an "ask a parent"
request. There is no operation here that adds a minute to anyone's day.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import struct
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from biokids_core.config import Config, get_config
from biokids_core.errors import IpcError
from biokids_core.ipc import MAX_LINE_BYTES, Event, Request, Response, ShellOp
from biokids_core.types import ShutdownOutcome
from biokids_daemon.assistant import (
    MAX_HISTORY,
    MAX_TURN_CHARS,
    MAX_WORDS,
    PRACTICE,
    AssistantGate,
)
from biokids_daemon.bus import EventBus
from biokids_daemon.server import cancel_connections
from biokids_daemon.supervisor import ChildRuntime, Supervisor

if TYPE_CHECKING:  # pragma: no cover - the daemon only ever runs on Linux
    from asyncio import AbstractServer

log = logging.getLogger(__name__)

Handler = Callable[["ShellCaller", Request], Awaitable[Response]]

SHELL_SOCKET_MODE = 0o666
"""World-connectable on purpose; see the module docstring. The authorisation is
the peer's uid, not the file mode."""


class ShellCaller:
    """Who is on the other end of a connection, decided by the kernel."""

    def __init__(self, uid: int, runtime: ChildRuntime) -> None:
        self.uid = uid
        self.runtime = runtime

    @property
    def child_id(self) -> int:
        return self.runtime.child_id


def peer_uid(writer: asyncio.StreamWriter) -> int | None:
    """The uid of the connecting process, or None if it cannot be established.

    `SO_PEERCRED` is filled in by the kernel at connect time from the peer's
    real credentials. A process cannot lie about it, which is what makes an
    open-mode socket safe here.
    """
    raw = writer.get_extra_info("socket")
    if raw is None:  # pragma: no cover - only on a transport we do not use
        return None
    try:
        creds = raw.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,  # type: ignore[attr-defined]
            struct.calcsize("3i"),
        )
    except (OSError, AttributeError):  # pragma: no cover - non-Linux
        return None
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


class ShellServer:
    """Serves `ShellOp` to the child's session, and nothing else."""

    def __init__(
        self,
        supervisor: Supervisor,
        bus: EventBus,
        config: Config | None = None,
        assistant: AssistantGate | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.bus = bus
        self.config = config or get_config()
        self.assistant = assistant or AssistantGate(supervisor, bus, self.config)
        self._server: AbstractServer | None = None
        self._connections: set[asyncio.Task[None]] = set()
        """Every live connection handler, so stop() can end them.

        Its absence is the whole of the hang: `_stream` parks on the subscriber
        queue forever, `wait_closed()` waits for it, and nothing cancelled it."""

    @property
    def socket_path(self) -> Path:
        return self.config.paths.shell_socket

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        path = self.socket_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

        start_unix_server = cast(
            "Callable[..., Awaitable[AbstractServer]]",
            asyncio.start_unix_server,  # type: ignore[attr-defined]
        )
        # A press-and-speak recording arrives as one frame of base64 (D-166),
        # far over the stream's default 64 KiB line.
        self._server = await start_unix_server(
            self._serve, path=str(path), limit=MAX_LINE_BYTES + 4096
        )
        with contextlib.suppress(OSError):
            path.chmod(SHELL_SOCKET_MODE)
        log.info("child launcher socket at %s", path)

    async def stop(self) -> None:
        """The same shape as DaemonServer.stop, and for the same reason.

        This one is the worse of the two, because the subscriber that hangs it
        is the child's own launcher: it holds a subscription open for as long as
        the kiosk is on screen, so pressing the shutdown tile used to leave a
        four-year-old staring at a frozen screen for ninety seconds.
        """
        if self._server is not None:
            self._server.close()
        await cancel_connections(self._connections)
        await self.assistant.aclose()
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
        uid = peer_uid(writer)
        caller = self._identify(uid)
        try:
            if caller is None:
                # Not a managed child. Say so once and close; do not leak whether
                # the uid exists, which child ids there are, or anything else.
                writer.write(Response(ok=False, error="shell.not_a_child").encode())
                await writer.drain()
                return
            while True:
                line = await reader.readline()
                if not line:
                    return
                if not line.strip():
                    continue
                response, subscribe = await self._dispatch(caller, line)
                writer.write(response.encode())
                await writer.drain()
                if subscribe:
                    await self._stream(writer, caller.child_id)
                    return
        except (ConnectionResetError, BrokenPipeError):
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # a broken launcher must not stop enforcement
            log.exception("shell connection failed")
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def _identify(self, uid: int | None) -> ShellCaller | None:
        if uid is None:
            return None
        if uid == 0:
            # root connecting to the unprivileged socket is the test suite or an
            # administrator poking at it. There is nothing here root cannot get
            # from the privileged socket, so refuse rather than guess a child.
            return None
        name = username_for_uid(uid)
        if name is None:
            return None
        child_id = self.supervisor.by_user.get(name)
        if child_id is None:
            return None
        runtime = self.supervisor.children.get(child_id)
        return ShellCaller(uid, runtime) if runtime else None

    async def _dispatch(self, caller: ShellCaller, line: bytes) -> tuple[Response, bool]:
        try:
            request = Request.decode(line)
        except IpcError:
            return Response.failure("", "protocol.malformed"), False

        if request.op == ShellOp.SUBSCRIBE_EVENTS:
            return Response(id=request.id, ok=True, data={"subscribed": True}), True

        handler = self._handlers().get(request.op)
        if handler is None:
            return Response.failure(request.id, "shell.not_allowed"), False
        try:
            return await handler(caller, request), False
        except Exception:
            log.exception("shell op %s failed", request.op)
            return Response.failure(request.id, "error.unknown"), False

    async def _stream(self, writer: asyncio.StreamWriter, child_id: int) -> None:
        """Only this child's events. A launcher never hears about a sibling."""
        subscriber = self.bus.subscribe(child_id=child_id, label=f"shell:{child_id}")
        try:
            while True:
                event: Event = await subscriber.queue.get()
                subscriber.queue.task_done()
                if event.child_id not in (None, child_id):
                    continue
                writer.write(event.encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        finally:
            self.bus.unsubscribe(subscriber)

    # -------------------------------------------------------------- handlers

    def _handlers(self) -> dict[str, Handler]:
        return {
            ShellOp.PING: self._ping,
            ShellOp.GET_STATE: self._get_state,
            ShellOp.LAUNCH: self._launch,
            ShellOp.REPORT_ACTIVITY: self._report_activity,
            ShellOp.REPORT_VISIT: self._report_visit,
            ShellOp.REPORT_SEARCH: self._report_search,
            ShellOp.ENGLISH_STATE: self._english_state,
            ShellOp.ENGLISH_ANSWER: self._english_answer,
            ShellOp.READING_STATE: self._reading_state,
            ShellOp.READING_ANSWER: self._reading_answer,
            ShellOp.FIRST_RUN_DONE: self._first_run_done,
            ShellOp.FINISH_UP: self._finish_up,
            ShellOp.NOTE_SEEN: self._note_seen,
            ShellOp.SHARE_RECORDING: self._share_recording,
            ShellOp.REQUEST_SHUTDOWN: self._request_shutdown,
            ShellOp.SWITCH_USER: self._switch_user,
            ShellOp.ASK_A_PARENT: self._ask_a_parent,
            ShellOp.ASK_ASSISTANT: self._ask_assistant,
            ShellOp.ASSISTANT_MORE: self._assistant_more,
            ShellOp.HEAR_ASSISTANT: self._hear_assistant,
            ShellOp.WARM_ASSISTANT: self._warm_assistant,
            ShellOp.LIVE_FRAME: self._live_frame,
        }

    async def _ping(self, _caller: ShellCaller, request: Request) -> Response:
        return Response(id=request.id, ok=True)

    async def _get_state(self, caller: ShellCaller, request: Request) -> Response:
        return Response(
            id=request.id, ok=True, data=self.supervisor.shell_state(caller.child_id)
        )

    async def _launch(self, caller: ShellCaller, request: Request) -> Response:
        slug = str(request.args.get("app_slug", ""))
        decision = self.supervisor.request_launch(caller.child_id, slug)
        return Response(
            id=request.id,
            ok=True,
            data={
                "allow": decision.allow,
                "reason_code": decision.reason_code,
                "remaining_seconds": decision.remaining_seconds,
            },
        )

    async def _report_visit(self, caller: ShellCaller, request: Request) -> Response:
        host = str(request.args.get("host", "")).strip().lower()[:253]
        if host:
            self.supervisor.record_visit(caller.child_id, host)
        return Response(id=request.id, ok=True)

    async def _report_search(self, caller: ShellCaller, request: Request) -> Response:
        words = str(request.args.get("words", ""))
        host = str(request.args.get("host", "")).strip().lower()[:253]
        self.supervisor.record_refused_search(caller.child_id, words, host)
        return Response(id=request.id, ok=True)

    async def _english_state(self, caller: ShellCaller, request: Request) -> Response:
        return Response(
            id=request.id, ok=True, data=self.supervisor.english_state(caller.child_id)
        )

    async def _reading_state(self, caller: ShellCaller, request: Request) -> Response:
        return Response(
            id=request.id, ok=True, data=self.supervisor.reading_state(caller.child_id)
        )

    async def _reading_answer(self, caller: ShellCaller, request: Request) -> Response:
        item = str(request.args.get("item", ""))[:64]
        right = request.args.get("right") is True
        box = self.supervisor.reading_answer(caller.child_id, item, right)
        return Response(
            id=request.id,
            ok=box is not None,
            data={"box": box or 0},
            error="" if box is not None else "reading.unknown_item",
        )

    async def _english_answer(self, caller: ShellCaller, request: Request) -> Response:
        item = str(request.args.get("item", ""))[:64]
        right = request.args.get("right") is True
        box = self.supervisor.english_answer(caller.child_id, item, right)
        return Response(
            id=request.id,
            ok=box is not None,
            data={"box": box or 0},
            error="" if box is not None else "english.unknown_item",
        )

    async def _report_activity(self, caller: ShellCaller, request: Request) -> Response:
        # A `category` from a launcher older than D-228 is not read: the
        # daemon looks the kind of app up itself.
        self.supervisor.report_activity(
            caller.child_id,
            str(request.args.get("app_slug", "")),
            str(request.args.get("app_name", "")),
            unit=str(request.args.get("unit", "")),
        )
        return Response(id=request.id, ok=True)

    async def _live_frame(self, caller: ShellCaller, request: Request) -> Response:
        """A picture of the caller's own screen, while a parent watches it (D-253)."""
        frame = str(request.args.get("frame", ""))
        watching = self.supervisor.live.push(caller.child_id, frame)
        return Response(id=request.id, ok=True, data={"watching": watching})

    async def _share_recording(self, caller: ShellCaller, request: Request) -> Response:
        raw = request.args.get("seconds", 0)
        seconds = (
            float(raw) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0.0
        )
        shared = self.supervisor.share_recording(
            caller.child_id, str(request.args.get("audio", "")), seconds
        )
        return Response(id=request.id, ok=shared)

    async def _note_seen(self, caller: ShellCaller, request: Request) -> Response:
        raw = request.args.get("note_id")
        if not isinstance(raw, int) or isinstance(raw, bool):
            return Response.failure(request.id, "error.invalid_input")
        seen = self.supervisor.note_seen(caller.child_id, raw)
        return Response(id=request.id, ok=seen)

    async def _finish_up(self, caller: ShellCaller, request: Request) -> Response:
        seconds = self.supervisor.finish_up(caller.child_id)
        return Response(id=request.id, ok=True, data={"seconds": seconds})

    async def _first_run_done(self, caller: ShellCaller, request: Request) -> Response:
        self.supervisor.mark_first_run_done(caller.child_id)
        return Response(id=request.id, ok=True)

    async def _request_shutdown(self, caller: ShellCaller, request: Request) -> Response:
        """The child may shut the machine down from a clearly marked tile (D-100).

        It is a request rather than a command, answered before it is carried out
        and with what will happen (D-209): the computer switches off, or - while
        anyone else is logged in - only this child's session ends, because one
        person's "Kapat" must not close another's work. Who the others are is not
        said: the child's screen has no use for another account's name.
        """
        outcome = await self.supervisor.child_requested_shutdown(caller.child_id)
        return Response(
            id=request.id,
            ok=True,
            data={"poweroff": outcome is ShutdownOutcome.POWEROFF, "outcome": outcome.value},
        )

    async def _switch_user(self, caller: ShellCaller, request: Request) -> Response:
        ok = await self.supervisor.child_switch_user(caller.child_id)
        return Response(id=request.id, ok=ok)

    async def _ask_a_parent(self, caller: ShellCaller, request: Request) -> Response:
        url = str(request.args.get("url", ""))[:1024]
        kind = str(request.args.get("kind", "site"))
        created = self.supervisor.file_access_request(caller.child_id, url, kind)
        # 0 is "asked too often this hour": the launcher says so kindly.
        return Response(
            id=request.id,
            ok=created > 0,
            data={"request_id": created},
            error="" if created else "shell.request.too_often",
        )

    async def _ask_assistant(self, caller: ShellCaller, request: Request) -> Response:
        question = str(request.args.get("question", ""))[:1000]
        if request.args.get("mode") != PRACTICE:
            reply = await self.assistant.ask(caller.child_id, question)
            return Response(id=request.id, ok=True, data=reply)
        # English with Sio (D-226): the conversation so far and the words the
        # child knows come from the child's own page, so only their shape and
        # length are taken; the assistant service screens the words in them.
        raw = request.args.get("history")
        history = [
            [str(entry[0]), str(entry[1])[:MAX_TURN_CHARS]]
            for entry in (raw if isinstance(raw, list) else [])[-MAX_HISTORY:]
            if isinstance(entry, list) and len(entry) == 2 and entry[0] in {"child", "sio"}
        ]
        listed = request.args.get("words")
        words = [str(word)[:30] for word in (listed if isinstance(listed, list) else [])][
            :MAX_WORDS
        ]
        reply = await self.assistant.ask(
            caller.child_id, question, mode=PRACTICE, history=history, words=words
        )
        return Response(id=request.id, ok=True, data=reply)

    async def _assistant_more(self, caller: ShellCaller, request: Request) -> Response:
        """The rest of an answer (D-216). The id only names it; whose it is, the
        kernel said, and the gate answers about this child's answer alone."""
        answer_id = str(request.args.get("answer_id", ""))[:64]
        reply = await self.assistant.more(caller.child_id, answer_id)
        return Response(id=request.id, ok=True, data=reply)

    async def _warm_assistant(self, caller: ShellCaller, request: Request) -> Response:
        warm = await self.assistant.warmup(caller.child_id)
        return Response(id=request.id, ok=True, data=warm)

    async def _hear_assistant(self, caller: ShellCaller, request: Request) -> Response:
        audio = str(request.args.get("audio", ""))
        # English is the only other language a child is ever heard in (D-225);
        # "auto", Turkish or English, whichever they spoke (D-244).
        asked = request.args.get("language")
        language = str(asked) if asked in ("en", "auto") else None
        heard = await self.assistant.hear(caller.child_id, audio, language)
        return Response(
            id=request.id,
            ok=bool(heard.get("ok")),
            data=heard,
            error=str(heard.get("error", "")),
        )


def username_for_uid(uid: int) -> str | None:
    """Ask the password database who this uid is.

    `pwd` is POSIX-only and typeshed says so, so it is reached through a cast;
    `mypy --strict` runs on the Windows host this project is developed from and
    production is Linux (D-12).
    """
    try:
        import pwd as pwd_module
    except ImportError:  # pragma: no cover - the Windows development host
        return None
    getpwuid = cast("Callable[[int], Any]", pwd_module.getpwuid)  # type: ignore[attr-defined]
    try:
        return str(getpwuid(uid).pw_name)
    except KeyError:
        return None
