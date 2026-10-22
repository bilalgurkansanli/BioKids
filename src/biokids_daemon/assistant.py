"""Where a child's question to the assistant is allowed, counted, and reported (D-165).

The launcher asks on the child's socket; the daemon knows *which* child from
the kernel (D-123) and decides from that child's policy whether the assistant
is on, whether press-and-speak is on, and whether they have asked too much in
the last ten minutes. Only then does the question reach `biokids-ai`, which has
no database and no say in any of that.

When the assistant refuses a question for a reason a parent should know about,
this is where the alert is written - the question itself, its category, when -
and where the panel hears about it. Questions that were answered are not kept
anywhere: the parent asked to know about the worrying ones, not to read every
question a child ever wondered about.

An answer arrives a sentence at a time (D-216). `ask` returns once the question
has been refused or has passed the assistant's checks; the launcher then asks
`more` about twice a second, and each call is carried to the assistant service
and back. The gate remembers, per child, the one answer in progress and the
question it answers, so that an answer the word list stops part-way still
becomes an alert with the child's words in it, and so that no child can read
another's answer. A launcher that stops asking is noticed here
(`ABANDONED_S`): what the answer came to is collected - an alert is still an
alert - and the model is told to stop.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from biokids_core import repo
from biokids_core.config import Config
from biokids_core.db.models import AiAlert
from biokids_core.errors import IpcError
from biokids_core.ipc import AsyncDaemonClient, EventKind, Response

log = logging.getLogger(__name__)

AI_SOCKET_NAME: Final = "ai.sock"
ASK_TIMEOUT_S: Final = 300.0
"""A first question loads the model: up to four minutes on a slow disk."""
POLL_TIMEOUT_S: Final = 10.0
"""`more` and `cancel` answer from memory at once; this only bounds a wedged service."""
ABANDONED_S: Final = 15.0
"""A launcher that has not asked for the rest of an answer for this long has gone:
the child went home, or the launcher closed. Its page asks twice a second, and a
browser slows a page it is not showing to once a second at the most."""
WATCH_EVERY_S: Final = 5.0
WINDOW_S: Final = 600.0
MAX_AUDIO_B64: Final = 700_000
MAX_ALERT_TEXT: Final = 500
MAX_ANSWER_ID: Final = 64
PRACTICE: Final = "english"
"""The conversation mode of English with Sio (D-226)."""
MAX_HISTORY: Final = 8
MAX_TURN_CHARS: Final = 300
MAX_WORDS: Final = 30


class AiClient(Protocol):
    async def call(self, op: str, **args: Any) -> Response: ...


def refusal(key: str, category: str) -> dict[str, Any]:
    """An answer that ends here, before or instead of anything the model wrote."""
    return {
        "refused": True,
        "text": "",
        "category": category,
        "stage": "daemon",
        "refusal_key": key,
        "alert": False,
        "done": True,
        "sentences": [],
        "answer_id": "",
    }


@dataclass(eq=False)
class OpenAnswer:
    """The answer a child is reading, while the model is still writing it (D-216)."""

    answer_id: str
    question: str
    """For the alert, should the answer be stopped part-way; never stored otherwise."""
    runtime: Any
    seen: float


class AssistantGate:
    def __init__(
        self,
        supervisor: Any,
        bus: Any,
        config: Config,
        *,
        client: AiClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.supervisor = supervisor
        self.bus = bus
        self.config = config
        path = Path(config.paths.ai_run_dir) / AI_SOCKET_NAME
        self.client: AiClient = client or AsyncDaemonClient(path, timeout=ASK_TIMEOUT_S)
        self.poller: AiClient = client or AsyncDaemonClient(path, timeout=POLL_TIMEOUT_S)
        """The same socket with a short timeout, for `more` and `cancel`."""
        self._clock = clock
        self._asked: dict[int, collections.deque[float]] = collections.defaultdict(
            collections.deque
        )
        self._open: dict[int, OpenAnswer] = {}
        self._watcher: asyncio.Task[None] | None = None

    def _policy(self, child_id: int) -> Any:
        runtime = self.supervisor._require(child_id)
        return runtime, runtime.policy

    def _over_limit(self, child_id: int, practice: bool = False) -> bool:
        """Too many in ten minutes. A conversation in English has its own count
        (D-226): its turns are short and come quickly, and a child talking with
        Sio should not use up their questions."""
        now = self._clock()
        asked = self._asked[child_id if not practice else -child_id]
        while asked and now - asked[0] > WINDOW_S:
            asked.popleft()
        limit = (
            self.config.ai.english_turns_per_10min
            if practice
            else self.config.ai.assistant_per_10min
        )
        if len(asked) >= max(1, limit):
            return True
        asked.append(now)
        return False

    async def ask(
        self,
        child_id: int,
        question: str,
        *,
        mode: str = "",
        history: list[list[str]] | None = None,
        words: list[str] | None = None,
    ) -> dict[str, Any]:
        """A question to the assistant: refused here, refused there, or begun.

        Begun means `done` is false and `answer_id` names the answer for `more`.
        With `mode` "english" it is a turn of a conversation in English with
        Sio (D-226): the same switch, the same checks, the same alerts.
        """
        runtime, policy = self._policy(child_id)
        practice = mode == PRACTICE
        if policy is None or not policy.assistant_enabled:
            return refusal("assistant.refuse.off", "off")
        if self._over_limit(child_id, practice):
            return refusal("assistant.refuse.slow_down", "slow_down")
        # One answer at a time per child: a new question stops the last one,
        # which the child has stopped reading, and frees the model for this.
        await self._close(child_id)

        child = repo.get_child(self.supervisor.session, child_id)
        age = child.age_on(dt.date.today()) if child is not None else 7
        try:
            extra: dict[str, Any] = (
                {
                    "mode": PRACTICE,
                    "history": history or [],
                    "words": words or [],
                    "first_language": runtime.locale,
                }
                if practice
                else {}
            )
            response = await self.client.call(
                "ask",
                question=question,
                age=age,
                locale=runtime.locale,
                persona="sio" if practice else "zio",
                name=runtime.display_name,
                **extra,
            )
        except IpcError as exc:
            log.warning("the assistant service is not answering: %s", exc)
            return refusal("assistant.refuse.unavailable", "unavailable")
        if not response.ok:
            return refusal("assistant.refuse.unavailable", "unavailable")

        reply = dict(response.data)
        answer_id = str(reply.get("answer_id", ""))[:MAX_ANSWER_ID]
        if reply.get("done", True) or not answer_id:
            reply["done"] = True
            if reply.get("alert"):
                self._report(runtime, child_id, question, reply)
            return reply
        self._open[child_id] = OpenAnswer(answer_id, question, runtime, self._clock())
        self._watch()
        return reply

    async def more(self, child_id: int, answer_id: str) -> dict[str, Any]:
        """The next sentences of this child's answer in progress, and its ending (D-216).

        Only the answer this child is reading: an id that is not theirs, or
        no longer open, ends with "I'm resting" and reaches nothing. A parent
        who switches the assistant off is obeyed between two sentences.
        """
        opened = self._open.get(child_id)
        if opened is None or not answer_id or opened.answer_id != answer_id:
            return refusal("assistant.refuse.unavailable", "unavailable")
        _runtime, policy = self._policy(child_id)
        if policy is None or not policy.assistant_enabled:
            await self._close(child_id)
            return refusal("assistant.refuse.off", "off")
        opened.seen = self._clock()
        reply = await self._collect(child_id, opened)
        if reply is None:
            return refusal("assistant.refuse.unavailable", "unavailable")
        return reply

    async def _collect(self, child_id: int, opened: OpenAnswer) -> dict[str, Any] | None:
        """One `more`, with the alert filed if the answer ended in one. None: gone."""
        try:
            response = await self.poller.call("more", answer_id=opened.answer_id)
        except IpcError as exc:
            log.warning("the assistant service stopped answering mid-answer: %s", exc)
            response = Response(ok=False)
        if not response.ok:
            self._forget(child_id, opened)
            return None
        reply = dict(response.data)
        if reply.get("done", True):
            reply["done"] = True
            self._forget(child_id, opened)
            if reply.get("alert"):
                self._report(opened.runtime, child_id, opened.question, reply)
        return reply

    def _forget(self, child_id: int, opened: OpenAnswer) -> None:
        if self._open.get(child_id) is opened:
            del self._open[child_id]

    async def _close(self, child_id: int) -> None:
        """Stop this child's answer in progress, if any, after a last look at it."""
        opened = self._open.get(child_id)
        if opened is None:
            return
        reply = await self._collect(child_id, opened)
        if reply is not None and not reply["done"]:
            self._forget(child_id, opened)
            with contextlib.suppress(IpcError):
                await self.poller.call("cancel", answer_id=opened.answer_id)

    async def forget_abandoned(self) -> int:
        """Close the answers whose launcher stopped asking; how many there were."""
        now = self._clock()
        gone = [
            child_id
            for child_id, opened in self._open.items()
            if now - opened.seen > ABANDONED_S
        ]
        for child_id in gone:
            log.info("child %s stopped reading an answer; it is stopped", child_id)
            await self._close(child_id)
        return len(gone)

    def _watch(self) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watch_loop(), name="assistant-answers")

    async def _watch_loop(self) -> None:
        while self._open:
            await asyncio.sleep(WATCH_EVERY_S)
            try:
                await self.forget_abandoned()
            except Exception:  # the watch must outlive one bad answer
                log.exception("could not close an abandoned answer")

    async def aclose(self) -> None:
        """The daemon is stopping: stop watching. The service stops its own answers."""
        if self._watcher is not None:
            self._watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher
            self._watcher = None
        self._open.clear()

    def _report(
        self, runtime: Any, child_id: int, question: str, reply: dict[str, Any]
    ) -> None:
        category = str(reply.get("category", "unknown"))[:32]
        session = self.supervisor.session
        alert = AiAlert(
            child_id=child_id,
            score=100,
            category=category,
            reason_key=f"assistant.category.{category}"[:64],
            confidence=1.0,
            band="notify",
            app="assistant",
            text=question[:MAX_ALERT_TEXT],
        )
        session.add(alert)
        repo.audit(
            session,
            actor=f"child:{runtime.unix_user}",
            action="assistant_refused",
            target=f"child:{child_id}",
            detail={"category": category, "stage": str(reply.get("stage", ""))},
            severity="warning",
        )
        session.commit()
        self.bus.publish(
            EventKind.ASSISTANT_ALERT, child_id=child_id, category=category, alert_id=alert.id
        )
        log.info("assistant refused a question from child %s (%s)", child_id, category)

    async def hear(
        self, child_id: int, audio_b64: str, language: str | None = None
    ) -> dict[str, Any]:
        """Press-and-speak: a short recording in, its words out. Nothing kept (D-166).

        `language` "en" hears the recording as English whatever the child's own
        language: a word said back in the English corner (D-225). The same
        switch guards it - the parent's "speak to the assistant" - because it
        is the same microphone.
        """
        runtime, policy = self._policy(child_id)
        if policy is None or not (policy.assistant_enabled and policy.assistant_voice):
            return {"ok": False, "error": "assistant.voice.off"}
        if len(audio_b64) > MAX_AUDIO_B64:
            return {"ok": False, "error": "assistant.voice.too_long"}
        try:
            response = await self.client.call(
                "transcribe", audio=audio_b64, locale=language or runtime.locale
            )
        except IpcError as exc:
            log.warning("the assistant service is not answering: %s", exc)
            return {"ok": False, "error": "assistant.voice.unavailable"}
        if not response.ok:
            return {"ok": False, "error": "assistant.voice.unavailable"}
        return {"ok": True, "text": str(response.data.get("text", ""))}

    async def warmup(self, child_id: int) -> dict[str, Any]:
        """Load the model as the child opens the assistant, if they may use it."""
        runtime, policy = self._policy(child_id)
        if policy is None or not policy.assistant_enabled:
            return {"ready": False}
        child = repo.get_child(self.supervisor.session, child_id)
        age = child.age_on(dt.date.today()) if child is not None else 7
        try:
            response = await self.client.call(
                "warmup", age=age, locale=runtime.locale, name=runtime.display_name
            )
        except IpcError:
            return {"ready": False}
        return dict(response.data) if response.ok else {"ready": False}

    async def status(self) -> dict[str, Any]:
        try:
            response = await self.client.call("status")
        except IpcError:
            return {"running": False}
        return {"running": True, **dict(response.data)} if response.ok else {"running": False}
