"""The words that end a question, or an answer, before a model is trusted with it.

D-165. The assistant answers a child's typed or spoken question with a local
language model, and a model is not a safety boundary: it can be talked round,
and it can be wrong. So the first and the last thing that happens to every
question and every answer is this - deterministic, instant, testable, and
written by people. The model classifier between them handles what a word list
cannot (`docs/AI-LAYER.md` section 11).

A list that refuses "Yavru kedi meme emer mi?" teaches a child the assistant is
broken and teaches a parent to switch it off, so the lexicon is short and every
term in it is unambiguous; see the comments in `data/lexicon.toml`.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Final

LEXICON_RESOURCE: Final = "lexicon.toml"

#: Turkish letters to their ASCII neighbours, so "sikiş" and "sikis" are one word.
FOLD: Final = str.maketrans(
    {"ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u", "â": "a", "î": "i", "û": "u"}
)
#: Digits and symbols used as letters: "p0rn0", "$ex". Applied only inside a
#: word that also has letters, so "15" stays a number.
LEET: Final = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "€": "e"}
)
_CHUNK: Final = re.compile(r"[\w@$€]+", re.UNICODE)
_LETTERS: Final = re.compile(r"[^\W\d_]+", re.UNICODE)
_RUN: Final = re.compile(r"(.)\1+")

#: Things a child should not be typing to anyone, assistant included.
_PHONE: Final = re.compile(
    r"(?:\+?90[\s.-]?)?\(?0?5\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}"
)
_EMAIL: Final = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.UNICODE)


def turkish_lower(text: str) -> str:
    """Lower case as Turkish has it: "I" is "ı" and "İ" is "i"."""
    return text.replace("İ", "i").replace("I", "ı").lower()


def tokens(text: str) -> list[str]:
    """Turkish-lowercased words, digits-as-letters read, repeated letters squeezed."""
    words: list[str] = []
    for chunk in _CHUNK.findall(turkish_lower(text)):
        if any(c.isalpha() for c in chunk):
            chunk = chunk.translate(LEET)
        for word in _LETTERS.findall(chunk):
            words.append(_RUN.sub(r"\1", word))
    return words


def fold(word: str) -> str:
    return word.translate(FOLD)


@dataclass(frozen=True, slots=True)
class Hit:
    """Why a text was stopped. `term` is for the log, never for the child."""

    category: str
    term: str


@dataclass(frozen=True, slots=True)
class _Category:
    exact: frozenset[str] = frozenset()
    prefix: tuple[str, ...] = ()
    tr_exact: frozenset[str] = frozenset()
    phrase: tuple[tuple[str, ...], ...] = ()
    squash: tuple[str, ...] = ()


def _norm_folded(term: str) -> tuple[str, ...]:
    return tuple(fold(word) for word in tokens(term))


@dataclass(frozen=True)
class Lexicon:
    categories: Mapping[str, _Category] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Mapping[str, Iterable[str]]]) -> Lexicon:
        categories: dict[str, _Category] = {}
        for name, spec in raw.items():
            one = [_norm_folded(t) for t in spec.get("exact", [])]
            categories[name] = _Category(
                exact=frozenset(t[0] for t in one if len(t) == 1),
                prefix=tuple(
                    t[0]
                    for t in (_norm_folded(p) for p in spec.get("prefix", []))
                    if len(t) == 1
                ),
                tr_exact=frozenset(
                    t[0] for t in (tokens(w) for w in spec.get("tr_exact", [])) if len(t) == 1
                ),
                phrase=tuple(t for t in (_norm_folded(p) for p in spec.get("phrase", [])) if t)
                # A multi-word "exact" term is a phrase.
                + tuple(t for t in one if len(t) > 1),
                squash=tuple("".join(_norm_folded(s)) for s in spec.get("squash", [])),
            )
        return cls(categories)

    @classmethod
    def load(cls, path: Path | None = None) -> Lexicon:
        if path is not None:
            text = path.read_text(encoding="utf-8")
        else:
            text = (
                resources.files("biokids_ai")
                .joinpath("data", LEXICON_RESOURCE)
                .read_text(encoding="utf-8")
            )
        return cls.from_mapping(tomllib.loads(text))

    def screen(self, text: str) -> Hit | None:
        """The first category `text` falls into, or None if the list has no objection."""
        if _PHONE.search(text) or _EMAIL.search(text):
            return Hit("personal_info", "<contact details>")

        tr_words = tokens(text)
        folded = [fold(w) for w in tr_words]
        squashed = "".join(folded)
        for name, cat in self.categories.items():
            for word, tr_word in zip(folded, tr_words, strict=True):
                if word in cat.exact:
                    return Hit(name, word)
                if tr_word in cat.tr_exact:
                    return Hit(name, tr_word)
                for start in cat.prefix:
                    if word.startswith(start):
                        return Hit(name, start)
            for phrase in cat.phrase:
                if _contains_phrase(folded, phrase):
                    return Hit(name, " ".join(phrase))
            for term in cat.squash:
                if term and term in squashed:
                    return Hit(name, term)
        return None


def _contains_phrase(words: list[str], phrase: tuple[str, ...]) -> bool:
    """`phrase` in order; every word whole except the last, which may carry a suffix."""
    n = len(phrase)
    for i in range(len(words) - n + 1):
        window = words[i : i + n]
        if window[:-1] == list(phrase[:-1]) and window[-1].startswith(phrase[-1]):
            return True
    return False
