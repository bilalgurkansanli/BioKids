"""llama-server and whisper-server: started when a question comes, stopped when idle.

D-165. A loaded chat model holds one to four gigabytes of a family laptop's
memory, which the child's own apps need more. So neither server runs until it
is needed, and each is stopped after `idle_unload_minutes` with nothing asked
(docs/AI-LAYER.md section 10's idle budget). The first question after a pause
waits for the model to load - seconds, not minutes - and the launcher says so.

A server that will not start, or dies, is reported as unavailable; the
assistant then refuses rather than answering without its checks (principle 3).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, Final

from biokids_ai.localhttp import Endpoint, is_healthy

log = logging.getLogger(__name__)

START_TIMEOUT_S: Final = 240.0
"""A 3 GB model read from a slow laptop disk takes a while; give it four minutes."""
POLL_S: Final = 0.5
STDERR_LINES: Final = 20
"""How much of a server's own error output is kept, to say why it stopped (D-248)."""

Spawn = Callable[..., Awaitable[Any]]
Health = Callable[[Endpoint], Awaitable[bool]]


class ServerUnavailable(Exception):
    pass


class ManagedServer:
    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        endpoint: Endpoint,
        *,
        idle_seconds: float,
        start_timeout: float = START_TIMEOUT_S,
        spawn: Spawn = asyncio.create_subprocess_exec,
        healthy: Health = is_healthy,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.endpoint = endpoint
        self.idle_seconds = idle_seconds
        self.start_timeout = start_timeout
        self._spawn = spawn
        self._healthy = healthy
        self._clock = clock
        self.process: Any = None
        self.last_used = 0.0
        self.starts = 0
        self._lock = asyncio.Lock()
        self._stderr: collections.deque[str] = collections.deque(maxlen=STDERR_LINES)
        """The server's last lines of error output. Read as they come, so a
        server that writes a lot never fills the pipe and stops."""
        self._stderr_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def ensure(self) -> None:
        """Running and answering, or ServerUnavailable."""
        async with self._lock:
            if self.running and await self._healthy(self.endpoint):
                self._touch()
                return
            await self._stop_locked()
            await self._start_locked()
            self._touch()

    def _touch(self) -> None:
        self.last_used = self._clock()
        if self._idle_task is None or self._idle_task.done():
            self._idle_task = asyncio.create_task(self._idle_watch(), name=f"{self.name}-idle")

    async def _keep_stderr(self, stream: asyncio.StreamReader) -> None:
        with contextlib.suppress(Exception):
            async for line in stream:
                text = line.decode("utf-8", "replace").strip()
                if text:
                    self._stderr.append(text[:300])

    async def _start_locked(self) -> None:
        self._clear_stale_socket()
        log.info("starting %s", self.name)
        try:
            self.process = await self._spawn(
                *self.argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ServerUnavailable(f"{self.name} could not be started: {exc}") from exc
        self._stderr.clear()
        stream = getattr(self.process, "stderr", None)
        if stream is not None:
            self._stderr_task = asyncio.create_task(
                self._keep_stderr(stream), name=f"{self.name}-stderr"
            )
        self.starts += 1
        deadline = self._clock() + self.start_timeout
        while self._clock() < deadline:
            if not self.running:
                code = getattr(self.process, "returncode", None)
                self.process = None
                await asyncio.sleep(0)  # the last lines it wrote, read
                said = f": {self._stderr[-1]}" if self._stderr else ""
                raise ServerUnavailable(f"{self.name} exited while loading (code {code}){said}")
            if await self._healthy(self.endpoint):
                log.info("%s is ready", self.name)
                return
            await asyncio.sleep(POLL_S)
        await self._stop_locked()
        raise ServerUnavailable(
            f"{self.name} did not become ready in {self.start_timeout:.0f}s"
        )

    def _clear_stale_socket(self) -> None:
        if self.endpoint.address.startswith("unix:"):
            path = Path(self.endpoint.address.removeprefix("unix:"))
            with contextlib.suppress(OSError):
                if path.is_socket():
                    path.unlink()

    async def _idle_watch(self) -> None:
        while True:
            remaining = self.last_used + self.idle_seconds - self._clock()
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            async with self._lock:
                if self._clock() - self.last_used >= self.idle_seconds:
                    if self.running:
                        log.info("%s idle for %.0fs; unloading", self.name, self.idle_seconds)
                    await self._stop_locked()
                    return

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()

    async def _stop_locked(self) -> None:
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=10)
        except TimeoutError:  # pragma: no cover - a wedged server
            process.kill()
