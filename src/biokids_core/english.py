"""The English corner: what a child learns, and when they see it again (D-225).

The owner saw a child who had learned fluent English from a computer and asked
for the same here. What reaches fluency, in the research and in the homes that
manage it (D-225 has the sources), is a lot of English the child understands,
heard every day; short sessions that ask the child to *do* something with it -
point, answer, say it back - rather than watch; and words met again just as
they are about to be forgotten. Passive video teaches young children little;
being answered teaches them a lot.

So a lesson here is five to ten minutes of that, built fresh each day:

* **review** - the words that are due today, heard and found among pictures;
* **new words** - a few at a time from the unit the child is in, each met
  (seen, heard, said back) and then found;
* **lines** - once a unit's words are known, its short sentences, heard and
  said back whole: the chunks a child uses long before they could build them;
* **the story** - once the lines are too, five or six pictures read aloud, and
  a question or two answered by tapping a picture.

Every item - a word, a line, a unit's story - moves through Leitner boxes:
right the first time, it goes up a box and comes back later; wrong, it goes
back to the first box and comes back tomorrow. Box `MASTERED_BOX` and up is a
word the child knows. A wrong answer never costs anything else: the child hears
the word again and goes on.

Pure functions over plain data. The daemon stores the boxes (`english_item`),
the launcher draws the lesson, and the panel reads the summary; all three call
this module.
"""

from __future__ import annotations

import datetime as dt
import difflib
import functools
import random
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

DEFAULT_UNITS: Final = Path(__file__).resolve().parent / "data" / "english" / "units.toml"

SLUG: Final = "english"
"""The tile's slug in the catalogue, and the app its minutes are counted under."""

LANGUAGE: Final = "en"
"""What the voice speaks the lessons in."""

CHAT_MODE: Final = "english"
"""The assistant's conversation mode: English with Sio (D-226)."""

BOX_DAYS: Final = (0, 1, 2, 4, 7, 14, 30)
"""Days until an item comes back, by its box. Box 0 is an item never seen. The
gaps grow faster than a grown-up's flashcards would, but start shorter: a child
of six forgets a new word by the weekend."""
MAX_BOX: Final = len(BOX_DAYS) - 1
MASTERED_BOX: Final = 4
"""Right four times in a row, the last time after four days: the child knows it."""

REVIEW_MAX: Final = 6
"""Words to find again in one lesson, the most overdue first."""
LINES_REVIEW_MAX: Final = 2
NEW_PER_LESSON: Final = 4
"""New words in one lesson. Four is what a young child holds in one sitting."""
NEW_PER_DAY: Final = 8
"""New words a day, over every lesson: two lessons' worth, then only review."""
NEW_LINES_PER_LESSON: Final = 2
CHOICES: Final = 3
"""Pictures to choose from. Three, not four: a four-year-old taps, not reads."""

PRAISE: Final = ("Great!", "Well done!", "Super!", "Yes, that's right!", "Good job!")
"""Said in English after a right answer: praise is language too, and the most
heard phrase of the corner should be one worth knowing."""
GOODBYE: Final = "Great job! See you tomorrow!"

STORY: Final = "story"
LINE: Final = "line"


@dataclass(frozen=True, slots=True)
class Card:
    """Something to hear and see: a word, a line, or a line of a story."""

    en: str
    tr: str
    pic: str


@dataclass(frozen=True, slots=True)
class Question:
    en: str
    tr: str
    answer: str
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Unit:
    slug: str
    title_tr: str
    title_en: str
    icon: str
    words: tuple[Card, ...]
    lines: tuple[Card, ...]
    story: tuple[Card, ...]
    questions: tuple[Question, ...]

    def word_id(self, card: Card) -> str:
        return f"{self.slug}/{word_key(card.en)}"

    def line_id(self, index: int) -> str:
        return f"{self.slug}/{LINE}-{index + 1}"

    @property
    def story_id(self) -> str:
        return f"{self.slug}/{STORY}"

    def item_ids(self) -> list[str]:
        """Words, then lines, then the story: the order a child meets them."""
        return [
            *(self.word_id(card) for card in self.words),
            *(self.line_id(n) for n in range(len(self.lines))),
            *([self.story_id] if self.story else []),
        ]

    def title(self, locale: str) -> str:
        return self.title_tr if locale == "tr" else self.title_en


def word_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


@dataclass(frozen=True, slots=True)
class ItemState:
    """Where one item is for one child."""

    box: int = 0
    due: dt.date | None = None
    first_day: dt.date | None = None

    @property
    def met(self) -> bool:
        return self.box > 0


Progress = Mapping[str, ItemState]


# ------------------------------------------------------------------ content


def _card(raw: Mapping[str, object]) -> Card:
    return Card(en=str(raw["en"]), tr=str(raw["tr"]), pic=str(raw["pic"]))


def _question(raw: Mapping[str, object]) -> Question:
    listed = raw["choices"]
    if not isinstance(listed, list):
        raise ValueError(f"{raw['en']!r}: choices must be a list")
    choices = tuple(str(choice) for choice in listed)
    answer = str(raw["answer"])
    if answer not in choices:
        raise ValueError(f"{raw['en']!r}: the answer is not one of the choices")
    return Question(en=str(raw["en"]), tr=str(raw["tr"]), answer=answer, choices=choices)


def parse_units(text: str) -> tuple[Unit, ...]:
    data = tomllib.loads(text)
    units: list[Unit] = []
    for raw in data.get("unit", []):
        unit = Unit(
            slug=str(raw["slug"]),
            title_tr=str(raw["title_tr"]),
            title_en=str(raw["title_en"]),
            icon=str(raw["icon"]),
            words=tuple(_card(word) for word in raw.get("words", [])),
            lines=tuple(_card(line) for line in raw.get("lines", [])),
            story=tuple(_card(line) for line in raw.get("story", [])),
            questions=tuple(_question(q) for q in raw.get("questions", [])),
        )
        ids = unit.item_ids()
        if len(set(ids)) != len(ids):
            raise ValueError(f"{unit.slug}: two items with one id")
        units.append(unit)
    if len({unit.slug for unit in units}) != len(units):
        raise ValueError("two units with one slug")
    return tuple(units)


@functools.lru_cache(maxsize=4)
def load_units(path: Path = DEFAULT_UNITS) -> tuple[Unit, ...]:
    return parse_units(path.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=4)
def _item_index(units: tuple[Unit, ...]) -> dict[str, Unit]:
    return {item: unit for unit in units for item in unit.item_ids()}


def is_item(item_id: str, units: tuple[Unit, ...] | None = None) -> bool:
    """Whether an id names something a child can learn: the daemon stores no other."""
    return item_id in _item_index(units if units is not None else load_units())


def is_word(item_id: str) -> bool:
    """A word, not a line or a story: what "new words" counts."""
    unit, _, rest = item_id.partition("/")
    return bool(unit) and rest != STORY and not rest.startswith(f"{LINE}-")


def card_for(item_id: str, units: tuple[Unit, ...] | None = None) -> Card | None:
    """The word or line an id names; None for a story or an unknown id."""
    units = units if units is not None else load_units()
    unit = _item_index(units).get(item_id)
    if unit is None:
        return None
    for card in unit.words:
        if unit.word_id(card) == item_id:
            return card
    for index, card in enumerate(unit.lines):
        if unit.line_id(index) == item_id:
            return card
    return None


# ----------------------------------------------------------------- the boxes


def answered(state: ItemState | None, right: bool, today: dt.date) -> ItemState:
    """The item after one answer: up a box when right, back to the first when not.

    A new item goes to the first box whichever way: meeting a word is not
    knowing it, and getting it wrong the first time is not a failure.
    """
    state = state or ItemState()
    box = 1 if state.box == 0 or not right else min(state.box + 1, MAX_BOX)
    return ItemState(
        box=box,
        due=today + dt.timedelta(days=BOX_DAYS[box]),
        first_day=state.first_day or today,
    )


def is_due(state: ItemState, today: dt.date) -> bool:
    return state.met and (state.due is None or state.due <= today)


# ---------------------------------------------------------------- the lesson


@dataclass(frozen=True, slots=True)
class Step:
    """One screen of a lesson. `kind` is `meet`, `find`, `line` or `story`."""

    kind: str
    item: str
    unit: str
    card: Card | None = None
    choices: tuple[str, ...] = ()
    """For `find`: pictures to choose from, the right one among them."""
    story: tuple[Card, ...] = ()
    questions: tuple[Question, ...] = ()

    def as_json(self) -> dict[str, object]:
        out: dict[str, object] = {"kind": self.kind, "item": self.item, "unit": self.unit}
        if self.card is not None:
            out["card"] = {"en": self.card.en, "tr": self.card.tr, "pic": self.card.pic}
        if self.choices:
            out["choices"] = list(self.choices)
        if self.story:
            out["story"] = [{"en": c.en, "tr": c.tr, "pic": c.pic} for c in self.story]
            out["questions"] = [
                {"en": q.en, "tr": q.tr, "answer": q.answer, "choices": list(q.choices)}
                for q in self.questions
            ]
        return out


@dataclass(frozen=True, slots=True)
class Lesson:
    unit: Unit | None
    """The unit new things come from; None once every unit is done."""
    steps: tuple[Step, ...]
    new_words: int
    """How many of the steps meet a word for the first time."""


def current_unit(units: Sequence[Unit], progress: Progress) -> Unit | None:
    """The first unit with something the child has not met yet."""
    for unit in units:
        if any(not _state(progress, item).met for item in unit.item_ids()):
            return unit
    return None


def _state(progress: Progress, item: str) -> ItemState:
    return progress.get(item) or ItemState()


def _choices(
    answer: Card, unit: Unit, others: Sequence[Card], rng: random.Random
) -> tuple[str, ...]:
    """The right picture and two others, from the same unit when it has enough."""
    pool = [card.pic for card in unit.words if card.pic != answer.pic]
    if len(pool) < CHOICES - 1:
        pool += [card.pic for card in others if card.pic != answer.pic and card.pic not in pool]
    picked = rng.sample(pool, min(CHOICES - 1, len(pool)))
    shown = [answer.pic, *picked]
    rng.shuffle(shown)
    return tuple(shown)


def plan_lesson(
    units: Sequence[Unit],
    progress: Progress,
    today: dt.date,
    *,
    rng: random.Random | None = None,
) -> Lesson:
    """Today's lesson for one child: review first, then something new."""
    rng = rng or random.Random()  # noqa: S311 - which picture goes where, not a secret
    all_words = [card for unit in units for card in unit.words]
    words: dict[str, tuple[Unit, Card]] = {
        unit.word_id(card): (unit, card) for unit in units for card in unit.words
    }
    lines: dict[str, tuple[Unit, Card]] = {
        unit.line_id(n): (unit, card) for unit in units for n, card in enumerate(unit.lines)
    }

    def overdue(item: str) -> tuple[dt.date, int]:
        state = _state(progress, item)
        return (state.due or today, state.box)

    due_words = sorted((w for w in words if is_due(_state(progress, w), today)), key=overdue)
    due_lines = sorted((w for w in lines if is_due(_state(progress, w), today)), key=overdue)

    steps: list[Step] = []
    for item in due_words[:REVIEW_MAX]:
        home, card = words[item]
        steps.append(Step("find", item, home.slug, card, _choices(card, home, all_words, rng)))
    rng.shuffle(steps)
    for item in due_lines[:LINES_REVIEW_MAX]:
        home, card = lines[item]
        steps.append(Step("line", item, home.slug, card))

    unit = current_unit(units, progress)
    met_today = sum(
        1 for item, state in progress.items() if state.first_day == today and item in words
    )
    budget = max(0, min(NEW_PER_LESSON, NEW_PER_DAY - met_today))
    new_words = 0
    if unit is not None:
        fresh = [card for card in unit.words if not _state(progress, unit.word_id(card)).met]
        meeting = fresh[:budget]
        for card in meeting:
            steps.append(Step("meet", unit.word_id(card), unit.slug, card))
        finding = [
            Step(
                "find",
                unit.word_id(card),
                unit.slug,
                card,
                _choices(card, unit, all_words, rng),
            )
            for card in meeting
        ]
        rng.shuffle(finding)
        steps += finding
        new_words = len(meeting)
        if not fresh:
            # The unit's words are all met: its lines, then its story.
            new_lines = [
                n for n in range(len(unit.lines)) if not _state(progress, unit.line_id(n)).met
            ]
            for n in new_lines[:NEW_LINES_PER_LESSON]:
                steps.append(Step("line", unit.line_id(n), unit.slug, unit.lines[n]))
            if not new_lines and unit.story and not _state(progress, unit.story_id).met:
                steps.append(_story_step(unit))

    # A story whose time has come again, read with whatever else is due.
    for done in units:
        if done is not unit and done.story and is_due(_state(progress, done.story_id), today):
            steps.append(_story_step(done))
            break
    return Lesson(unit=unit, steps=tuple(steps), new_words=new_words)


def _story_step(unit: Unit) -> Step:
    return Step("story", unit.story_id, unit.slug, story=unit.story, questions=unit.questions)


def practice(
    units: Sequence[Unit],
    progress: Progress,
    *,
    rng: random.Random | None = None,
    count: int = 6,
) -> tuple[Step, ...]:
    """More, after today's lesson is done: words already met, found again.

    Answers to these move no box down: the child chose to play, and a word
    that is not due is not being tested.
    """
    rng = rng or random.Random()  # noqa: S311 - which picture goes where, not a secret
    all_words = [card for unit in units for card in unit.words]
    met = [
        (unit, card)
        for unit in units
        for card in unit.words
        if _state(progress, unit.word_id(card)).met
    ]
    picked = rng.sample(met, min(count, len(met)))
    return tuple(
        Step("find", unit.word_id(card), unit.slug, card, _choices(card, unit, all_words, rng))
        for unit, card in picked
    )


# ------------------------------------------------------------- what was said


_FILLER: Final = frozenset({"a", "an", "the", "it", "is", "its", "it's", "um", "uh", "er"})


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def heard_matches(heard: str, target: str) -> bool:
    """Whether what speech recognition heard is the word or line asked for.

    Generous on purpose: a child of five saying "a cat", "cat!" or "kat" said
    it. The words are compared without articles and fillers, a whole line by
    how much of it was heard, a single word also by how close it sounds as
    spelt; a wrong answer only means "listen once more".
    """
    want = [word for word in _words(target) if word not in _FILLER] or _words(target)
    got = [word for word in _words(heard) if word not in _FILLER] or _words(heard)
    if not want or not got:
        return False
    if len(want) == 1:
        word = want[0]
        return any(
            candidate == word
            or difflib.SequenceMatcher(None, candidate, word).ratio() >= 0.75
            or (len(word) >= 3 and _one_letter_apart(candidate, word))
            for candidate in got
        )
    matcher = difflib.SequenceMatcher(None, " ".join(want), " ".join(got))
    return matcher.ratio() >= 0.7


def _one_letter_apart(a: str, b: str) -> bool:
    """One letter changed, added or left out: "kat" for "cat", "dogs" for "dog"."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b, strict=True)) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1 :] == short for i in range(len(long_)))


# ---------------------------------------------------------------- the summary


@dataclass(frozen=True, slots=True)
class Summary:
    """What the parent reads in Analiz."""

    words_met: int
    words_known: int
    words_total: int
    units_done: int
    units_total: int
    current: Unit | None
    new_in_period: int
    known_words: tuple[Card, ...] = field(default_factory=tuple)
    """The words the child knows, in the order they were met."""


def summarize(
    units: Sequence[Unit],
    progress: Progress,
    since: dt.date | None = None,
    until: dt.date | None = None,
) -> Summary:
    words = [(unit.word_id(card), card) for unit in units for card in unit.words]
    known = [card for item, card in words if _state(progress, item).box >= MASTERED_BOX]

    def in_period(state: ItemState) -> bool:
        day = state.first_day
        return (
            day is not None
            and (since is None or day >= since)
            and (until is None or day <= until)
        )

    return Summary(
        words_met=sum(1 for item, _card in words if _state(progress, item).met),
        words_known=len(known),
        words_total=len(words),
        units_done=sum(
            1 for unit in units if all(_state(progress, item).met for item in unit.item_ids())
        ),
        units_total=len(units),
        current=current_unit(units, progress),
        new_in_period=sum(1 for item, _card in words if in_period(_state(progress, item))),
        known_words=tuple(known),
    )


def word_of_the_day(
    units: Sequence[Unit], progress: Progress, today: dt.date, salt: int = 0
) -> Card | None:
    """One word for the corner's door: the same all day, a different one tomorrow.

    From the words the child has met, so it is one they can be proud of; before
    the first lesson, the first word of the first unit.
    """
    met = [
        card
        for unit in units
        for card in unit.words
        if _state(progress, unit.word_id(card)).met
    ]
    if not met:
        return units[0].words[0] if units and units[0].words else None
    return met[(today.toordinal() * 7919 + salt) % len(met)]


def states(
    rows: Iterable[tuple[str, int, dt.date | None, dt.date | None]],
) -> dict[str, ItemState]:
    """(item, box, due, first_day) rows, as the daemon stores them, as a progress map."""
    return {item: ItemState(box, due, first) for item, box, due, first in rows}


# ------------------------------------------------- talking with Sio (D-226)

CHAT_QUESTIONS: Final = {
    "animals": "What is your favourite animal?",
    "colours": "What is your favourite colour?",
    "numbers": "How old are you?",
    "family": "Who is in your family?",
    "food": "What do you like to eat?",
    "body": "Can you touch your nose?",
    "clothes": "What are you wearing today?",
    "home": "What is in your room?",
    "nature": "What is the weather like today?",
    "actions": "What do you like to do?",
    "feelings": "How are you today?",
    "transport": "Do you like cars or trains?",
}
"""Sio's first question, about the unit the child is learning: a conversation
starts where the words are fresh."""

CHAT_ALWAYS: Final = (("👍", "Yes"), ("👎", "No"), ("🤷", "I don't know"), ("👋", "Hello!"))
"""Answers a child can tap before they can say them."""
CHAT_WORDS: Final = 6


def chat_opening(unit: Unit | None, name: str) -> str:
    """What Sio says first, in English: hello, who he is, and one easy question."""
    question = CHAT_QUESTIONS.get(unit.slug if unit else "", "How are you today?")
    hello = f"Hello, {name.strip()}!" if name.strip() else "Hello!"
    return f"{hello} I'm Sio. Let's talk in English! {question}"


def chat_chips(units: Sequence[Unit], progress: Progress) -> list[tuple[str, str]]:
    """Words to tap as an answer: the ones met most recently in the unit the
    child is in (or the last one finished), then the answers that always fit."""
    unit = current_unit(units, progress)
    pool = [unit_ for unit_ in units if any(_state(progress, w).met for w in unit_.item_ids())]
    if unit is not None and unit not in pool:
        pool.append(unit)
    met: list[tuple[str, str]] = []
    for source in reversed(pool):
        for card in source.words:
            if _state(progress, source.word_id(card)).met:
                met.append((card.pic, card.en))
        if met:
            break
    return [*met[:CHAT_WORDS], *CHAT_ALWAYS]


def known_words(units: Sequence[Unit], progress: Progress) -> list[str]:
    """Every word the child has met, for Sio to use (`PRACTICE_WORDS` of them)."""
    return [
        card.en
        for unit in units
        for card in unit.words
        if _state(progress, unit.word_id(card)).met
    ]
