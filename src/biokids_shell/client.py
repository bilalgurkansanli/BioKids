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

    async def english_state(self) -> dict[str, Any] | None:
        """This child's boxes in the English corner (D-225), or None."""
        try:
            response = await self._client.call(ShellOp.ENGLISH_STATE)
        except (IpcError, OSError) as exc:
            log.warning("could not read the English corner: %s", exc)
            return None
        return dict(response.data) if response.ok else None

    async def english_answer(self, item: str, right: bool) -> int | None:
        """Record one answer; the item's new box, or None."""
        try:
            response = await self._client.call(ShellOp.ENGLISH_ANSWER, item=item, right=right)
        except (IpcError, OSError) as exc:
            log.warning("the English answer was not recorded: %s", exc)
            return None
        return int(response.data.get("box", 0)) if response.ok else None

    async def report_refused_search(self, words: str, host: str) -> None:
        try:
            await self._client.call(ShellOp.REPORT_SEARCH, words=words, host=host)
        except (IpcError, OSError):
            return

    async def report_activity(self, app_slug: str, app_name: str = "", unit: str = "") -> None:
        """Which app is in front, and the scope it runs in when known. Its kind
        is the daemon's to look up (D-228)."""
        try:
            await self._client.call(
                ShellOp.REPORT_ACTIVITY,
                app_slug=app_slug,
                app_name=app_name,
                unit=unit,
            )
        except (IpcError, OSError):
            log.debug("could not report activity; the daemon will time it out")

    async def reading_state(self) -> dict[str, Any] | None:
        """The calling child's boxes in the reading corner (D-241), or None."""
        try:
            response = await self._client.call(ShellOp.READING_STATE)
        except (IpcError, OSError) as exc:
            log.warning("could not read the reading corner's progress: %s", exc)
            return None
        return response.data if response.ok else None

    async def reading_answer(self, item: str, right: bool) -> int | None:
        try:
            response = await self._client.call(ShellOp.READING_ANSWER, item=item, right=right)
        except (IpcError, OSError) as exc:
            log.warning("the reading answer was not recorded: %s", exc)
            return None
        return int(response.data.get("box", 0)) if response.ok else None

    async def share_recording(self, audio_b64: str, seconds: float) -> bool:
        """Sesim (D-240): the recording the child chose, for the parent's panel."""
        try:
            response = await self._client.call(
                ShellOp.SHARE_RECORDING, audio=audio_b64, seconds=seconds
            )
        except (IpcError, OSError) as exc:
            log.warning("could not send the recording: %s", exc)
            return False
        return response.ok

    async def live_frame(self, frame_b64: str) -> bool:
        """A picture of this screen for the parent watching it (D-253). False
        when nobody watches any more, or the daemon cannot be reached."""
        try:
            response = await self._client.call(ShellOp.LIVE_FRAME, frame=frame_b64)
        except (IpcError, OSError) as exc:
            log.debug("could not send the screen: %s", exc)
            return False
        return response.ok and bool(response.data.get("watching"))

    async def note_seen(self, note_id: int) -> bool:
        """The screen showed a parent's note (D-233); the panel says "okundu"."""
        try:
            return (await self._client.call(ShellOp.NOTE_SEEN, note_id=note_id)).ok
        except (IpcError, OSError) as exc:
            log.info("could not say that a note was seen: %s", exc)
            return False

    async def finish_up(self) -> int:
        """Ask for the last round (D-230): the seconds given, 0 when none was due
        or the daemon could not be asked."""
        try:
            response = await self._client.call(ShellOp.FINISH_UP)
        except (IpcError, OSError) as exc:
            log.warning("could not ask for the last round: %s", exc)
            return 0
        if not response.ok:
            return 0
        try:
            return max(0, int(response.data.get("seconds", 0)))
        except (TypeError, ValueError):
            return 0

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

    async def switch_user(self) -> bool:
        """ "Oturumu değiştir" (D-227): True once the daemon has taken it.

        As for "Evet, kapat", the daemon answers first and ends the session
        afterwards, so the goodbye can be drawn before it goes.
        """
        try:
            response = await self._client.call(ShellOp.SWITCH_USER)
        except (IpcError, OSError) as exc:
            log.warning("could not ask to switch user: %s", exc)
            return False
        if not response.ok:
            log.warning("the daemon did not take the switch: %s", response.error)
        return response.ok

    async def ask_a_parent(self, url: str, kind: str = "site") -> tuple[bool, str]:
        """(filed, reason). The reason is a catalog key when it was not filed."""
        try:
            response = await self._client.call(ShellOp.ASK_A_PARENT, url=url, kind=kind)
        except (IpcError, OSError):
            return False, "shell.error.unknown"
        return response.ok, response.error or ""

    async def ask_assistant(
        self,
        question: str,
        *,
        mode: str = "",
        history: list[list[str]] | None = None,
        words: list[str] | None = None,
    ) -> dict[str, Any]:
        """The start of the assistant's reply (D-165, D-216). Resting when unreachable.

        Its own connection with a long timeout: the first question after a pause
        waits for the model to load, which is seconds on a good machine and can
        be minutes on an old one. It returns once the question is refused or
        accepted; an accepted one carries the `answer_id` that `assistant_more`
        asks about.
        """
        client = AsyncDaemonClient(self.socket_path, timeout=ASSISTANT_TIMEOUT_S)
        try:
            args: dict[str, Any] = {"question": question}
            if mode:
                # A turn of English with Sio (D-226).
                args |= {"mode": mode, "history": history or [], "words": words or []}
            response = await client.call(ShellOp.ASK_ASSISTANT, **args)
        except (IpcError, OSError) as exc:
            log.warning("could not ask the assistant: %s", exc)
            return resting()
        if not response.ok:
            return resting()
        return dict(response.data)

    async def assistant_more(self, answer_id: str) -> dict[str, Any]:
        """The next sentences of an answer, and its ending once it has one (D-216).

        A daemon that cannot be reached ends the answer with "I'm resting": the
        page stops asking, and the child is not left in front of a sentence
        that never finishes.
        """
        client = AsyncDaemonClient(self.socket_path, timeout=ASSISTANT_POLL_TIMEOUT_S)
        try:
            response = await client.call(ShellOp.ASSISTANT_MORE, answer_id=answer_id)
        except (IpcError, OSError) as exc:
            log.warning("could not read the rest of the answer: %s", exc)
            return resting()
        if not response.ok:
            return resting()
        return dict(response.data)

    async def warm_assistant(self) -> None:
        """Start loading the model while the child thinks of a question."""
        client = AsyncDaemonClient(self.socket_path, timeout=ASSISTANT_TIMEOUT_S)
        try:
            await client.call(ShellOp.WARM_ASSISTANT)
        except (IpcError, OSError) as exc:
            log.info("the assistant could not warm up: %s", exc)

    async def hear_assistant(self, audio_b64: str, language: str = "") -> str | None:
        """The words in a press-and-speak recording (D-166), or None. With
        `language` "en", heard as English (D-225)."""
        client = AsyncDaemonClient(self.socket_path, timeout=ASSISTANT_TIMEOUT_S)
        args: dict[str, str] = {"audio": audio_b64}
        if language:
            args["language"] = language
        try:
            response = await client.call(ShellOp.HEAR_ASSISTANT, **args)
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
ASSISTANT_POLL_TIMEOUT_S = 15.0
"""For `assistant_more`, which the daemon answers from the assistant's memory;
longer than the daemon's own ten seconds for the same question."""


def resting() -> dict[str, Any]:
    """An answer that ends now, with "Şu an biraz dinleniyorum" (D-165)."""
    return {
        "refused": True,
        "done": True,
        "sentences": [],
        "refusal_key": "assistant.refuse.unavailable",
    }
