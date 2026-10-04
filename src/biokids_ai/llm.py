"""The chat model, behind llama-server's OpenAI-style endpoint (D-165)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Final

from biokids_ai import prompts
from biokids_ai.localhttp import Endpoint, HttpError, post_json

log = logging.getLogger(__name__)

CHAT_PATH: Final = "/v1/chat/completions"
CLASSIFY_TIMEOUT_S: Final = 60.0
ANSWER_TIMEOUT_S: Final = 120.0
WARMUP_TIMEOUT_S: Final = 600.0
"""Loading the model and reading both prompts for the first time. Nobody is
waiting for it - a question waits for the warm-up instead - and cut short at
the classifier's minute it left the next question to start the work again, and
time out in turn: on the development VM every first question failed."""

Poster = Callable[..., Awaitable[dict[str, Any]]]


class ModelError(Exception):
    """The model could not be asked, or its answer could not be used."""


class ChatModel:
    def __init__(self, endpoint: Endpoint, *, post: Poster = post_json) -> None:
        self.endpoint = endpoint
        self._post = post

    async def _complete(self, payload: dict[str, Any], wait_s: float) -> str:
        # Qwen 3.5 thinks aloud unless told not to; a template that has no such
        # switch ignores it. Thinking would be slow, and a child would wait for it.
        payload.setdefault("chat_template_kwargs", {"enable_thinking": False})
        try:
            data = await self._post(self.endpoint, CHAT_PATH, payload, wait_s=wait_s)
        except HttpError as exc:
            raise ModelError(str(exc)) from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelError("the model's answer had no message") from exc
        if not isinstance(content, str):
            raise ModelError("the model's answer was not text")
        return content.strip()

    async def classify(self, question: str, age: int, wait_s: float | None = None) -> str:
        """One category from `prompts.CATEGORIES`, never anything else."""
        content = await self._complete(
            {
                "messages": prompts.classify_messages(question, age),
                "temperature": 0,
                "max_tokens": 24,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "verdict", "schema": prompts.CLASSIFY_SCHEMA},
                },
            },
            wait_s or CLASSIFY_TIMEOUT_S,
        )
        try:
            category = json.loads(content)["category"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ModelError(
                f"the classifier did not return a category: {content[:80]!r}"
            ) from exc
        if category not in prompts.CATEGORIES:
            raise ModelError(f"the classifier returned {category!r}")
        return str(category)

    async def answer(
        self,
        question: str,
        age: int,
        locale: str,
        persona: str,
        name: str = "",
        wait_s: float | None = None,
    ) -> str:
        band = prompts.band_for(age)
        return await self._complete(
            {
                "messages": prompts.answer_messages(question, age, locale, persona, name),
                "temperature": 0.2,
                "top_p": 0.9,
                "max_tokens": prompts.MAX_TOKENS[band],
            },
            wait_s or ANSWER_TIMEOUT_S,
        )
