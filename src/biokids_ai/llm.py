"""The chat model, behind llama-server's OpenAI-style endpoint (D-165)."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any, Final

from biokids_ai import prompts
from biokids_ai.localhttp import Endpoint, HttpError, post_events, post_json

log = logging.getLogger(__name__)

CHAT_PATH: Final = "/v1/chat/completions"
CLASSIFY_TIMEOUT_S: Final = 60.0
ANSWER_TIMEOUT_S: Final = 120.0
"""For the whole answer, streamed or not: the first word to the last (D-216)."""
WARMUP_TIMEOUT_S: Final = 600.0
"""Loading the model and reading both prompts for the first time. Nobody is
waiting for it - a question waits for the warm-up instead - and cut short at
the classifier's minute it left the next question to start the work again, and
time out in turn: on the development VM every first question failed."""

Poster = Callable[..., Awaitable[dict[str, Any]]]
Streamer = Callable[..., AsyncGenerator[dict[str, Any]]]


class ModelError(Exception):
    """The model could not be asked, or its answer could not be used."""


class ChatModel:
    def __init__(
        self, endpoint: Endpoint, *, post: Poster = post_json, stream: Streamer = post_events
    ) -> None:
        self.endpoint = endpoint
        self._post = post
        self._stream = stream

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
        context: str = "",
    ) -> str:
        """The whole answer at once. Only the warm-up still asks this way."""
        return await self._complete(
            _answer_payload(question, age, locale, persona, name, context),
            wait_s or ANSWER_TIMEOUT_S,
        )

    async def stream_answer(
        self,
        question: str,
        age: int,
        locale: str,
        persona: str,
        name: str = "",
        wait_s: float | None = None,
        context: str = "",
    ) -> AsyncIterator[str]:
        """The answer as the model writes it, a piece at a time (D-216).

        Raises ModelError when the model cannot be asked, takes longer than
        `wait_s` for the whole answer, or stops before it says it has finished:
        an answer that broke off is not an answer that ended. Closing this
        early closes the connection, and llama-server stops writing.
        """
        payload = _answer_payload(question, age, locale, persona, name, context)
        payload["stream"] = True
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        events = self._stream(
            self.endpoint, CHAT_PATH, payload, wait_s=wait_s or ANSWER_TIMEOUT_S
        )
        finished = False
        try:
            async with contextlib.aclosing(events):
                async for event in events:
                    choice = _first_choice(event)
                    if choice is None:
                        continue
                    delta = choice.get("delta")
                    content = delta.get("content") if isinstance(delta, dict) else None
                    if isinstance(content, str) and content:
                        yield content
                    if choice.get("finish_reason"):
                        finished = True
        except HttpError as exc:
            raise ModelError(str(exc)) from exc
        if not finished:
            raise ModelError("the answer broke off before the model had finished it")


def _answer_payload(
    question: str, age: int, locale: str, persona: str, name: str, context: str
) -> dict[str, Any]:
    band = prompts.band_for(age)
    return {
        "messages": prompts.answer_messages(question, age, locale, persona, name, context),
        "temperature": 0.2,
        "top_p": 0.9,
        "max_tokens": prompts.MAX_TOKENS[band],
    }


def _first_choice(event: dict[str, Any]) -> dict[str, Any] | None:
    """The one choice an event carries; the last event may carry none, only usage."""
    choices = event.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return None
