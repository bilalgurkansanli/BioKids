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
"""

from __future__ import annotations

import collections
import datetime as dt
import logging
import time
from collections.abc import Callable
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
WINDOW_S: Final = 600.0
MAX_AUDIO_B64: Final = 700_000
MAX_ALERT_TEXT: Final = 500


class AiClient(Protocol):
    async def call(self, op: str, **args: Any) -> Response: ...


def refusal(key: str, category: str) -> dict[str, Any]:
    return {
        "refused": True,
        "text": "",
        "category": category,
        "stage": "daemon",
        "refusal_key": key,
        "alert": False,
    }


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
        self.client: AiClient = client or AsyncDaemonClient(
            Path(config.paths.ai_run_dir) / AI_SOCKET_NAME, timeout=ASK_TIMEOUT_S
        )
        self._clock = clock
        self._asked: dict[int, collections.deque[float]] = collections.defaultdict(
            collections.deque
        )

    def _policy(self, child_id: int) -> Any:
        runtime = self.supervisor._require(child_id)
        return runtime, runtime.policy

    def _over_limit(self, child_id: int) -> bool:
        now = self._clock()
        asked = self._asked[child_id]
        while asked and now - asked[0] > WINDOW_S:
            asked.popleft()
        if len(asked) >= max(1, self.config.ai.assistant_per_10min):
            return True
        asked.append(now)
        return False

    async def ask(self, child_id: int, question: str) -> dict[str, Any]:
        runtime, policy = self._policy(child_id)
        if policy is None or not policy.assistant_enabled:
            return refusal("assistant.refuse.off", "off")
        if self._over_limit(child_id):
            return refusal("assistant.refuse.slow_down", "slow_down")

        child = repo.get_child(self.supervisor.session, child_id)
        age = child.age_on(dt.date.today()) if child is not None else 7
        try:
            response = await self.client.call(
                "ask",
                question=question,
                age=age,
                locale=runtime.locale,
                persona="zio",
                name=runtime.display_name,
            )
        except IpcError as exc:
            log.warning("the assistant service is not answering: %s", exc)
            return refusal("assistant.refuse.unavailable", "unavailable")
        if not response.ok:
            return refusal("assistant.refuse.unavailable", "unavailable")

        reply = dict(response.data)
        if reply.get("alert"):
            self._report(runtime, child_id, question, reply)
        return reply

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

    async def hear(self, child_id: int, audio_b64: str) -> dict[str, Any]:
        """Press-and-speak: a short recording in, its words out. Nothing kept (D-166)."""
        runtime, policy = self._policy(child_id)
        if policy is None or not (policy.assistant_enabled and policy.assistant_voice):
            return {"ok": False, "error": "assistant.voice.off"}
        if len(audio_b64) > MAX_AUDIO_B64:
            return {"ok": False, "error": "assistant.voice.too_long"}
        try:
            response = await self.client.call(
                "transcribe", audio=audio_b64, locale=runtime.locale
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
