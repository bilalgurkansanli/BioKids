"""Searches a child's browser will not make (D-202).

The owner watched a child type an adult word into Google. SafeSearch filtered
the results, but the search itself went through and a results page came back.
The DNS filter (D-167) closes sites, not questions, so this closes the question:
a search whose words are on a list never leaves the machine.

**Where it is enforced.** In Chromium, by the BioKids extension (D-187), as
`declarativeNetRequest` rules. Each rule is one regular expression from this
module, matched against the address of every page, frame, script and
background request the child's browser makes, before it is sent. A match is
refused: a typed search, a suggestion while typing, a Wikipedia search or
article, the offline Vikipedi's search, suggestions and articles. The extension
installs only what the launcher's `GET /search-rules` serves, which is
`payload()`, so every pattern Chromium runs is built, and tested, here.

**Where in an address a term counts** (`FAMILIES`). A term must start a word:
at the start of a value or a path segment, or after a space (`+`, `%20`), `_`,
`-` or an encoded punctuation mark. And then:

- a short term, one word of at most `SHORT_LETTERS` letters ("porn", "seks",
  "sikiş"), only in a search box - the value of `q` (Google, Bing, DuckDuckGo,
  Brave, Ecosia, the suggestions of Google, YouTube and Wikipedia, most sites),
  `query`, `search` (Wikipedia), `search_query` (YouTube), `text` (Yandex),
  `pattern` or `term` (kiwix-serve's search and suggestions) - or in a title,
  any part of a path after `/wiki/` (Wikipedia) or `/content/` (kiwix-serve 3,
  `/content/<book>/A/<title>`);
- a longer word or a phrase in any parameter's value or any part of a path,
  on any site.

Short words stay out of the rest of an address because the random tokens in
long addresses spell one now and then, and a child would meet a closed page
they never asked for; six letters and more almost never come up by chance.

**Normalisation, inside the pattern.** The browser matches an address, not the
text the child typed, so each way of typing a word is spelled out in the
pattern instead of normalised away beforehand. The address carries the words as
typed, percent-encoded as UTF-8 ("ş" is `%C5%9F`), a space as `+` or `%20`. So
each letter of a term matches itself in either case (the rule is
case-insensitive); a Turkish letter also matches its ASCII neighbour and its
capital ("ş" is `s`, `%C5%9F` or `%C5%9E`), so "sikis" and "SİKİŞ" are
"sikiş"; a look-alike digit stands for a letter (4 a, 3 e, 1 i, 0 o, 5 s); and
a letter may be held down ("pooorno") unless the term is `exact`. Between the
words of a phrase, any run of spaces or punctuation. A dotted "i" in a term
never matches a dotless "ı", so "sık sık" and "sıkışık trafik" stay searchable
while "sikiş" does not - except typed in capitals with an ASCII "I", which
reads as "i" too ("SIKIŞIK"). An ASCII letter in a term does not match an
invented accent: "pörnö" is not a spelling anyone types.

**Limits.** Chromium compiles each regular expression with 2 KB of RE2 memory,
about a hundred program instructions, and takes at most 1000 of them per
extension. `re2_cost` estimates a pattern's instructions the way RE2 counts
them for these patterns, and terms are packed into patterns of at most
`MAX_COST`, which was measured against Chromium's own `isRegexSupported`. A
term whose fullest spelling does not fit drops the look-alike digits, then
held-down letters, then the capital "İ" (`SPELLINGS`). The extension checks
each pattern again before installing it, and logs any it had to skip.

Pure functions, and no dependency on the database or the network: the
launcher, the panel and the tests all call the same code.
"""

from __future__ import annotations

import functools
import hashlib
import re
import tomllib
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DEFAULT_LIST: Final = Path(__file__).resolve().parent / "data" / "search_words.toml"
"""The curated list, with what it covers and leaves out written at its top."""

SETTING_KEY: Final = "search_words"
"""The `setting` row holding the parent's own terms, a JSON list, for the whole
family: there is one browser policy on the machine, and a word a parent does
not want one child of four to ten to search is rarely one they want another to."""

MODES: Final = ("prefix", "whole", "exact", "squash")

MAX_PARENT_TERMS: Final = 100
"""How many terms a parent may add."""

MAX_TERM_CHARS: Final = 30
"""The longest term a parent may add, spaces included. A longer one is a
sentence, and no search is that exact."""

WHOLE_BELOW_LETTERS: Final = 4
"""A parent's word with fewer letters than this closes only itself, whole.
Turkish is agglutinative, so a longer one closes the words that begin with it
("kumar" closes "kumarhane", "kumarda"); a short one would close half the
language that way."""

MAX_COST: Final = 104
"""The most RE2 instructions (`re2_cost`) one pattern may have.

Measured, not derived (D-202): Chromium 154's `isRegexSupported` (Edge 154,
the same engine) accepted all 804 patterns built at 104 and at 116 from the
default list and from 400 random parent terms heavy in Turkish letters, and
refused a 90-letter literal behind a lead-in, estimated at 120. 104 keeps a
tenth in hand. Each family's lead-in takes 27 to 63 of it."""

PATTERN_VERSION: Final = 2
"""Raised whenever the shape of the patterns changes, so an extension that
kept yesterday's rules installs today's even when the terms are the same."""

# ----------------------------------------------------------- normalisation


def turkish_lower(text: str) -> str:
    """Lower case as Turkish has it: "I" is "ı" and "İ" is "i".

    Safe for English too: a dotless "ı" in a term matches an ASCII "i" in the
    address, so "PORN" and "BIKINI" still read as the words they are.
    """
    text = unicodedata.normalize("NFC", text)
    return text.replace("İ", "i").replace("I", "ı").lower()


_WORD: Final = re.compile(r"[^\W_]+")


def canonical(text: str) -> str:
    """A term as it is stored: Turkish lower case, its words split by one space.

    Punctuation and spaces only separate words: "Çıplak-Kız!" is "çıplak kız".
    """
    return " ".join(_WORD.findall(turkish_lower(text)))


def letters(text: str) -> int:
    return sum(1 for ch in text if ch.isalpha())


# --------------------------------------------------------------------- terms


@dataclass(frozen=True, slots=True)
class Term:
    """One entry: its words in canonical form, and how they are matched."""

    words: tuple[str, ...]
    mode: str = "prefix"

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown match mode {self.mode!r}")
        if not self.words or not all(_WORD.fullmatch(word) for word in self.words):
            raise ValueError(f"not a term: {self.words!r}")

    @property
    def text(self) -> str:
        return " ".join(self.words)

    @classmethod
    def parse(cls, text: str, mode: str = "prefix") -> Term:
        return cls(tuple(canonical(text).split()), mode)


def load_terms(path: Path = DEFAULT_LIST) -> tuple[Term, ...]:
    """The curated list: every category's terms, in the file's order.

    A term listed twice is an error rather than a choice: the second mode
    would be dropped without a word, as "hentai" in both `prefix` and `squash`
    once was, and its spelled-out form with it.
    """
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    terms: list[Term] = []
    for category in raw.values():
        if not isinstance(category, dict):
            raise ValueError(f"{path}: every category is a table")
        for mode, entries in category.items():
            if mode not in MODES:
                raise ValueError(f"{path}: unknown match mode {mode!r}")
            terms.extend(Term.parse(str(entry), mode) for entry in entries)
    unique = _unique(terms)
    if len(unique) != len(terms):
        seen = [term.words for term in terms]
        twice = sorted({" ".join(words) for words in seen if seen.count(words) > 1})
        raise ValueError(f"{path}: listed more than once: {', '.join(twice)}")
    return tuple(unique)


@functools.cache
def default_terms() -> tuple[Term, ...]:
    return load_terms(DEFAULT_LIST)


def parent_term(text: str) -> Term | None:
    """A parent's term as a `Term`, or None if it cannot be one.

    A word of fewer than `WHOLE_BELOW_LETTERS` letters is matched whole, a longer
    one as the start of a word; in a phrase, the last word decides. None for a
    term too long, with fewer than two letters, or too long for one pattern
    even at its plainest spelling.
    """
    term = canonical(text)
    if not term or len(term) > MAX_TERM_CHARS or letters(term) < 2:
        return None
    words = tuple(term.split())
    mode = "whole" if letters(words[-1]) < WHOLE_BELOW_LETTERS else "prefix"
    found = Term(words, mode)
    return found if fits(found) else None


def parse_parent_terms(text: str) -> tuple[list[str], list[str]]:
    """What a parent typed, one term per line (or comma): (kept, refused).

    Kept terms are canonical and unique, at most `MAX_PARENT_TERMS`. Refused
    are shown back to the parent as typed: too long, fewer than two letters,
    or over the limit.
    """
    kept: list[str] = []
    refused: list[str] = []
    for item in re.split(r"[\n\r,;]+", text):
        term = canonical(item)
        if not term or term in kept:
            continue
        if parent_term(term) is None or len(kept) >= MAX_PARENT_TERMS:
            refused.append(item.strip())
            continue
        kept.append(term)
    return kept, refused


def effective_terms(parent: Iterable[str] = ()) -> tuple[Term, ...]:
    """The default list, then the parent's terms it does not already hold."""
    extra = [term for term in (parent_term(text) for text in parent) if term is not None]
    return tuple(_unique([*default_terms(), *extra[:MAX_PARENT_TERMS]]))


def _unique(terms: Iterable[Term]) -> list[Term]:
    seen: set[tuple[str, ...]] = set()
    out: list[Term] = []
    for term in terms:
        if term.words not in seen:
            seen.add(term.words)
            out.append(term)
    return out


# ------------------------------------------------------------------ patterns

_LOOKALIKE: Final[dict[str, str]] = {"a": "4", "e": "3", "i": "1", "o": "0", "s": "5"}
"""Digits a child types for letters: "p0rn", "s3x", "s1k1s"."""

_TURKISH: Final[dict[str, tuple[str, str]]] = {
    "ç": ("c", "%c3%[8a]7"),
    "ğ": ("g", "%c4%9[ef]"),
    "ı": ("i", "%c4%b1"),
    "ö": ("o", "%c3%[9b]6"),
    "ş": ("s", "%c5%9[ef]"),
    "ü": ("u", "%c3%[9b]c"),
    "â": ("a", "%c3%[8a]2"),
    "î": ("i", "%c3%[8a]e"),
    "û": ("u", "%c3%[9b]b"),
}
"""A Turkish letter in a term: the ASCII letter it is typed as on a keyboard
without Turkish letters, and its UTF-8 encoding in both cases, folded into one
expression (`%C5%9F` is ş, `%C5%9E` is Ş). The capital of "ı" is the ASCII "I",
which the rule's case-insensitivity already gives.

Never the other way round: an "i" in a term does not match a typed "ı", so
"sık" is not "sik"; it does match a typed "İ", its capital."""

_DOTTED_CAPITAL: Final = "%c4%b0"
"""İ, the capital of the dotted "i"."""

_SPACE: Final = r"(?:[+_-]|%2[0-9a-f])"
"""A space or a mark between words: `+` in a value, `_` in a Wikipedia title,
`-`, and `%20` to `%2F`, which are an encoded space and `!"#$%&'()*+,-./`."""

_AFTER: Final = r"(?:[^a-z0-9%]|%[23]|$)"
"""What ends a whole word: anything but a letter, a digit or the start of an
encoded letter (`%C3...`); an encoded space or punctuation mark; the end."""

_GAP: Final = r"[._+-]?"
"""Between the letters of a `squash` term: "p o r n o", "p.o.r.n.o"."""

SHORT_LETTERS: Final = 5
"""A one-word term of at most this many letters is short. The random tokens in
long addresses spell a short word now and then, so a short term counts only in
a search box or a wiki title; a longer word or a phrase counts anywhere."""


@dataclass(frozen=True, slots=True)
class Family:
    """Where a term may stand: the start of each pattern, and for which terms."""

    name: str
    lead: str
    short: bool
    """The family of the short terms (True) or of the long ones (False)."""


FAMILIES: Final = (
    Family(
        "search",
        rf"[?&](?:q|query|search(?:_query)?|te(?:xt|rm)|pattern)=(?:[^&]*{_SPACE})?",
        short=True,
    ),
    Family("title", r"/(?:wiki|content)/(?:[^?]*(?:[/_:-]|%[23][0-9a-f]))?", short=True),
    Family("anywhere", rf"[=/](?:[^?&]*{_SPACE})?", short=False),
)
"""The search box: `q` (Google, Bing, DuckDuckGo, Brave, Ecosia, YouTube's and
Wikipedia's suggestions, most sites), `query` (Startpage), `search` (Wikipedia),
`search_query` (YouTube), `text` (Yandex), `pattern` and `term` (kiwix-serve's
search and suggestions). The title: any part of a path after `/wiki/`
(Wikipedia) or `/content/` (kiwix-serve 3, `/content/<book>/A/<title>`).
Anywhere: any parameter's value and any part of any path, on any site."""


def is_short(term: Term) -> bool:
    return len(term.words) == 1 and letters(term.words[0]) <= SHORT_LETTERS


@dataclass(frozen=True, slots=True)
class Spelling:
    """Which spellings a term's pattern accepts, beyond case and Turkish letters."""

    lookalike: bool = True
    """Digits for letters: "p0rn", "s3x"."""
    held: bool = True
    """A letter held down: "pooorno". Never for an `exact` term."""
    capital_i: bool = True
    """"İ" for a dotted "i", as Turkish capitals are typed: "SİKİŞ"."""


FULL_SPELLING: Final = Spelling()

SPELLINGS: Final = (
    FULL_SPELLING,
    Spelling(lookalike=False),
    Spelling(lookalike=False, held=False),
    Spelling(lookalike=False, held=False, capital_i=False),
)
"""From the fullest down; a term uses the first that fits in a pattern. Case,
Turkish letters typed in ASCII, and Turkish capitals other than "İ" cost little
and are always matched."""


def _encoded(text: str) -> str:
    return "".join(f"%{byte:02x}" for byte in text.encode("utf-8"))


def letter_pattern(ch: str, spelling: Spelling = FULL_SPELLING) -> str:
    """Every spelling of one letter (or digit) of a term, as one regex atom."""
    if ch.isascii():
        if not ch.isalnum():
            raise ValueError(f"not a letter or digit: {ch!r}")
        plain = f"[{ch}{_LOOKALIKE[ch]}]" if spelling.lookalike and ch in _LOOKALIKE else ch
        if ch == "i" and spelling.capital_i:
            return f"(?:{plain}|{_DOTTED_CAPITAL})"
        return plain
    if ch in _TURKISH:
        base, encoded = _TURKISH[ch]
        plain = letter_pattern(base, Spelling(spelling.lookalike, capital_i=False))
        return f"(?:{plain}|{encoded})"
    # Another script or accent: its unaccented ASCII letter if it has one ("ñ"
    # is n), and its own UTF-8 bytes in both cases.
    options: list[str] = []
    stripped = unicodedata.normalize("NFKD", ch)[0]
    if stripped.isascii() and stripped.isalnum():
        options.append(letter_pattern(stripped.lower(), spelling))
    for form in (ch, ch.upper()):
        if len(form) == 1 and _encoded(form) not in options:
            options.append(_encoded(form))
    return f"(?:{'|'.join(options)})"


def word_pattern(
    word: str, spelling: Spelling = FULL_SPELLING, *, spelled_out: bool = False
) -> str:
    """One word of a term, every letter in all its spellings."""
    atoms = [letter_pattern(ch, spelling) + ("+" if spelling.held else "") for ch in word]
    return (_GAP if spelled_out else "").join(atoms)


def term_pattern(term: Term, spelling: Spelling = FULL_SPELLING) -> str:
    """The pattern for one term, without its family's lead-in."""
    if term.mode == "exact":
        spelling = Spelling(spelling.lookalike, held=False, capital_i=spelling.capital_i)
    words = (_SPACE + "+").join(
        word_pattern(word, spelling, spelled_out=term.mode == "squash") for word in term.words
    )
    return words + (_AFTER if term.mode in {"whole", "exact"} else "")


def _fitted(term: Term, lead_cost: int) -> str | None:
    """The fullest spelling of `term` that fits beside a lead-in, or None."""
    for spelling in SPELLINGS:
        body = term_pattern(term, spelling)
        if lead_cost + fragment_cost(body) <= MAX_COST:
            return body
    return None


def families_for(term: Term) -> list[Family]:
    return [family for family in FAMILIES if family.short == is_short(term)]


def fits(term: Term) -> bool:
    """Whether `term` fits in a pattern of each of its families, at its plainest."""
    return all(
        _fitted(term, re2_cost(family.lead)) is not None for family in families_for(term)
    )


def build_patterns(terms: Sequence[Term]) -> list[str]:
    """`<lead-in>(?:t1|t2|...)` for each family, as few as fit `MAX_COST` each.

    Terms keep their order. A term that does not fit even at its plainest
    (only possible for one that bypassed `parent_term`) gets a pattern of its
    own, for Chromium's `isRegexSupported` to judge.
    """
    patterns: list[str] = []
    for family in FAMILIES:
        lead_cost = re2_cost(family.lead)
        group: list[str] = []
        group_cost = 0
        for term in terms:
            if is_short(term) != family.short:
                continue
            body = _fitted(term, lead_cost) or term_pattern(term, SPELLINGS[-1])
            cost = fragment_cost(body)
            if group and lead_cost + group_cost + 1 + cost > MAX_COST:
                patterns.append(f"{family.lead}(?:{'|'.join(group)})")
                group, group_cost = [], 0
            group_cost += cost + (1 if group else 0)
            group.append(body)
        if group:
            patterns.append(f"{family.lead}(?:{'|'.join(group)})")
    return patterns


def payload(parent: Iterable[str] = (), *, complete: bool = True) -> dict[str, object]:
    """What `GET /search-rules` answers, for the extension to install.

    `complete` is False when the parent's terms could not be read (the daemon
    did not answer): the extension then keeps a complete set it already has
    rather than trade it for the default list alone.
    """
    terms = effective_terms(parent)
    patterns = _patterns_for(terms)
    digest = hashlib.sha256(
        "\n".join([str(PATTERN_VERSION), *patterns]).encode("utf-8")
    ).hexdigest()
    return {
        "biokids": "search-rules",
        "version": digest[:16],
        "complete": complete,
        "terms": len(terms),
        "patterns": list(patterns),
    }


@functools.lru_cache(maxsize=8)
def _patterns_for(terms: tuple[Term, ...]) -> tuple[str, ...]:
    return tuple(build_patterns(terms))


# ---------------------------------------------------------- the RE2 budget


def re2_cost(pattern: str) -> int:
    """About how many instructions RE2 compiles `pattern` into, as Chromium runs it.

    Chromium compiles a rule's expression with RE2 in Latin-1 mode, case-
    insensitive, without captures. Then a literal is one instruction; a
    character class is one per byte range and one per join between them, with
    A-Z dropped when the class has a-z as well; every `|`, `+`, `*` and `?` is
    one; and an unanchored pattern starts with a two-instruction `.*?` loop,
    besides the program's Match and Fail. RE2 factors common prefixes out of an
    alternation and coalesces `a+a+`, so this errs high rather than low. Only
    the syntax this module writes is read: literals, `\\` escapes, `[...]`,
    `(?:...)`, `|`, `+`, `*`, `?`, `^`, `$`.
    """
    return fragment_cost(pattern) + 2 + (0 if pattern.startswith("^") else 2)


def fragment_cost(fragment: str) -> int:
    """`re2_cost` of a piece of a pattern, without the program's own overhead."""
    parser = _CostParser(fragment)
    cost = parser.alternation()
    if parser.pos != len(fragment):
        raise ValueError(f"unbalanced pattern at {parser.pos}: {fragment!r}")
    return cost


class _CostParser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def _peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def alternation(self) -> int:
        costs = [self._sequence()]
        while self._peek() == "|":
            self.pos += 1
            costs.append(self._sequence())
        return sum(costs) + len(costs) - 1

    def _sequence(self) -> int:
        total = 0
        while self._peek() not in {"", "|", ")"}:
            atom = self._atom()
            while self._peek() in {"+", "*", "?"}:
                self.pos += 1
                atom += 1
            total += atom
        return total

    def _atom(self) -> int:
        if self.text.startswith("(?:", self.pos):
            self.pos += 3
            inner = self.alternation()
            if self._peek() != ")":
                raise ValueError(f"unclosed group in {self.text!r}")
            self.pos += 1
            return inner
        ch = self._peek()
        if ch == "(":
            raise ValueError(f"capturing group in {self.text!r}")
        if ch == "[":
            return self._char_class()
        self.pos += 2 if ch == "\\" else 1
        return 3 if ch == "." else 1

    def _char_class(self) -> int:
        self.pos += 1
        negate = self._peek() == "^"
        if negate:
            self.pos += 1
        members: set[int] = set()
        first = True
        while first or self._peek() != "]":
            if not self._peek():
                raise ValueError(f"unclosed class in {self.text!r}")
            first = False
            low = self._class_char()
            high = low
            if self._peek() == "-" and self.text[self.pos + 1 : self.pos + 2] not in {"]", ""}:
                self.pos += 1
                high = self._class_char()
            members.update(range(ord(low), ord(high) + 1))
        self.pos += 1
        for byte in list(members):
            if chr(byte).isascii() and chr(byte).isalpha():
                members.add(ord(chr(byte).swapcase()))
        if negate:
            members = set(range(256)) - members
        # Case-insensitive: the A-Z ranges are dropped, a-z carries the fold.
        kept = sorted(byte for byte in members if not 0x41 <= byte <= 0x5A)
        ranges = sum(1 for i, byte in enumerate(kept) if i == 0 or byte != kept[i - 1] + 1)
        return max(1, 2 * ranges - 1)

    def _class_char(self) -> str:
        ch = self._peek()
        if ch == "\\":
            self.pos += 2
            return self.text[self.pos - 1]
        self.pos += 1
        return ch


# ------------------------------------------------------------------ matching


@functools.lru_cache(maxsize=8)
def _compiled(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    # The flags Chromium applies: case-insensitive, and the address is ASCII.
    return tuple(re.compile(pattern, re.IGNORECASE | re.ASCII) for pattern in patterns)


def address_blocked(url: str, patterns: Sequence[str]) -> bool:
    """Would Chromium, given `patterns`, refuse this address?

    The same expressions with the same flags; Python's `re` and RE2 agree on
    the small syntax used here.
    """
    return any(pattern.search(url) for pattern in _compiled(tuple(patterns)))
