"""The sums in a child's question, worked out by the computer (D-248).

A small language model guesses at arithmetic: asked "7 kere 8 kaç eder?" Gemma
4 E2B answered "yedi eder... yirmi dört". No instruction fixes that, so the
assistant does what larger systems do with a calculator: the one sum in the
question is found here, worked out exactly, and given to the model as a fact
to use, beside what the encyclopedia says.

Whole numbers only, in digits or in words, Turkish or English, up to a
million; one sum per question - a child's question has one. Anything this does
not recognise is left to the model as before.
"""

from __future__ import annotations

import re
from typing import Final

LIMIT: Final = 1_000_000

_UNITS_TR: Final = {
    "sıfır": 0, "bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5,
    "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9,
}  # fmt: skip
_TENS_TR: Final = {
    "on": 10, "yirmi": 20, "otuz": 30, "kırk": 40, "elli": 50,
    "altmış": 60, "yetmiş": 70, "seksen": 80, "doksan": 90,
}  # fmt: skip
_UNITS_EN: Final = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}  # fmt: skip
_TENS_EN: Final = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}  # fmt: skip

#: The operations, by the words a child uses for them. Not "-" or "/": "7-8
#: yaşındakiler" and "1/2" are not sums to work out.
OPERATIONS: Final = (
    ("×", ("kere", "çarpı", "x", "*", "×", "times", "multiplied by")),
    ("+", ("artı", "+", "plus")),
    ("−", ("eksi", "minus", "take away")),
    ("÷", ("bölü", "÷", "divided by")),
)

_NUMBER_WORDS: Final = sorted(
    (*_UNITS_TR, *_TENS_TR, *_UNITS_EN, *_TENS_EN, "yüz", "bin", "hundred", "thousand"),
    key=len,
    reverse=True,
)
_WORD: Final = "(?:" + "|".join(_NUMBER_WORDS) + ")"
_NUMBER: Final = rf"(?:\d+|{_WORD}(?:[\s-]+{_WORD})*(?!\w))"


def _lower(text: str) -> str:
    return text.replace("I", "ı").replace("İ", "i").lower()


def number(text: str) -> int | None:
    """`text` as a whole number: digits, or number words in Turkish or English."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    total = current = 0
    for word in re.split(r"[\s-]+", text):
        if word in _UNITS_TR:
            current += _UNITS_TR[word]
        elif word in _TENS_TR:
            current += _TENS_TR[word]
        elif word in _UNITS_EN:
            current += _UNITS_EN[word]
        elif word in _TENS_EN:
            current += _TENS_EN[word]
        elif word in ("yüz", "hundred"):
            current = max(current, 1) * 100
        elif word in ("bin", "thousand"):
            total += max(current, 1) * 1000
            current = 0
        else:
            return None
    return total + current


def solve(question: str, locale: str = "tr") -> str | None:
    """The sum in `question` and its answer, like "7 × 8 = 56"; None for none."""
    text = _lower(question)
    for sign, words in OPERATIONS:
        spelled = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
        found = re.search(rf"(?<!\w)({_NUMBER})\s*(?:{spelled})\s*({_NUMBER})(?!\w)", text)
        if found is None:
            continue
        left, right = number(found.group(1)), number(found.group(2))
        if left is None or right is None or max(left, right) > LIMIT:
            return None
        return _worked(left, sign, right, locale)
    return None


def _worked(left: int, sign: str, right: int, locale: str) -> str | None:
    if sign == "×":
        return f"{left} × {right} = {left * right}"
    if sign == "+":
        return f"{left} + {right} = {left + right}"
    if sign == "−":
        return f"{left} − {right} = {left - right}"
    if right == 0:
        return None
    quotient, remainder = divmod(left, right)
    if remainder:
        if locale.startswith("tr"):
            return f"{left} ÷ {right} = {quotient}, kalan {remainder}"
        return f"{left} ÷ {right} = {quotient} remainder {remainder}"
    return f"{left} ÷ {right} = {quotient}"
