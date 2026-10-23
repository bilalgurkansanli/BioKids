"""Okuma köşesi: learning to read Turkish, the way first grade teaches it (D-241).

Turkish children learn to read with the sound-based method (ses temelli cümle
yöntemi): the sounds in five groups - e l a k i n, o m u t ü y, ö r ı d s b,
z ç g ş c p, h ğ v f j - each letter met and heard, then syllables made only
of the letters met so far, then words, then a short sentence. A child at home
at five or six meets the same order their teacher will use, and a child in
first grade practises what the class is on.

A lesson is a few minutes of that, with nothing to type:

* **letter** - the letter big, capital and small, a picture of a word with its
  sound, and Zio making the sound in syllables ("la, le, li");
* **sound** - Zio says a syllable, the child taps the letter it starts with;
* **syllable** - Zio says a syllable, the child taps it written among three;
* **word** - a word written, split as the class splits it ("an-ne"); the child
  reads it and taps its picture - reading, not listening, because nobody says
  it until the answer is in;
* **sentence** - a short sentence the same way.

Every item moves through the English corner's boxes (`english.answered`): right
the first time, it comes back later; wrong, it comes back tomorrow, and the
child simply hears it and tries again. Nothing here ever shows a letter the
child has not met: `parse_groups` refuses a file that would.

Pure functions over plain data, like `english.py`: the daemon keeps the boxes
(`reading_item`), the launcher draws the lesson, the panel reads the summary.
"""

from __future__ import annotations

import datetime as dt
import functools
import random
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from biokids_core.english import ItemState, answered, is_due

__all__ = [
    "ItemState",
    "answered",
    "is_due",
]

DEFAULT_GROUPS: Final = Path(__file__).resolve().parent / "data" / "reading" / "groups.toml"

SLUG: Final = "okuma"
"""The tile's slug, and the app its minutes are counted under."""

TURKISH_LETTERS: Final = frozenset("abcçdefgğhıijklmnoöprsştuüvyz")
PUNCTUATION: Final = frozenset(" .,!?'-")

MASTERED_BOX: Final = 4
REVIEW_MAX: Final = 6
NEW_LETTERS_PER_LESSON: Final = 2
NEW_SYLLABLES_PER_LESSON: Final = 3
NEW_WORDS_PER_LESSON: Final = 3
NEW_PER_DAY: Final = 10
CHOICES: Final = 3

PRAISE: Final = ("Aferin!", "Harika!", "Çok güzel!", "Bravo!", "Süpersin!")
GOODBYE: Final = "Bugünlük bu kadar. Çok iyi okudun!"


@dataclass(frozen=True, slots=True)
class Letter:
    char: str
    hint: str
    word: str
    pic: str


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    syllables: str
    pic: str


@dataclass(frozen=True, slots=True)
class Sentence:
    text: str
    pic: str


@dataclass(frozen=True, slots=True)
class Group:
    slug: str
    title: str
    letters: tuple[Letter, ...]
    syllables: tuple[str, ...]
    words: tuple[Word, ...]
    sentences: tuple[Sentence, ...]

    def letter_id(self, letter: Letter) -> str:
        return f"{self.slug}/letter-{letter.char}"

    def syllable_id(self, syllable: str) -> str:
        return f"{self.slug}/syl-{syllable}"

    def word_id(self, word: Word) -> str:
        return f"{self.slug}/word-{word.text}"

    def sentence_id(self, index: int) -> str:
        return f"{self.slug}/sentence-{index + 1}"

    def item_ids(self) -> list[str]:
        """In the order a child meets them: letters, syllables, words, sentences."""
        return [
            *(self.letter_id(letter) for letter in self.letters),
            *(self.syllable_id(s) for s in self.syllables),
            *(self.word_id(word) for word in self.words),
            *(self.sentence_id(n) for n in range(len(self.sentences))),
        ]


def upper(text: str) -> str:
    """Turkish capitals: i is İ and ı is I, which `str.upper` gets wrong."""
    return text.replace("i", "İ").replace("ı", "I").upper()


def lower(text: str) -> str:
    return text.replace("İ", "i").replace("I", "ı").lower()


Progress = Mapping[str, ItemState]


# ------------------------------------------------------------------ content


def _letters_of(text: str) -> set[str]:
    return {ch for ch in lower(text) if ch not in PUNCTUATION}


def parse_groups(text: str) -> tuple[Group, ...]:
    """The groups, checked: every syllable, word and sentence uses only letters
    met in its own group or an earlier one, and every word has a picture."""
    data = tomllib.loads(text)
    groups: list[Group] = []
    known: set[str] = set()
    for raw in data.get("group", []):
        letters = tuple(
            Letter(
                char=str(entry["char"]),
                hint=str(entry["hint"]),
                word=str(entry["word"]),
                pic=str(entry["pic"]),
            )
            for entry in raw.get("letter", [])
        )
        group = Group(
            slug=str(raw["slug"]),
            title=str(raw["title"]),
            letters=letters,
            syllables=tuple(str(s) for s in raw.get("syllables", [])),
            words=tuple(
                Word(text=str(w["text"]), syllables=str(w["syllables"]), pic=str(w["pic"]))
                for w in raw.get("word", [])
            ),
            sentences=tuple(
                Sentence(text=str(s["text"]), pic=str(s["pic"]))
                for s in raw.get("sentence", [])
            ),
        )
        listed = [str(ch) for ch in raw.get("letters", [])]
        if [letter.char for letter in letters] != listed:
            raise ValueError(f"{group.slug}: the letters and their cards differ")
        for letter in letters:
            if letter.char not in TURKISH_LETTERS:
                raise ValueError(f"{group.slug}: {letter.char!r} is not a Turkish letter")
        known |= set(listed)
        for what in (
            *group.syllables,
            *(w.text for w in group.words),
            *(s.text for s in group.sentences),
        ):
            unmet = _letters_of(what) - known
            if unmet:
                raise ValueError(
                    f"{group.slug}: {what!r} uses {''.join(sorted(unmet))} before it is met"
                )
        for letter in letters:
            unmet = _letters_of(letter.word) - known
            if unmet:
                raise ValueError(f"{group.slug}: {letter.word!r} uses {''.join(sorted(unmet))}")
        for word in group.words:
            if word.syllables.replace("-", "") != word.text:
                raise ValueError(
                    f"{group.slug}: {word.text!r} is not split as {word.syllables!r}"
                )
            if not word.pic:
                raise ValueError(f"{group.slug}: {word.text!r} has no picture")
        pics = [word.pic for word in group.words]
        if len(set(pics)) != len(pics):
            raise ValueError(f"{group.slug}: two words share a picture")
        ids = group.item_ids()
        if len(set(ids)) != len(ids):
            raise ValueError(f"{group.slug}: two items with one id")
        groups.append(group)
    if len({group.slug for group in groups}) != len(groups):
        raise ValueError("two groups with one slug")
    return tuple(groups)


@functools.lru_cache(maxsize=4)
def load_groups(path: Path = DEFAULT_GROUPS) -> tuple[Group, ...]:
    return parse_groups(path.read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=4)
def _item_index(groups: tuple[Group, ...]) -> dict[str, Group]:
    return {item: group for group in groups for item in group.item_ids()}


def is_item(item_id: str, groups: tuple[Group, ...] | None = None) -> bool:
    return item_id in _item_index(groups if groups is not None else load_groups())


def _state(progress: Progress, item: str) -> ItemState:
    return progress.get(item) or ItemState()


def current_group(groups: Sequence[Group], progress: Progress) -> Group | None:
    for group in groups:
        if any(not _state(progress, item).met for item in group.item_ids()):
            return group
    return None


def letters_met(groups: Sequence[Group], progress: Progress) -> list[str]:
    return [
        letter.char
        for group in groups
        for letter in group.letters
        if _state(progress, group.letter_id(letter)).met
    ]


# ---------------------------------------------------------------- the lesson


@dataclass(frozen=True, slots=True)
class Step:
    """One screen. `kind` is letter, sound, syllable, word or sentence."""

    kind: str
    item: str
    group: str
    show: str = ""
    """What is written on the screen: the letter, the word, the sentence."""
    say: str = ""
    """What Zio says: the letter's syllables, the syllable asked for, or - after
    the answer - the word or the sentence read aloud."""
    pic: str = ""
    syllables: str = ""
    answer: str = ""
    choices: tuple[str, ...] = ()

    def as_json(self) -> dict[str, object]:
        out: dict[str, object] = {"kind": self.kind, "item": self.item, "group": self.group}
        for name in ("show", "say", "pic", "syllables", "answer"):
            value = getattr(self, name)
            if value:
                out[name] = value
        if self.kind == "letter":
            out["upper"] = upper(self.show)
        if self.choices:
            out["choices"] = list(self.choices)
        return out


@dataclass(frozen=True, slots=True)
class Lesson:
    group: Group | None
    steps: tuple[Step, ...]
    new_items: int


def _pick(right: str, pool: Iterable[str], rng: random.Random) -> tuple[str, ...]:
    others = [p for p in dict.fromkeys(pool) if p != right]
    shown = [right, *rng.sample(others, min(CHOICES - 1, len(others)))]
    rng.shuffle(shown)
    return tuple(shown)


def _step(
    group: Group,
    item: str,
    groups: Sequence[Group],
    known: set[str],
    rng: random.Random,
    *,
    meeting: bool = False,
) -> Step | None:
    """The step that asks about `item`; a letter met for the first time is shown.

    `known` is every letter the child has met, with those being met in this
    lesson: the only letters any choice may show (D-241).
    """
    for letter in group.letters:
        if group.letter_id(letter) == item:
            if meeting:
                # The word is written only when every letter of it is known;
                # otherwise the picture alone, and Zio says the word.
                written = letter.word if _letters_of(letter.word) <= known else ""
                return Step(
                    "letter",
                    item,
                    group.slug,
                    show=letter.char,
                    say=f"{letter.hint}. {letter.word}",
                    pic=letter.pic,
                    syllables=written,
                )
            return Step(
                "sound",
                item,
                group.slug,
                say=_sound(letter),
                answer=letter.char,
                choices=_pick(letter.char, sorted(known), rng),
            )
    for syllable in group.syllables:
        if group.syllable_id(syllable) == item:
            pool = [s for g in groups for s in g.syllables if _letters_of(s) <= known]
            return Step(
                "syllable",
                item,
                group.slug,
                say=syllable,
                answer=syllable,
                choices=_pick(syllable, pool, rng),
            )
    readable = [w.pic for g in groups for w in g.words if _letters_of(w.text) <= known]
    for word in group.words:
        if group.word_id(word) == item:
            return Step(
                "word",
                item,
                group.slug,
                show=word.text,
                say=word.text,
                pic=word.pic,
                syllables=word.syllables,
                answer=word.pic,
                choices=_pick(word.pic, readable, rng),
            )
    for index, sentence in enumerate(group.sentences):
        if group.sentence_id(index) == item:
            return Step(
                "sentence",
                item,
                group.slug,
                show=sentence.text,
                say=sentence.text,
                pic=sentence.pic,
                answer=sentence.pic,
                choices=_pick(sentence.pic, readable, rng),
            )
    return None


def _sound(letter: Letter) -> str:
    """What Zio says for "which letter does it start with?": a vowel alone, a
    consonant in the first of its syllables that begins with it ("la" for l)."""
    for part in (piece.strip() for piece in letter.hint.split(",")):
        if part.startswith(letter.char):
            return part
    return letter.char


def plan_lesson(
    groups: Sequence[Group],
    progress: Progress,
    today: dt.date,
    *,
    rng: random.Random | None = None,
) -> Lesson:
    """Today's lesson: what is due again first, then a little that is new."""
    rng = rng or random.Random()  # noqa: S311 - where a picture goes, not a secret
    index = {item: group for group in groups for item in group.item_ids()}
    due = sorted(
        (item for item in index if is_due(_state(progress, item), today)),
        key=lambda item: (_state(progress, item).due or today, _state(progress, item).box),
    )
    met = set(letters_met(groups, progress))
    steps: list[Step] = []
    for item in due[:REVIEW_MAX]:
        step = _step(index[item], item, groups, met, rng)
        if step is not None:
            steps.append(step)
    rng.shuffle(steps)

    group = current_group(groups, progress)
    met_today = sum(1 for state in progress.values() if state.first_day == today)
    budget = max(0, NEW_PER_DAY - met_today)
    new_items = 0
    if group is not None and budget:
        fresh_letters = [
            letter
            for letter in group.letters
            if not _state(progress, group.letter_id(letter)).met
        ][: min(NEW_LETTERS_PER_LESSON, budget)]
        if fresh_letters:
            # Each letter met, then asked for straight away.
            known = met | {letter.char for letter in fresh_letters}
            for letter in fresh_letters:
                shown = _step(group, group.letter_id(letter), groups, known, rng, meeting=True)
                if shown is not None:
                    steps.append(shown)
            for letter in fresh_letters:
                asked = _step(group, group.letter_id(letter), groups, known, rng)
                if asked is not None:
                    steps.append(asked)
            new_items = len(fresh_letters)
        else:
            for kind_ids, most in (
                ([group.syllable_id(s) for s in group.syllables], NEW_SYLLABLES_PER_LESSON),
                ([group.word_id(w) for w in group.words], NEW_WORDS_PER_LESSON),
                ([group.sentence_id(n) for n in range(len(group.sentences))], 1),
            ):
                fresh = [item for item in kind_ids if not _state(progress, item).met]
                if fresh:
                    for item in fresh[: min(most, budget)]:
                        step = _step(group, item, groups, met, rng)
                        if step is not None:
                            steps.append(step)
                            new_items += 1
                    break
    return Lesson(group=group, steps=tuple(steps), new_items=new_items)


def has_read_words(groups: Sequence[Group], progress: Progress) -> bool:
    """Whether the child has read any word yet: "read more" needs one."""
    return any(_state(progress, g.word_id(w)).met for g in groups for w in g.words)


def practice(
    groups: Sequence[Group],
    progress: Progress,
    *,
    rng: random.Random | None = None,
    count: int = 5,
) -> tuple[Step, ...]:
    """More, after today's lesson: words already read, read again. No box moves:
    the child chose to play, and a word that is not due is not being tested."""
    rng = rng or random.Random()  # noqa: S311 - which word comes next, not a secret
    met = set(letters_met(groups, progress))
    read = [
        (group, word)
        for group in groups
        for word in group.words
        if _state(progress, group.word_id(word)).met
    ]
    picked = rng.sample(read, min(count, len(read)))
    steps = [_step(group, group.word_id(word), groups, met, rng) for group, word in picked]
    return tuple(step for step in steps if step is not None)


# ---------------------------------------------------------------- the summary


@dataclass(frozen=True, slots=True)
class Summary:
    letters_met: int
    letters_total: int
    words_read: int
    words_total: int
    groups_done: int
    groups_total: int
    current: Group | None
    letters_known: tuple[str, ...]


def summarize(groups: Sequence[Group], progress: Progress) -> Summary:
    letters = [(g.letter_id(letter), letter.char) for g in groups for letter in g.letters]
    words = [g.word_id(w) for g in groups for w in g.words]
    return Summary(
        letters_met=sum(1 for item, _ in letters if _state(progress, item).met),
        letters_total=len(letters),
        words_read=sum(1 for item in words if _state(progress, item).box >= 2),
        words_total=len(words),
        groups_done=sum(
            1 for g in groups if all(_state(progress, item).met for item in g.item_ids())
        ),
        groups_total=len(groups),
        current=current_group(groups, progress),
        letters_known=tuple(
            char for item, char in letters if _state(progress, item).box >= MASTERED_BOX
        ),
    )


def states(
    rows: Iterable[tuple[str, int, dt.date | None, dt.date | None]],
) -> dict[str, ItemState]:
    return {item: ItemState(box, due, first) for item, box, due, first in rows}
