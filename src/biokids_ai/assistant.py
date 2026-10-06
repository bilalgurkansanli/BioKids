"""A child's question in, a safe answer or a kind refusal out (D-165).

    question
      │
      ├─ 1 lexicon      words and contact details that end it at once   ~1 ms
      ├─ 2 classifier   the model sorts it into one fixed category      ~1-3 s
      ├─ 3 answer       only an "ok" question is answered, at the age    ~3-15 s
      └─ 4 lexicon      the answer is screened before a child reads it   ~1 ms

Any stage that cannot run ends the question with "I'm resting, ask me again
later", never with an unchecked answer (principle 3): a model that cannot be
asked whether a question is safe is not asked the question either.

A refusal is a fixed sentence from the catalogs, never generated, and carries
the category the parent's alert names. Whether the child is told that a parent
may see the question is the launcher's job: it says so on the screen, always
(principle 2, D-165).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Final, Protocol

from biokids_ai import prompts
from biokids_ai.llm import ModelError
from biokids_ai.safety import Lexicon, fold, tokens

log = logging.getLogger(__name__)

MAX_QUESTION_CHARS: Final = 300
_CONTROL: Final = re.compile(r"[\x00-\x1f\x7f]")

#: Refusal sentence per category; anything not listed uses "default".
REFUSAL_KEYS: Final = {
    "self_harm": "assistant.refuse.self_harm",
    "personal_info": "assistant.refuse.personal_info",
    "rude": "assistant.refuse.rude",
    "family": "assistant.refuse.family",
    "unavailable": "assistant.refuse.unavailable",
    "default": "assistant.refuse.default",
}


#: Words a child greets or thanks with. A message made only of these is small
#: talk, answered without asking the classifier: the small model filed "selam
#: canım" under "ask your family" however plainly the prompt said otherwise,
#: and a child who says hello and is told to ask their parents learns that
#: the assistant does not want to talk to them. The word list has already run.
SMALL_TALK: Final = frozenset(
    fold(token)
    for word in [
        "merhaba",
        "selam",
        "selamlar",
        "canım",
        "canımsın",
        "tatlım",
        "nasılsın",
        "naber",
        "napıyorsun",
        "ne",
        "yapıyorsun",
        "günaydın",
        "iyi",
        "akşamlar",
        "geceler",
        "günler",
        "teşekkür",
        "teşekkürler",
        "teşekkür",
        "ederim",
        "sağ",
        "ol",
        "sağol",
        "eyvallah",
        "hoşça",
        "kal",
        "görüşürüz",
        "zio",
        "sio",
        "ben",
        "de",
        "çok",
        "iyiyim",
        "iyiyim",
        "hello",
        "hi",
        "hey",
        "thanks",
        "thank",
        "you",
        "good",
        "morning",
        "night",
        "how",
        "are",
        "bye",
        "see",
        "later",
        "fine",
    ]
    for token in tokens(word)
)


def is_small_talk(message: str) -> bool:
    words = [fold(word) for word in tokens(message)]
    return 0 < len(words) <= 6 and all(word in SMALL_TALK for word in words)


class Model(Protocol):
    async def classify(self, question: str, age: int, wait_s: float | None = None) -> str: ...

    async def answer(
        self,
        question: str,
        age: int,
        locale: str,
        persona: str,
        name: str = "",
        wait_s: float | None = None,
        context: str = "",
    ) -> str: ...


class Reference(Protocol):
    async def lookup(self, question: str, locale: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Reply:
    refused: bool
    text: str = ""
    """The answer, when not refused. A refusal's sentence comes from `refusal_key`."""
    category: str = "ok"
    stage: str = ""
    """`lexicon` | `classifier` | `answer` | `error`, for the log and the alert."""
    refusal_key: str = ""

    @property
    def alert(self) -> bool:
        """Should the parent hear about it? Not for "ask your family" or a tired model."""
        return self.refused and self.category not in prompts.QUIET_CATEGORIES | {"unavailable"}

    def as_dict(self) -> dict[str, object]:
        return {
            "refused": self.refused,
            "text": self.text,
            "category": self.category,
            "stage": self.stage,
            "refusal_key": self.refusal_key,
            "alert": self.alert,
        }


def refuse(category: str, stage: str) -> Reply:
    key = REFUSAL_KEYS.get(category, REFUSAL_KEYS["default"])
    return Reply(refused=True, category=category, stage=stage, refusal_key=key)


def tidy(question: str) -> str:
    return " ".join(_CONTROL.sub(" ", question).split())[:MAX_QUESTION_CHARS]


class Assistant:
    def __init__(
        self, model: Model, lexicon: Lexicon | None = None, reference: Reference | None = None
    ) -> None:
        self.model = model
        self.lexicon = lexicon or Lexicon.load()
        self.reference = reference
        """The encyclopedia on the machine (D-178), when the parent downloaded one."""

    async def ask(
        self, question: str, *, age: int, locale: str, persona: str = "zio", name: str = ""
    ) -> Reply:
        question = tidy(question)
        if not question:
            return Reply(refused=False, text="", stage="empty")

        hit = self.lexicon.screen(question)
        if hit is not None:
            log.info("question stopped by the word list (%s)", hit.category)
            return refuse(hit.category, "lexicon")

        if is_small_talk(question):
            category = "ok"
        else:
            try:
                category = await self.model.classify(question, age)
            except ModelError as exc:
                log.warning("the classifier could not run: %s", exc)
                return refuse("unavailable", "error")
        if category != "ok":
            log.info("question refused by the classifier (%s)", category)
            return refuse(category, "classifier")

        context = await self._look_up(question, locale)
        try:
            answer = await self.model.answer(
                question, age, locale, persona, name, context=context
            )
        except ModelError as exc:
            log.warning("the model could not answer: %s", exc)
            return refuse("unavailable", "error")
        if not answer:
            return refuse("unavailable", "error")

        hit = self.lexicon.screen(answer)
        if hit is not None:
            # The question was fine and the answer was not. Rare, and exactly
            # why this check exists; the parent hears about it.
            log.warning("an answer was stopped by the word list (%s)", hit.category)
            return refuse(hit.category, "answer")
        return Reply(refused=False, text=answer, stage="answer")

    async def _look_up(self, question: str, locale: str) -> str:
        """What the encyclopedia says, or "". Screened like everything else: a
        passage the word list stops is not handed to the model."""
        if self.reference is None:
            return ""
        try:
            found = await self.reference.lookup(question, locale)
        except Exception:  # a lookup is a help, never a reason to fail
            log.exception("the encyclopedia lookup failed")
            return ""
        if found and self.lexicon.screen(found) is not None:
            return ""
        return found
