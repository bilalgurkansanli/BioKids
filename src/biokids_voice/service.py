"""The voice service: one socket, one queue, one line at a time.

Listens on `/run/user/<uid>/biokids-voice.sock` and speaks the protocol of
VOICE.md §8. The daemon asks for an event, the panel asks for a preview, and
the shell subscribes so it knows when to take the character card away again.

    ->  {"op": "speak", "event": "warn_10", "persona": "auto",
         "vars": {"name": "Elif", "minutes": 10}, "locale": "tr",
         "priority": "normal", "expression": "neutral"}
    <-  {"id": "1", "ok": true, "data": {"status": "queued", "id": "u3",
         "text": "On dakikan kaldı.", "persona": "zio", ...}}
    <-  {"event": "speak_finished", "payload": {"id": "u3", ...}}     (subscribers)

The reply carries the *resolved sentence*, not just an acknowledgement, because
the card has to show the words as well as say them and VOICE.md §7 is explicit
that this is not optional: a deaf child, a muted machine and a noisy room all
get the same information. It is also why almost nothing here returns a failure.
No Piper, no sound card, quiet hours, a queue that is already full of more
urgent things — every one of those still leaves the shell with a sentence to
put on screen, and the difference shows up in `status` rather than in `ok`.

Three rules from VOICE.md §6 are implemented here rather than anywhere else:

* **Never interrupt.** The worker takes the next line only when the previous one
  has finished. Priority decides what is *next*, never what is *now*, and it is
  read from the event rather than trusted to the caller: `time_up` and `blocked`
  are high whoever asks for them.
* **Drop, do not talk over.** A full queue loses its oldest normal-priority
  line. The dropped line gets a finished notification anyway, or its card would
  sit on the child's screen forever.
* **Quiet hours are visual, not silent.** The card still appears and still holds
  for as long as the line would have taken to say; only the audio is skipped.

Frames are the daemon's line-delimited JSON (`biokids_core.ipc`), with one
concession: VOICE.md §8 writes its arguments at the top level and the daemon
nests them under `args`. Both are accepted, which costs three lines and means
the shell can carry one client for two sockets.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from enum import StrEnum
from itertools import count
from pathlib import Path
from typing import Any, Final, cast

from biokids_core.clock import Clock, SystemClock
from biokids_core.config import Config, get_config
from biokids_core.errors import IpcError
from biokids_core.i18n import normalise_locale
from biokids_core.ipc import Event, Request, Response, decode_frame
from biokids_core.types import TimeWindow
from biokids_voice.lines import LineBook
from biokids_voice.player import PipeWirePlayer, Player, SilentPlayer, describe
from biokids_voice.synth import Synthesizer, VoiceBank, VoiceSpec, estimate_duration

LOG = logging.getLogger(__name__)

PERSONAS: Final = ("zio", "sio")
EXPRESSIONS: Final = ("neutral", "happy", "concerned", "sleepy")

PRIORITY_HIGH: Final = "high"
PRIORITY_NORMAL: Final = "normal"

HIGH_PRIORITY_EVENTS: Final = frozenset({"time_up", "blocked"})
"""VOICE.md §6 makes urgency a property of the event, not of the caller.

A daemon that forgets the field must not be able to leave the last line of the
day queued behind three cheerful ones, or have it dropped when the queue fills.
"""

EXPRESSION_BY_EVENT: Final[dict[str, str]] = {
    "earned": "happy",
    "blocked": "concerned",
    "bedtime": "sleepy",
}
"""The face VOICE.md §7 asks for when the caller does not name one. Warnings —
and everything not listed — stay `neutral`."""

STATUS_QUEUED: Final = "queued"
STATUS_DROPPED: Final = "dropped"
STATUS_SPOKEN: Final = "spoken"
STATUS_SILENT: Final = "silent"
STATUS_BANNER: Final = "banner"
"""No audio, card only — the last link of the fallback chain, and still a success."""

EVENT_STARTED: Final = "speak_started"
EVENT_FINISHED: Final = "speak_finished"


class VoiceOp(StrEnum):
    """The whole vocabulary of this socket. VOICE.md §8 defines the first two."""

    SPEAK = "speak"
    PREVIEW = "preview"
    SUBSCRIBE = "subscribe"
    """Hold the connection open and receive every started/finished frame."""
    PING = "ping"
    STATUS = "status"
    """Why the machine is quiet, in a form the panel can show a parent."""


PREVIEW_EVENT: Final = "preview"
SUBSCRIBER_BACKLOG: Final = 64
MAX_TEXT_CHARS: Final = 400
"""A preview is a sentence, not an essay. Anything longer is a mistake or an abuse."""


# ---------------------------------------------------------------- utterance


@dataclass(frozen=True, slots=True)
class Utterance:
    """One line on its way to the speakers."""

    id: str
    event: str
    text: str
    persona: str
    expression: str
    priority: str
    locale: str
    hold_s: float
    sequence: int

    @property
    def is_high(self) -> bool:
        return self.priority == PRIORITY_HIGH

    def card(self) -> dict[str, Any]:
        """What the shell needs to draw and label the character card."""
        return {
            "id": self.id,
            "event": self.event,
            "text": self.text,
            "persona": self.persona,
            "expression": self.expression,
            "locale": self.locale,
            "priority": self.priority,
            "duration_ms": int(self.hold_s * 1000),
        }


def _order(item: Utterance) -> tuple[int, int]:
    """High before normal, and arrival order within a priority."""
    return (0 if item.is_high else 1, item.sequence)


# ------------------------------------------------------------------ service


class VoiceService:
    """Everything above the socket: the queue, the fallbacks, the notifications."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        lines: LineBook | None = None,
        voices: VoiceBank | None = None,
        synthesizer: Synthesizer | None = None,
        player: Player | None = None,
        clock: Clock | None = None,
        hold: Callable[[str], float] = estimate_duration,
        rng: random.Random | None = None,
    ) -> None:
        self._config = config or get_config()
        self._clock = clock or SystemClock()
        self._hold = hold
        # Picking Zio or Sio at random is a coin toss, not a secret (see lines.py).
        self._rng = rng or random.Random()  # noqa: S311

        self._lines = lines or LineBook(self._config)
        self._voices = voices or VoiceBank(self._config)

        # `voice.enabled = false` is a parent turning the companions off, not a
        # degraded machine: no synthesiser and no player are built at all, and
        # every line comes out as a silent card.
        self._audio = self._config.voice.enabled
        self._synth = (
            (synthesizer or Synthesizer.from_config(self._config)) if self._audio else None
        )
        self._player = (
            (player or PipeWirePlayer.from_config(self._config)) if self._audio else None
        )

        self._quiet = _parse_quiet_hours(self._config.voice.quiet_hours)
        self._pending: list[Utterance] = []
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._ids = count(1)
        self._sequence = count(1)
        self._persona_turn = 0
        self.spoken = 0
        """How many lines have actually been heard. The `status` op reports it."""

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="biokids-voice-worker")

    async def aclose(self) -> None:
        """Stop speaking, and leave no card stranded on the child's screen."""
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._drain(STATUS_BANNER)
        if self._player is not None:
            await self._player.aclose()

    # ------------------------------------------------------------ requests

    async def handle(self, request: Request) -> Response:
        """One request, one response. `subscribe` is handled by the connection."""
        match request.op:
            case VoiceOp.SPEAK:
                return self._speak_request(request)
            case VoiceOp.PREVIEW:
                return self._preview_request(request)
            case VoiceOp.PING:
                return Response(id=request.id, ok=True, data={"service": "biokids-voice"})
            case VoiceOp.STATUS:
                return Response(id=request.id, ok=True, data=self.status())
            case _:
                return Response.failure(request.id, f"unknown op: {request.op}")

    def status(self) -> dict[str, Any]:
        """Enough for the panel to explain a silent machine to a parent."""
        return {
            "enabled": self._audio,
            "quiet_hours": str(self._quiet) if self._quiet else "",
            "quiet_now": self._quiet_now(),
            "queued": len(self._pending),
            "queue_depth": self._depth(),
            "spoken": self.spoken,
            "backend": self._synth.backend.name if self._synth else "",
            "cache_hits": self._synth.cache.hits if self._synth else 0,
            "player": describe(self._player) if self._player else {"kind": "disabled"},
        }

    def _speak_request(self, request: Request) -> Response:
        args = request.args
        # Lowercased here and not only in `lines.py`: the event decides the
        # priority and the expression as well as the filename, and `TIME_UP`
        # must not be urgent in one of those three places and not the others.
        event = str(args.get("event", "")).strip().lower()
        if not event:
            return Response.failure(request.id, "speak needs an event")

        locale = normalise_locale(_as_str(args.get("locale")))
        variables = args.get("vars")
        # No session: this process cannot open the database (see lines.py). A
        # family's own wording reaches it through the installed text files.
        line = self._lines.render(
            event, locale, variables if isinstance(variables, dict) else {}
        )
        if line is None:
            # Not a failure the child sees: the daemon asked for an event this
            # machine ships no text for. Logged so a packager can fix it.
            LOG.warning("no %s line for event %r; nothing to say", locale, event)
            return Response.failure(request.id, f"no line for event: {event}")

        return self._enqueue(
            request.id,
            event=event,
            text=line.text,
            locale=locale,
            persona=self._pick_persona(_as_str(args.get("persona"))),
            expression=_expression(_as_str(args.get("expression")), event),
            priority=_priority(_as_str(args.get("priority")), event),
        )

    def _preview_request(self, request: Request) -> Response:
        """The panel's voice preview and line editor (VOICE.md §8)."""
        text = _as_str(request.args.get("text")).strip()[:MAX_TEXT_CHARS]
        if not text:
            return Response.failure(request.id, "preview needs text")
        return self._enqueue(
            request.id,
            event=PREVIEW_EVENT,
            text=text,
            locale=normalise_locale(_as_str(request.args.get("locale"))),
            persona=self._pick_persona(_as_str(request.args.get("persona"))),
            expression=_expression(_as_str(request.args.get("expression")), PREVIEW_EVENT),
            # A parent pressed a button and is waiting: it goes to the front.
            priority=PRIORITY_HIGH,
        )

    def _enqueue(self, request_id: str, **fields: Any) -> Response:
        utterance = Utterance(
            id=f"u{next(self._ids)}",
            hold_s=self._hold(fields["text"]),
            sequence=next(self._sequence),
            **fields,
        )
        status = self._admit(utterance)
        data = utterance.card()
        data["status"] = status
        data["silent"] = self._quiet_now() or not self._audio
        return Response(id=request_id, ok=True, data=data)

    def _admit(self, utterance: Utterance) -> str:
        """Make room if there is any to make, and say what happened."""
        if len(self._pending) >= self._depth():
            victim = next((item for item in self._pending if not item.is_high), None)
            if victim is None:
                LOG.warning("voice queue full of urgent lines; dropping %s", utterance.event)
                return STATUS_DROPPED
            self._pending.remove(victim)
            LOG.info(
                "voice queue full; dropped %s to make room for %s",
                victim.event,
                utterance.event,
            )
            # Its card is already on screen, put there by the reply to the
            # request that queued it. Somebody has to take it down again.
            self._finish(victim, STATUS_DROPPED)

        self._pending.append(utterance)
        self._wake.set()
        return STATUS_QUEUED

    def _depth(self) -> int:
        return max(1, self._config.voice.queue_depth)

    # ------------------------------------------------------------- speaking

    async def _run(self) -> None:
        while True:
            utterance = await self._next()
            try:
                await self._say(utterance)
            except asyncio.CancelledError:
                self._finish(utterance, STATUS_BANNER)
                raise
            except Exception:
                # A line that fails must never take the service down with it;
                # the next warning is more important than this one.
                LOG.exception("speaking %s failed", utterance.event)
                self._finish(utterance, STATUS_BANNER)

    async def _next(self) -> Utterance:
        while not self._pending:
            # No await between the emptiness check and the clear, so an enqueue
            # cannot slip in and have its wake-up thrown away.
            self._wake.clear()
            await self._wake.wait()
        self._pending.sort(key=_order)
        return self._pending.pop(0)

    async def _say(self, utterance: Utterance) -> None:
        self._emit(EVENT_STARTED, utterance, STATUS_QUEUED)
        outcome = await self._render_and_play(utterance)
        if outcome is STATUS_SPOKEN:
            self.spoken += 1
        self._finish(utterance, outcome)

    async def _render_and_play(self, utterance: Utterance) -> str:
        if self._quiet_now():
            # The card is the whole point during quiet hours, so it has to stay
            # up as long as it would have if the line had been spoken.
            await self._hold_card(utterance)
            return STATUS_SILENT

        if self._synth is None or self._player is None or not self._player.available():
            # Nothing can be played, so nothing is synthesised: running Piper to
            # produce a WAV that no command exists to play would cost a child's
            # laptop a second of CPU for silence.
            await self._hold_card(utterance)
            return STATUS_BANNER

        voices: tuple[VoiceSpec, ...] = self._voices.candidates(
            utterance.persona, utterance.locale
        )
        result = await asyncio.to_thread(self._synth.render, utterance.text, voices)
        if result.path is not None and await self._player.play(result.path):
            return STATUS_SPOKEN

        LOG.warning(
            "%s could not be spoken; showing the card alone for %s",
            utterance.event,
            utterance.persona,
        )
        await self._hold_card(utterance)
        return STATUS_BANNER

    async def _hold_card(self, utterance: Utterance) -> None:
        if utterance.hold_s > 0:
            await asyncio.sleep(utterance.hold_s)

    def _quiet_now(self) -> bool:
        if self._quiet is None:
            return False
        return self._quiet.contains(self._clock.read().wall.time())

    def _pick_persona(self, requested: str) -> str:
        """`auto` alternates, so a child hears both of them (VOICE.md §5)."""
        choice = requested.strip().lower()
        if choice in PERSONAS:
            return choice
        if choice == "random":
            return self._rng.choice(PERSONAS)
        persona = PERSONAS[self._persona_turn % len(PERSONAS)]
        self._persona_turn += 1
        return persona

    # -------------------------------------------------------- notifications

    async def events(self) -> AsyncGenerator[Event]:
        """Every started/finished frame, until the caller stops listening.

        A generator rather than an iterator in the signature because the caller
        closes it: `_stream` has to unsubscribe when the shell goes away, and
        nothing else would run the `finally` below.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=SUBSCRIBER_BACKLOG)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)

    def _finish(self, utterance: Utterance, status: str) -> None:
        self._emit(EVENT_FINISHED, utterance, status)

    def _emit(self, kind: str, utterance: Utterance, status: str) -> None:
        payload = utterance.card()
        payload["status"] = status
        frame = Event(
            kind=kind,
            payload=payload,
            ts=self._clock.read().wall.isoformat(),
        )
        for queue in self._subscribers:
            if queue.full():
                # A subscriber that is not reading is a stuck shell. The newest
                # frame is the one that dismisses a card, so it is the one to keep.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)

    def _drain(self, status: str) -> None:
        pending, self._pending = self._pending, []
        for utterance in pending:
            self._finish(utterance, status)

    # -------------------------------------------------------------- socket

    async def serve(self, socket_path: Path | None = None) -> None:
        """Listen until cancelled. The socket is the child's alone, mode 0600."""
        path = socket_path or default_socket_path(self._config)
        _clear_stale_socket(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # typeshed only declares the unix helpers on POSIX, and this project is
        # developed on Windows. Production is Linux (D-12).
        start_unix_server = getattr(asyncio, "start_unix_server", None)
        if start_unix_server is None:  # pragma: no cover - development host only
            raise IpcError("unix sockets are not available on this platform")

        # The worker starts before the socket does, so the very first request of
        # the session cannot sit in the queue waiting for a task that is not there.
        await self.start()
        server = await cast("Any", start_unix_server)(self._connection, str(path))
        _restrict(path)
        LOG.info("biokids-voice listening on %s", path)
        try:
            async with server:
                await server.serve_forever()
        finally:
            with contextlib.suppress(OSError):
                path.unlink()

    async def _connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                try:
                    raw = await reader.readline()
                except ValueError:
                    # A line longer than the stream's buffer. The reader has
                    # already dropped it, so the next newline resynchronises us;
                    # letting it out of here would kill the connection without a
                    # word and leave the caller waiting for a reply.
                    LOG.warning("dropping an oversized frame")
                    writer.write(Response.failure("", "frame too long").encode())
                    await writer.drain()
                    continue
                if not raw:
                    return
                if not raw.strip():
                    continue
                request = _parse(raw)
                if request is None:
                    writer.write(Response.failure("", "malformed frame").encode())
                    await writer.drain()
                    continue
                if request.op == VoiceOp.SUBSCRIBE:
                    await self._stream(request, writer)
                    return
                writer.write((await self.handle(request)).encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            return  # the shell went away; nothing to clean up but the socket
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    async def _stream(self, request: Request, writer: asyncio.StreamWriter) -> None:
        """Acknowledge once, then push frames until the subscriber disconnects."""
        writer.write(Response(id=request.id, ok=True, data={"subscribed": True}).encode())
        await writer.drain()
        frames = self.events()
        try:
            async for frame in frames:
                writer.write(frame.encode())
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            return
        finally:
            await frames.aclose()


# ------------------------------------------------------------------ helpers


def default_socket_path(config: Config | None = None) -> Path:
    """`$XDG_RUNTIME_DIR/biokids-voice.sock`, which is `/run/user/<uid>` in a session.

    The environment variable comes first because that is what systemd sets for
    a user unit, and because it is the seam that lets a developer run two
    sessions side by side.
    """
    config = config or get_config()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime) / config.voice.socket_name
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return Path(f"/run/user/{getuid()}") / config.voice.socket_name
    return config.paths.run_dir / config.voice.socket_name  # pragma: no cover


def _clear_stale_socket(path: Path) -> None:
    """A socket file left behind by a killed service is not a running service.

    Only a socket is removed. Anything else at that path is somebody else's
    file, and deleting a parent's data because a config line pointed the wrong
    way is not a mistake this service gets to make.
    """
    if not path.exists():
        return
    if not path.is_socket():
        raise IpcError(f"{path} exists and is not a socket; refusing to remove it")
    LOG.info("removing a stale socket at %s", path)
    with contextlib.suppress(OSError):
        path.unlink()


def _restrict(path: Path) -> None:
    if os.name == "posix":
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def _parse(raw: bytes) -> Request | None:
    """Accept both frame shapes: VOICE.md's flat one and the daemon's nested one."""
    try:
        payload = decode_frame(raw)
    except IpcError as exc:
        LOG.warning("dropping a malformed frame: %s", exc)
        return None
    op = payload.get("op")
    if not isinstance(op, str) or not op:
        return None
    nested = payload.get("args")
    args = (
        dict(nested)
        if isinstance(nested, dict)
        else {k: v for k, v in payload.items() if k not in {"op", "id"}}
    )
    return Request(op=op, id=str(payload.get("id", "")), args=args)


def _parse_quiet_hours(raw: str) -> TimeWindow | None:
    """Empty means never quiet; malformed means never quiet, loudly.

    Refusing to speak because a config line has a typo would silence the
    warnings a child needs, which is the wrong way round to fail.
    """
    text = raw.strip()
    if not text:
        return None
    try:
        return TimeWindow.parse(text)
    except ValueError as exc:
        LOG.warning("voice.quiet_hours %r is not a time window (%s); ignoring", raw, exc)
        return None


def _priority(raw: str, event: str) -> str:
    """High when the caller asks, and high anyway for the events §6 names."""
    if raw.strip().lower() == PRIORITY_HIGH or event in HIGH_PRIORITY_EVENTS:
        return PRIORITY_HIGH
    return PRIORITY_NORMAL


def _expression(raw: str, event: str) -> str:
    """The face the caller asked for, or the one the event implies."""
    choice = raw.strip().lower()
    if choice in EXPRESSIONS:
        return choice
    return EXPRESSION_BY_EVENT.get(event, EXPRESSIONS[0])


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


# --------------------------------------------------------------- one-shot run


async def run_service(
    config: Config | None = None,
    *,
    socket_path: Path | None = None,
    audio: bool = True,
    stop: asyncio.Event | None = None,
) -> None:
    """Serve until `stop` is set or the server falls over."""
    config = config or get_config()
    service = VoiceService(config, player=None if audio else SilentPlayer())
    stop = stop or asyncio.Event()

    serving: asyncio.Task[None] = asyncio.create_task(
        service.serve(socket_path), name="biokids-voice-server"
    )
    waiting: asyncio.Task[bool] = asyncio.create_task(stop.wait(), name="biokids-voice-stop")
    try:
        await asyncio.wait({serving, waiting}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (serving, waiting):
            task.cancel()
        await _settle(serving)
        await _settle(waiting)
        await service.aclose()
    if serving.done() and not serving.cancelled():
        # Re-raise a server failure rather than exiting zero: systemd should
        # restart us, and a voice service that quietly stopped listening is
        # exactly the silent failure principle 3 forbids.
        serving.result()


async def _settle(task: asyncio.Task[Any]) -> None:
    """Let a cancelled task finish. Its outcome is examined by the caller."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
