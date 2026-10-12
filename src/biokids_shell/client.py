"""Talking to the daemon from inside the child's session.

A thin wrapper over the shell socket that never raises at a call site. Every
method has a defined answer for "the daemon is not there", because the launcher
is the only thing on the child's screen: if it throws, a six-year-old is looking
at a blank window with no way out, which is a worse failure than any it could be
reporting.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from biokids_core.errors import IpcError
from biokids_core.ipc import AsyncDaemonClient, Event, ShellOp
from biokids_core.types import ShutdownOutcome

log = logging.getLogger(__name__)


class ShellClient:
    def __init__(self, socket_path: Path, timeout: float = 5.0) -> None:
        self._client = AsyncDaemonClient(socket_path, timeout=timeout)
        self.socket_path = socket_path

    async def available(self) -> bool:
        try:
            return (await self._client.call(ShellOp.PING)).ok
        except (IpcError, OSError):
            return False

    async def state(self) -> dict[str, Any] | None:
        """Identity, tiles and remaining time. None when the daemon is absent."""
        try:
            response = await self._client.call(ShellOp.GET_STATE)
        except (IpcError, OSError) as exc:
            log.warning("could not read the launcher state: %s", exc)
            return None
        return response.data if response.ok else None

    async def may_launch(self, app_slug: str) -> tuple[bool, str]:
        """(allowed, reason_code). Refused rather than allowed when unreachable.

        This is the one place the launcher fails *closed*. Starting an
        application because the daemon did not answer would mean the one moment
        supervision is broken is the moment a child gets whatever they like.
        """
        try:
            response = await self._client.call(ShellOp.LAUNCH, app_slug=app_slug)
        except (IpcError, OSError) as exc:
            log.warning("could not ask about %s: %s", app_slug, exc)
            return False, "error.daemon_unreachable"
        if not response.ok:
            return False, response.error or "error.unknown"
        return bool(response.data.get("allow")), str(response.data.get("reason_code", ""))

    async def report_visit(self, host: str) -> None:
        try:
            await self._client.call(ShellOp.REPORT_VISIT, host=host)
        except (IpcError, OSError):
            return

    async def report_activity(self, app_slug: str, app_name: str, category: str) -> None:
        try:
            await self._client.call(
                ShellOp.REPORT_ACTIVITY,
                app_slug=app_slug,
                app_name=app_name,
                category=category,
            )
        except (IpcError, OSError):
            log.debug("could not report activity; the daemon will time it out")

    async def first_run_done(self) -> None:
        try:
            await self._client.call(ShellOp.FIRST_RUN_DONE)
        except (IpcError, OSError):
            log.warning("could not record that the first-run screen was seen")

    async def request_shutdown(self) -> ShutdownOutcome | None:
        """What "Evet, kapat" does, or None when the daemon could not be asked.

        The daemon answers before it acts (D-209), so the five seconds are
        plenty. None is never shown as the home screen: put back where they
        were, a child cannot tell "it did not work" from "it is about to", and
        walks away from an open session.
        """
        try:
            response = await self._client.call(ShellOp.REQUEST_SHUTDOWN)
        except (IpcError, OSError) as exc:
            log.warning("could not ask to shut down: %s", exc)
            return None
        if not response.ok:
            log.warning("the daemon did not take the shutdown: %s", response.error)
            return None
        try:
            return ShutdownOutcome(str(response.data.get("outcome", ShutdownOutcome.POWEROFF)))
        except ValueError:
            return None

    async def ask_a_parent(self, url: str, kind: str = "site") -> tuple[bool, str]:
        """(filed, reason). The reason is a catalog key when it was not filed."""
        try:
            response = await self._client.call(ShellOp.ASK_A_PARENT, url=url, kind=kind)
        except (IpcError, OSError):
            return False, "shell.error.unknown"
        return response.ok, response.error or ""

    async def ask_assistant(self, question: str) -> dict[str, Any]:
        """The assistant's reply (D-165). A refusal when anything is unreachable.

        Its own connection with a long timeout: the first question after a pause
        waits for the model to load, which is seconds on a good machine and can
        be minutes on an old one.
        """
        client = AsyncDaemonClient(self.socket_path, timeout=ASSISTANT_TIMEOUT_S)
        try:
            response = await client.call(ShellOp.ASK_ASSISTANT, question=question)
        except (IpcError, OSError) as exc:
            log.warning("could not ask the assistant: %s", exc)
            return {"refused": True, "refusal_key": "assistant.refuse.unavailable"}
        if not response.ok:
            return {"refused": True, "refusal_key": "assistant.refuse.unavailable"}
        return dict(response.data)

    async def warm_assistant(self) -> None:
        """Start loading the model while the child thinks of a question."""
        client = AsyncDaemonClient(self.socket_path, timeout=ASSISTANT_TIMEOUT_S)
        try:
            await client.call(ShellOp.WARM_ASSISTANT)
        except (IpcError, OSError) as exc:
            log.info("the assistant could not warm up: %s", exc)

    async def hear_assistant(self, audio_b64: str) -> str | None:
        """The words in a press-and-speak recording (D-166), or None."""
        client = AsyncDaemonClient(self.socket_path, timeout=ASSISTANT_TIMEOUT_S)
        try:
            response = await client.call(ShellOp.HEAR_ASSISTANT, audio=audio_b64)
        except (IpcError, OSError) as exc:
            log.warning("could not reach the assistant's ears: %s", exc)
            return None
        if not response.ok:
            return None
        return str(response.data.get("text", ""))

    async def events(self) -> AsyncIterator[Event]:
        async for event in self._client.stream_events():
            yield event


ASSISTANT_TIMEOUT_S = 300.0
