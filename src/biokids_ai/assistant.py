"""A child's question in, a safe answer or a kind refusal out (D-165).

    question
      │
      ├─ 1 lexicon      words and contact details that end it at once   ~1 ms
      ├─ 2 classifier   the model sorts it into one fixed category      ~1-3 s
      ├─ 3 answer       only an "ok" question is answered, at the age,  ~3-15 s
      │                 a sentence at a time as the model writes it
      └─ 4 lexicon      each sentence is screened before a child reads it ~1 ms

Any stage that cannot run ends the question with "I'm resting, ask me again
later", never with an unchecked answer (principle 3): a model that cannot be
asked whether a question is safe is not asked the question either.

The answer is released sentence by sentence (D-216): the model writes three
tokens a second on a modest machine, and a child should not wait half a minute
for the first word. Steps 1 and 2 still run before anything is shown, and
step 4 now runs before each sentence, over everything released so far. A
sentence it stops is not shown, nothing after it is either, and the answer
ends with a sentence from the catalogs.

A refusal is a fixed sentence from the catalogs, never generated, and carries
the category the parent's alert names. Whether the child is told that a parent
may see the question is the launcher's job: it says so on the screen, always
(principle 2, D-165).
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Final, Protocol

from biokids_ai import prompts
from biokids_ai.llm import ModelError
from biokids_ai.safety import Lexicon, fold, tokens
from biokids_ai.sentences import SentenceSplitter

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
CUT_KEY: Final = "assistant.refuse.cut"
"""How an answer ends when the word list stopped it after some of it was shown
(D-216): that is as much as Zio can tell, and a grown-up can tell the rest."""
OWN_ENDING: Final = frozenset({"self_harm", "unavailable"})
"""Categories that keep their own sentence even part-way through an answer:
self-harm's gives 183 and 112, and "I'm resting" says why the answer stopped."""


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

    def stream_answer(
        self,
        question: str,
        age: int,
        locale: str,
        persona: str,
        name: str = "",
        wait_s: float | None = None,
        context: str = "",
    ) -> AsyncIterator[str]: ...


class Reference(Protocol):
    async def lookup(self, question: str, locale: str) -> str: ...


@dataclass(frozen=True, slots=True)
class Reply:
    refused: bool
    text: str = ""
    """What the child was shown of the answer, its sentences joined. Empty for a
    refusal before anything was shown, whose sentence comes from `refusal_key`."""
    category: str = "ok"
    stage: str = ""
    """`lexicon` | `classifier` | `answer` | `error`, for the log and the alert."""
    refusal_key: str = ""
    """The catalog sentence the answer ends with, or "" when it ended by itself."""
    sentences: tuple[str, ...] = ()
    """Every sentence released, in order, each one screened before it was (D-216)."""

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
            "sentences": list(self.sentences),
        }


def refuse(category: str, stage: str) -> Reply:
    key = REFUSAL_KEYS.get(category, REFUSAL_KEYS["default"])
    return Reply(refused=True, category=category, stage=stage, refusal_key=key)


def cut(category: str, stage: str, released: Sequence[str]) -> Reply:
    """An answer stopped part-way: by the word list, a model that died, or time (D-216).

    Nothing shown yet, and it is the refusal it always was. Something shown,
    and that stays - every sentence of it was screened - and the catalog's
    ending follows it instead of the category's refusal, which would answer a
    question the child did not ask. Self-harm and "I'm resting" keep their own
    sentences wherever they fall (`OWN_ENDING`).
    """
    if not released or category in OWN_ENDING:
        reply = refuse(category, stage)
    else:
        reply = Reply(refused=True, category=category, stage=stage, refusal_key=CUT_KEY)
    return replace(reply, text=" ".join(released), sentences=tuple(released))


def answered(released: Sequence[str]) -> Reply:
    return Reply(
        refused=False, text=" ".join(released), stage="answer", sentences=tuple(released)
    )


SentenceSink = Callable[[str], object]
"""Called with each sentence as it is released, in order (D-216)."""


def tidy(question: str) -> str:
    return " ".join(_CONTROL.sub(" ", question).split())[:MAX_QUESTION_CHARS]


def _ignore(_sentence: str) -> None:
    return None


class Assistant:
    def __init__(
        self, model: Model, lexicon: Lexicon | None = None, reference: Reference | None = None
    ) -> None:
        self.model = model
        self.lexicon = lexicon or Lexicon.load()
        self.reference = reference
        """The encyclopedia on the machine (D-178), when the parent downloaded one."""

    async def ask(
        self,
        question: str,
        *,
        age: int,
        locale: str,
        persona: str = "zio",
        name: str = "",
        on_answering: Callable[[], object] | None = None,
        on_sentence: SentenceSink | None = None,
    ) -> Reply:
        """Check `question`, then answer it a sentence at a time.

        `on_answering` is called once both checks have passed and the model is
        about to write; `on_sentence` with each sentence as it is released. The
        returned Reply is how the answer ended, with every sentence released.
        """
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

        if on_answering is not None:
            on_answering()
        context = await self._look_up(question, locale)
        return await self._answer(
            question, age, locale, persona, name, context, on_sentence or _ignore
        )

    async def _answer(
        self,
        question: str,
        age: int,
        locale: str,
        persona: str,
        name: str,
        context: str,
        on_sentence: SentenceSink,
    ) -> Reply:
        """The model's answer, released one screened sentence at a time (D-216)."""
        released: list[str] = []
        splitter = SentenceSplitter()
        pieces = self.model.stream_answer(question, age, locale, persona, name, context=context)
        try:
            async for piece in pieces:
                stopped = self._release(splitter.feed(piece), released, on_sentence)
                if stopped is not None:
                    return stopped
            stopped = self._release(splitter.close(), released, on_sentence)
            if stopped is not None:
                return stopped
        except ModelError as exc:
            log.warning(
                "the model stopped answering after %d sentences: %s", len(released), exc
            )
            return cut("unavailable", "error", released)
        finally:
            # Stopping early must stop the model too, not leave it writing on.
            close = getattr(pieces, "aclose", None)
            if close is not None:
                await close()
        if not released:
            return refuse("unavailable", "error")
        return answered(released)

    def _release(
        self, candidates: list[str], released: list[str], on_sentence: SentenceSink
    ) -> Reply | None:
        """Show each candidate the word list allows; at the first it stops, stop.

        A sentence is screened together with every sentence before it, so a
        phrase or a squashed spelling that runs across two of them is found
        exactly as it was when the whole answer was screened at once.
        """
        for sentence in candidates:
            hit = self.lexicon.screen(" ".join([*released, sentence]))
            if hit is not None:
                # The question was fine and the answer was not. Rare, and exactly
                # why this check exists; the parent hears about it.
                log.warning(
                    "an answer was stopped by the word list (%s) after %d sentences",
                    hit.category,
                    len(released),
                )
                return cut(hit.category, "answer", released)
            released.append(sentence)
            on_sentence(sentence)
        return None

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
