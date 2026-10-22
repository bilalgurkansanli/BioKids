"""`biokids-ai`: the assistant's service, on a socket only the daemon talks to (D-165).

It runs as the `biokids-ai` user with no network at all (`PrivateNetwork=yes`)
and cannot read the family's database. It does not decide whether a child may
use the assistant - the daemon does, from the child's policy, before a question
ever gets here - and it never writes an alert: it says what it refused and why,
and the daemon, which owns the database, tells the parent.

Operations, line-delimited JSON as everywhere else in BioKids:

    status                         which models are installed and selected
    ask      question, age, locale, persona  -> the start of an answer
    more     answer_id                       -> its next sentences, and how it ended
    cancel   answer_id                       -> stop writing it
    transcribe   audio (base64 WAV), locale  -> {"text": ...}

An answer is streamed (D-216). `ask` returns as soon as the question has been
refused or has passed both checks, with an `answer_id`; the model writes on in
the background, and each `more` returns the sentences released since the last
one. Every reply has the same shape, `progress()`: the new `sentences`, whether
the answer is `done`, and once it is, how it ended - the Reply's category,
stage, refusal key and alert. Polling rather than a streamed reply keeps every
hop a single request and a single response, as every other operation in BioKids
is, each with its own timeout: a launcher that goes away simply stops asking,
and the answer it left is stopped (`ABANDONED_S`).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from biokids_ai import prompts
from biokids_ai.assistant import Assistant, Reply, cut, refuse
from biokids_ai.knowledge import PORT as KNOWLEDGE_PORT
from biokids_ai.knowledge import Book, Knowledge
from biokids_ai.llm import WARMUP_TIMEOUT_S, ChatModel, ModelError
from biokids_ai.localhttp import Endpoint, is_healthy
from biokids_ai.models import ModelFile, by_key
from biokids_ai.runtime import ManagedServer, ServerUnavailable
from biokids_ai.setup import read_selection
from biokids_ai.stt import Transcriber, TranscriptionError
from biokids_core.config import Config
from biokids_core.errors import IpcError
from biokids_core.ipc import MAX_LINE_BYTES, Request, Response, decode_frame

log = logging.getLogger(__name__)

SOCKET_NAME: Final = "ai.sock"
LLM_SOCKET_NAME: Final = "llm.sock"
STT_PORT: Final = 8178
"""whisper-server listens here inside the service's own, empty network
namespace; nothing outside the service can reach it."""
SOCKET_MODE: Final = 0o660
CONTEXT_TOKENS: Final = 4096
"""Split across two slots: one keeps the classifier's prompt warm, the other
the answer's. With one slot each call evicted the other's cached prompt and
every question paid for both in full."""


MAX_THREADS: Final = 8

ABANDONED_S: Final = 60.0
"""An answer nobody has asked `more` of for this long is stopped and forgotten.
The daemon asks for the launcher twice a second and gives up on a silent
launcher after fifteen seconds, collecting what is left; this is the backstop
for a daemon that went away, so the model is not left writing to nobody."""
REAP_EVERY_S: Final = 5.0


@dataclass(eq=False)
class AnswerInProgress:
    """One answer being written, until the daemon has read all of it (D-216).

    Kept in memory only, and only until it has been read or abandoned: an
    ordinary answer is forgotten once it has been shown (D-165).
    """

    id: str
    seen: float
    """When the daemon last asked about it, on the service's monotonic clock."""
    sentences: list[str] = field(default_factory=list)
    taken: int = 0
    """How many of `sentences` the daemon has already been given."""
    reply: Reply | None = None
    """How the answer ended, once it has."""
    decided: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once the question was refused or has passed both checks."""
    task: asyncio.Task[None] | None = None

    def finish(self, reply: Reply) -> None:
        if self.reply is None:
            self.reply = reply
        self.decided.set()


def progress(answer_id: str, sentences: list[str], reply: Reply | None) -> dict[str, Any]:
    """What `ask` and `more` return: new sentences, and the ending once there is one."""
    data: dict[str, Any] = {
        "answer_id": answer_id,
        "sentences": sentences,
        "done": reply is not None,
        "refused": False,
        "category": "ok",
        "stage": "answer",
        "refusal_key": "",
        "alert": False,
    }
    if reply is not None:
        data.update(
            refused=reply.refused,
            category=reply.category,
            stage=reply.stage,
            refusal_key=reply.refusal_key,
            alert=reply.alert,
        )
    return data


def thread_count(configured: int, cpus: int | None = None) -> int:
    """Half the logical CPUs, at most eight, unless the parent set a number.

    llama.cpp's threads wait for each other by spinning, so one thread that
    loses its CPU stalls all of them. Measured on the development VM (eight
    vCPUs on a 6P+4E laptop, Gemma 4 E4B): four or six threads read a prompt at
    29 tokens a second and write at 11; eight managed 0.1. Half the logical
    CPUs is roughly the physical cores on a real machine and leaves room for
    the child's own app.
    """
    if configured > 0:
        return configured
    logical = cpus if cpus is not None else (os.cpu_count() or 2)
    return max(1, min(MAX_THREADS, logical // 2))


class AssistantService:
    def __init__(self, config: Config, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.models_dir = config.paths.ai_models_dir
        self.run_dir = config.paths.ai_run_dir
        self._question_lock = asyncio.Lock()
        self._llm: ManagedServer | None = None
        self._stt: ManagedServer | None = None
        self._assistant: Assistant | None = None
        self._knowledge: Knowledge | None = None
        self._transcriber: Transcriber | None = None
        self._loaded: dict[str, str] = {}
        self._clock = clock
        self._answers: dict[str, AnswerInProgress] = {}
        self._reaper: asyncio.Task[None] | None = None

    # ------------------------------------------------------------- models

    def selected(self, role: str) -> ModelFile | None:
        key = read_selection(self.models_dir).get(role, "")
        model = by_key(key)
        if model is None or model.role != role:
            return None
        if not (self.models_dir / model.filename).is_file():
            return None
        return model

    def _servers(self) -> None:
        """(Re)build the servers when the parent's selection changed on disk."""
        threads = str(thread_count(self.config.ai.threads))
        idle = self.config.ai.idle_unload_minutes * 60.0
        chat = self.selected("chat")
        if chat is not None and self._loaded.get("chat") != chat.key:
            endpoint = Endpoint(f"unix:{self.run_dir / LLM_SOCKET_NAME}")
            self._llm = ManagedServer(
                "llama-server",
                [
                    self.config.ai.llama_server_binary,
                    "--model",
                    str(self.models_dir / chat.filename),
                    "--host",
                    str(self.run_dir / LLM_SOCKET_NAME),
                    "--ctx-size",
                    str(CONTEXT_TOKENS),
                    "--threads",
                    threads,
                    "--parallel",
                    "2",
                    "--no-webui",
                    "--jinja",
                ],
                endpoint,
                idle_seconds=idle,
            )
            self._assistant = Assistant(ChatModel(endpoint), reference=self._reference())
            self._loaded["chat"] = chat.key
        stt = self.selected("stt")
        if stt is not None and self._loaded.get("stt") != stt.key:
            endpoint = Endpoint(f"http://127.0.0.1:{STT_PORT}")
            self._stt = ManagedServer(
                "whisper-server",
                [
                    self.config.ai.whisper_server_binary,
                    "--model",
                    str(self.models_dir / stt.filename),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(STT_PORT),
                    "--threads",
                    threads,
                ],
                endpoint,
                idle_seconds=idle,
            )
            self._transcriber = Transcriber(endpoint)
            self._loaded["stt"] = stt.key

    def _reference(self) -> Knowledge | None:
        """kiwix-serve over the encyclopedias on disk (D-178), or None."""
        books = [
            Book(
                locale=role.removeprefix("knowledge-"), name=model.filename.removesuffix(".zim")
            )
            for role in ("knowledge-tr", "knowledge-en")
            if (model := self.selected(role)) is not None
        ]
        if not books:
            return None
        endpoint = Endpoint(f"http://127.0.0.1:{KNOWLEDGE_PORT}")

        async def healthy(target: Endpoint) -> bool:
            return await is_healthy(target, "/")

        server = ManagedServer(
            "kiwix-serve",
            [
                self.config.ai.kiwix_serve_binary,
                "--address=127.0.0.1",
                f"--port={KNOWLEDGE_PORT}",
                "--nosearchbar",
                *(str(self.models_dir / f"{book.name}.zim") for book in books),
            ],
            endpoint,
            idle_seconds=self.config.ai.idle_unload_minutes * 60.0,
            start_timeout=30.0,
            healthy=healthy,
        )
        self._knowledge = Knowledge(server, books)
        return self._knowledge

    # ---------------------------------------------------------------- ops

    async def handle(self, request: Request) -> Response:
        match request.op:
            case "status":
                return Response(id=request.id, ok=True, data=self.status())
            case "ask":
                return Response(id=request.id, ok=True, data=await self.ask(request.args))
            case "more":
                answer_id = str(request.args.get("answer_id", ""))
                return Response(id=request.id, ok=True, data=self.more(answer_id))
            case "cancel":
                cancelled = self.cancel(str(request.args.get("answer_id", "")))
                return Response(id=request.id, ok=True, data={"cancelled": cancelled})
            case "transcribe":
                return await self.transcribe(request)
            case "warmup":
                return Response(id=request.id, ok=True, data=await self.warmup(request.args))
            case _:
                return Response.failure(request.id, f"unknown op: {request.op}")

    def status(self) -> dict[str, Any]:
        chat, stt = self.selected("chat"), self.selected("stt")
        return {
            "ready": chat is not None,
            "chat": chat.key if chat else "",
            "stt": stt.key if stt else "",
            "voice": stt is not None,
            "knowledge": [
                role.removeprefix("knowledge-")
                for role in ("knowledge-tr", "knowledge-en")
                if self.selected(role) is not None
            ],
            "loaded": bool(self._llm and self._llm.running),
        }

    async def ask(self, args: dict[str, Any]) -> dict[str, Any]:
        """Start answering; return once the question is refused or accepted (D-216).

        The checks run before this returns, exactly as before: a refused
        question comes back done, with its refusal, and the model has written
        nothing. An accepted one comes back with an `answer_id` and, usually,
        no sentences yet; the model writes on while the daemon asks `more`.
        """
        self._servers()
        llm, assistant = self._llm, self._assistant
        if llm is None or assistant is None:
            return progress("", [], refuse("unavailable", "error"))
        answer = AnswerInProgress(id=secrets.token_hex(8), seen=self._clock())
        self._answers[answer.id] = answer
        answer.task = asyncio.create_task(
            self._write(answer, llm, assistant, args), name=f"answer-{answer.id}"
        )
        self._watch()
        await answer.decided.wait()
        return self._take(answer)

    async def _write(
        self,
        answer: AnswerInProgress,
        llm: ManagedServer,
        assistant: Assistant,
        args: dict[str, Any],
    ) -> None:
        """The whole pipeline for one question, holding the model while it writes."""
        try:
            async with self._question_lock:
                try:
                    await llm.ensure()
                except ServerUnavailable as exc:
                    log.warning("%s", exc)
                    answer.finish(refuse("unavailable", "error"))
                    return
                reply = await assistant.ask(
                    str(args.get("question", "")),
                    age=_int(args.get("age"), 7),
                    locale=str(args.get("locale", "tr")),
                    persona=str(args.get("persona", "zio")),
                    name=str(args.get("name", ""))[:40],
                    on_answering=answer.decided.set,
                    on_sentence=answer.sentences.append,
                    conversation=conversation_from(args),
                )
                answer.finish(reply)
        except asyncio.CancelledError:
            answer.finish(cut("unavailable", "error", answer.sentences))
            raise
        except Exception:  # one broken answer must not take the service down
            log.exception("an answer failed")
            answer.finish(cut("unavailable", "error", answer.sentences))

    def more(self, answer_id: str) -> dict[str, Any]:
        """The sentences released since the last call, and the ending once there is one.

        An answer this service does not know - finished and read, abandoned,
        or written before a restart - ends with "I'm resting": the launcher is
        never left asking about something that will not come.
        """
        answer = self._answers.get(answer_id)
        if answer is None:
            return progress(answer_id, [], refuse("unavailable", "error"))
        return self._take(answer)

    def cancel(self, answer_id: str) -> bool:
        """Stop writing an answer and forget it; the daemon asked a new question."""
        answer = self._answers.pop(answer_id, None)
        if answer is None:
            return False
        # A task cancelled before it ever ran never reaches its own handler,
        # and `ask` would wait for a decision that never comes.
        answer.finish(cut("unavailable", "error", answer.sentences))
        if answer.task is not None and not answer.task.done():
            answer.task.cancel()
        return True

    def _take(self, answer: AnswerInProgress) -> dict[str, Any]:
        answer.seen = self._clock()
        fresh = answer.sentences[answer.taken :]
        answer.taken = len(answer.sentences)
        if answer.reply is not None:
            self._answers.pop(answer.id, None)  # read to its end: forgotten
        return progress(answer.id, fresh, answer.reply)

    def forget_abandoned(self) -> int:
        """Stop the answers nobody has asked about for `ABANDONED_S`; how many."""
        now = self._clock()
        stale = [
            answer.id
            for answer in self._answers.values()
            # Not before the checks are done: `ask` is still waiting for them.
            if answer.decided.is_set() and now - answer.seen > ABANDONED_S
        ]
        for answer_id in stale:
            log.info("an answer nobody is reading any more was stopped")
            self.cancel(answer_id)
        return len(stale)

    def _watch(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap(), name="answers-reaper")

    async def _reap(self) -> None:
        while self._answers:
            await asyncio.sleep(REAP_EVERY_S)
            self.forget_abandoned()

    async def warmup(self, args: dict[str, Any]) -> dict[str, Any]:
        """Load the model and its two fixed prompts before the first question.

        Called when a child opens the assistant (D-165). A child takes half a
        minute to type a question; the model takes about as long to load and
        read its instructions, so by the time the question comes it is ready.
        Both prompts stay cached, one per server slot.
        """
        self._servers()
        if self._llm is None or self._assistant is None:
            return {"ready": False}
        async with self._question_lock:
            try:
                await self._llm.ensure()
                model = self._assistant.model
                age = _int(args.get("age"), 7)
                locale = str(args.get("locale", "tr"))
                await model.classify("merhaba", age, wait_s=WARMUP_TIMEOUT_S)
                await model.answer(
                    "merhaba",
                    age,
                    locale,
                    "zio",
                    str(args.get("name", "")),
                    wait_s=WARMUP_TIMEOUT_S,
                )
            except (ServerUnavailable, ModelError) as exc:
                log.warning("the assistant could not warm up: %s", exc)
                return {"ready": False}
        return {"ready": True}

    async def transcribe(self, request: Request) -> Response:
        self._servers()
        if self._stt is None or self._transcriber is None:
            return Response.failure(request.id, "speech is not installed")
        try:
            wav = base64.b64decode(str(request.args.get("audio", "")), validate=True)
        except (binascii.Error, ValueError):
            return Response.failure(request.id, "audio is not base64")
        try:
            await self._stt.ensure()
            text = await self._transcriber.transcribe(
                wav, str(request.args.get("locale", "tr"))
            )
        except (ServerUnavailable, TranscriptionError) as exc:
            log.warning("speech could not be turned into text: %s", exc)
            return Response.failure(request.id, "speech unavailable")
        finally:
            del wav  # the recording ends here, in memory, as promised (D-166)
        return Response(id=request.id, ok=True, data={"text": text})

    async def aclose(self) -> None:
        for answer_id in list(self._answers):
            self.cancel(answer_id)
        if self._reaper is not None:
            self._reaper.cancel()
        for server in (self._llm, self._stt):
            if server is not None:
                await server.stop()

    # ------------------------------------------------------------- socket

    async def serve(self) -> None:
        path = self.run_dir / SOCKET_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if path.is_socket():
                path.unlink()
        start_unix_server = getattr(asyncio, "start_unix_server", None)
        if start_unix_server is None:  # pragma: no cover - development host only
            raise IpcError("unix sockets are not available on this platform")
        server = await cast("Any", start_unix_server)(
            self._connection, str(path), limit=MAX_LINE_BYTES + 4096
        )
        with contextlib.suppress(OSError):
            path.chmod(SOCKET_MODE)
        log.info("biokids-ai listening on %s", path)
        try:
            async with server:
                await server.serve_forever()
        finally:
            await self.aclose()
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
                    writer.write(Response.failure("", "frame too long").encode())
                    await writer.drain()
                    continue
                if not raw:
                    return
                if not raw.strip():
                    continue
                try:
                    payload = decode_frame(raw)
                    request = Request(
                        op=str(payload.get("op", "")),
                        id=str(payload.get("id", "")),
                        args=dict(payload.get("args") or {}),
                    )
                except (IpcError, TypeError, ValueError):
                    writer.write(Response.failure("", "malformed frame").encode())
                    await writer.drain()
                    continue
                writer.write((await self.handle(request)).encode())
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()


def conversation_from(args: dict[str, Any]) -> prompts.Conversation | None:
    """English with Sio (D-226), from what the daemon sent; None for a question.

    Bounded here as well as in the daemon: what was said so far is the last
    `PRACTICE_HISTORY` messages of at most `MAX_TURN_CHARS` each, and the words
    are short ones.
    """
    if args.get("mode") != prompts.PRACTICE:
        return None
    history: list[tuple[str, str]] = []
    raw = args.get("history")
    for entry in raw if isinstance(raw, list) else []:
        if (
            isinstance(entry, list | tuple)
            and len(entry) == 2
            and entry[0] in {"child", "sio"}
            and isinstance(entry[1], str)
            and entry[1].strip()
        ):
            history.append((str(entry[0]), " ".join(entry[1].split())[:MAX_TURN_CHARS]))
    words = args.get("words")
    known = tuple(
        str(word)[:MAX_WORD_CHARS]
        for word in (words if isinstance(words, list) else [])
        if isinstance(word, str) and word.strip()
    )[: prompts.PRACTICE_WORDS]
    language = str(args.get("first_language", "tr"))
    return prompts.Conversation(
        history=tuple(history[-prompts.PRACTICE_HISTORY :]),
        words=known,
        first_language=language if language in prompts.FIRST_LANGUAGE else "tr",
    )


MAX_TURN_CHARS = 300
MAX_WORD_CHARS = 30


def _int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except ValueError:
        return default


def socket_path(config: Config) -> Path:
    return config.paths.ai_run_dir / SOCKET_NAME
