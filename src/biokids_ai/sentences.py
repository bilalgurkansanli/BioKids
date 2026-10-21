"""An answer, one whole sentence at a time, as the model writes it (D-216).

On the owner's VM the model writes about three tokens a second, so a child who
waited for the whole answer looked at "Düşünüyorum…" for half a minute. Each
sentence is now shown, and read aloud, as soon as it is known to be whole - and
screened before that (`assistant.py`).

Knowing that a sentence is whole is the delicate part. A full stop ends a
sentence only when what follows begins one: Turkish writes ordinals with a full
stop ("19. yüzyılda", "II. Mehmet"), titles are abbreviated ("Dr. Ayşe", "Prof.
Dr."), dates carry dots ("M.Ö. 3000"), and initials do ("J. K. Rowling"). So a
full stop needs to see the next word before it is believed, and a sentence that
runs on without one is cut at a pause once it is too long to wait for.

A wrong guess either way is harmless to safety: every sentence is screened
together with everything released before it, so where the cut falls cannot
change what the word list sees. It costs only a pause in Zio's reading, or a
sentence that arrives with the next one. The rules here keep both rare and
never cut inside a word.
"""

from __future__ import annotations

import re
from typing import Final

from biokids_ai.safety import fold, turkish_lower

MAX_SENTENCE_CHARS: Final = 200
"""A sentence for a child is far shorter: the prompts ask for five or seven
sentences in 260 or 380 tokens, about 180 characters each at the most. One that
runs past this is cut at a comma, or else between two words, so that a child
is not kept waiting by a model that forgot its full stops."""

#: A run of sentence-ending marks, any closing quotes or brackets after it, and
#: then whitespace - which must already have arrived, or the run may not be
#: over ("?!", "...") and the next word cannot be seen.
_END: Final = re.compile(r"([.!?…]+)[\"'”’»)\]]*(?=\s)")
_OPENERS: Final = "\"'“‘«(["
_ROMAN: Final = re.compile(r"[IVXLC]{1,7}")
_PAUSE: Final = re.compile(r"[,;:–—](?=\s)")
_SPACE: Final = re.compile(r"\s+")

ABBREVIATIONS: Final = frozenset(
    fold(turkish_lower(word))
    for word in (
        # Titles, Turkish first: "Dr. Ayşe", "Prof. Dr.", "Op. Dr.", "Hz. Ömer".
        "Dr",
        "Prof",
        "Doç",
        "Yrd",
        "Uzm",
        "Op",
        "Öğr",
        "Gör",
        "Arş",
        "Av",
        "Dt",
        "Sn",
        "Hz",
        "St",
        "Mr",
        "Mrs",
        "Ms",
        "Jr",
        "Sr",
        # In the middle of a sentence more often than at its end.
        "bkz",
        "örn",
        "vb",
        "vs",
        "vd",
        "yy",
        "MÖ",
        "cad",
        "sok",
        "mah",
        "apt",
        "tel",
    )
)
"""Words a full stop follows without ending the sentence, folded the way the
word list folds them ("Doç" and "doc" are one). "No." is not here: "No. The sun
is a star." is far likelier in an answer than a house number."""


def _abbreviated(before: str) -> bool:
    """Is the full stop after `before` part of the word rather than the end?"""
    words = before.split()
    if not words:
        return False
    word = words[-1].lstrip(_OPENERS)
    if not word:
        return False
    return (
        "." in word  # M.Ö, U.S - a dotted abbreviation's last dot
        or word.isdigit()  # "19. yüzyıl": the Turkish ordinal
        or _ROMAN.fullmatch(word) is not None  # "II. Mehmet", "XV. yüzyıl"
        or (len(word) == 1 and word.isalpha() and word.isupper())  # an initial
        or fold(turkish_lower(word)) in ABBREVIATIONS
    )


class SentenceSplitter:
    """Text in as it is written, whole sentences out as soon as each is known.

    `feed` takes the next piece of the model's text and returns the sentences
    it completed; `close` returns what is left once the model has finished.
    Sentences come back stripped and never empty.
    """

    def __init__(self, limit: int = MAX_SENTENCE_CHARS) -> None:
        self.limit = limit
        self._text = ""

    def feed(self, piece: str) -> list[str]:
        self._text += piece
        return self._take(final=False)

    def close(self) -> list[str]:
        sentences = self._take(final=True)
        rest = self._text.strip()
        self._text = ""
        if rest:
            sentences.append(rest)
        return sentences

    # ------------------------------------------------------------- internals

    def _take(self, *, final: bool) -> list[str]:
        sentences: list[str] = []
        while True:
            end = self._end(self._text, final=final)
            if end is None:
                end = self._overlong(self._text)
            if end is None:
                return sentences
            sentence, self._text = self._text[:end].strip(), self._text[end:]
            if sentence:
                sentences.append(sentence)

    def _end(self, text: str, *, final: bool) -> int | None:
        """Where the first sentence in `text` ends, or None if not known yet."""
        newline = _first_line_end(text)
        for match in _END.finditer(text):
            if newline is not None and match.start() >= newline:
                break
            verdict = _ends_here(text, match, final=final)
            if verdict is None:
                # The next word has not arrived. A line break that has is an
                # end all the same; otherwise wait for one more piece.
                return newline
            if verdict:
                return match.end()
        return newline

    def _overlong(self, text: str) -> int | None:
        """A cut for a sentence too long to wait for, between two words."""
        start = len(text) - len(text.lstrip())
        if len(text) - start <= self.limit:
            return None
        head = text[: start + self.limit]
        pauses = [m.end() for m in _PAUSE.finditer(head)]
        if pauses and pauses[-1] > start + self.limit // 2:
            return pauses[-1]
        spaces = [m.start() for m in _SPACE.finditer(head) if m.start() > start]
        if spaces:
            return spaces[-1]
        # One unbroken run of characters, a link or a row of emoji: cut at the
        # first space after it rather than inside a word the word list must see.
        later = _SPACE.search(text, start + self.limit)
        return later.start() if later else None


def _first_line_end(text: str) -> int | None:
    """The first line break that follows some text: a list item, a paragraph."""
    seen = False
    for index, char in enumerate(text):
        if char == "\n" and seen:
            return index
        if not char.isspace():
            seen = True
    return None


def _ends_here(text: str, match: re.Match[str], *, final: bool) -> bool | None:
    """True, False, or None when only the next word can tell."""
    rest = text[match.end() :].lstrip()
    if not rest:
        return True if final else None
    if rest[0].islower():
        # '"Neden?" diye sordu.' and "Dr. ayşe" alike: the sentence goes on.
        return False
    return not (match.group(1) == "." and _abbreviated(text[: match.start()]))
