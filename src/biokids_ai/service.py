"""`biokids-ai`: the assistant's service, on a socket only the daemon talks to (D-165).

It runs as the `biokids-ai` user with no network at all (`PrivateNetwork=yes`)
and cannot read the family's database. It does not decide whether a child may
use the assistant - the daemon does, from the child's policy, before a question
ever gets here - and it never writes an alert: it says what it refused and why,
and the daemon, which owns the database, tells the parent.

Operations, line-delimited JSON as everywhere else in BioKids:

    status                         which models are installed and selected
    ask      question, age, locale, persona  -> a Reply (assistant.py)
    transcribe   audio (base64 WAV), locale  -> {"text": ...}
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import os
from pathlib import Path
from typing import Any, Final, cast

from biokids_ai.assistant import Assistant, Reply, refuse
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
    def __init__(self, config: Config) -> None:
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
                reply = await self.ask(request.args)
                return Response(id=request.id, ok=True, data=reply.as_dict())
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

    async def ask(self, args: dict[str, Any]) -> Reply:
        question = str(args.get("question", ""))
        age = _int(args.get("age"), 7)
        locale = str(args.get("locale", "tr"))
        persona = str(args.get("persona", "zio"))
        self._servers()
        if self._llm is None or self._assistant is None:
            return refuse("unavailable", "error")
        async with self._question_lock:
            try:
                await self._llm.ensure()
            except ServerUnavailable as exc:
                log.warning("%s", exc)
                return refuse("unavailable", "error")
            return await self._assistant.ask(
                question,
                age=age,
                locale=locale,
                persona=persona,
                name=str(args.get("name", ""))[:40],
            )

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


def _int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except ValueError:
        return default


def socket_path(config: Config) -> Path:
    return config.paths.ai_run_dir / SOCKET_NAME
