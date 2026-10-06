"""A small encyclopedia on the machine, for the assistant to check itself against (D-178).

A four-billion-parameter model knows a great deal and is sure of all of it,
including the Moon going round the Sun (D-165). No prompt removes that; a source
does. Before the model answers, the question's subject is looked up in a
Wikipedia excerpt kept on the machine - the opening paragraphs of the most-read
articles, from Kiwix - and what is found is handed to the model as the facts to
trust.

`kiwix-serve` (kiwix-tools, GPL-3, in Debian) does the searching, as a separate
process like llama-server: inside the assistant's own sandbox, on 127.0.0.1 of a
network namespace nothing else shares, started with the first question and
stopped when idle. No library of ours links against it.

Nothing found is not an error: the model answers as before. A lookup that fails
or takes too long is the same.
"""

from __future__ import annotations

import html
import logging
import re
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from biokids_ai.localhttp import Endpoint, HttpError, request
from biokids_ai.runtime import ManagedServer, ServerUnavailable

log = logging.getLogger(__name__)

PORT: Final = 8179
LOOKUP_TIMEOUT_S: Final = 4.0
MAX_ARTICLES: Final = 2
MAX_CHARS: Final = 900

STOPWORDS: Final = {
    "tr": frozenset(
        [
            "ne",
            "neyin",
            "neyi",
            "neye",
            "neden",
            "niçin",
            "niye",
            "nasıl",
            "nedir",
            "neler",
            "kim",
            "kimdir",
            "nerede",
            "nereden",
            "nereye",
            "ne",
            "zaman",
            "hangi",
            "kaç",
            "mi",
            "mı",
            "mu",
            "mü",
            "midir",
            "mıdır",
            "bir",
            "ve",
            "ile",
            "da",
            "de",
            "bu",
            "şu",
            "o",
            "için",
            "gibi",
            "olur",
            "olurlar",
            "oluyor",
            "oluşur",
            "var",
            "yok",
            "mı",
            "acaba",
            "peki",
            "bana",
            "anlat",
            "söyle",
            "ben",
            "sen",
            "biz",
            "çok",
            "en",
            "daha",
            "niye",
            "nasil",
            "neden",
        ]
    ),
    "en": frozenset(
        [
            "what",
            "why",
            "how",
            "who",
            "where",
            "when",
            "which",
            "is",
            "are",
            "was",
            "were",
            "do",
            "does",
            "did",
            "the",
            "a",
            "an",
            "of",
            "to",
            "in",
            "on",
            "for",
            "can",
            "could",
            "would",
            "tell",
            "me",
            "about",
            "please",
            "there",
            "it",
            "its",
            "they",
            "them",
            "you",
            "i",
        ]
    ),
}

_TAG: Final = re.compile(r"<[^>]+>")
_PARAGRAPH: Final = re.compile(r"<p[^>]*>(.*?)</p>", re.S | re.I)
MIN_PARAGRAPH: Final = 40
_ITEM: Final = re.compile(r"<item>(.*?)</item>", re.S)
_LINK: Final = re.compile(r"<link>(.*?)</link>", re.S)
_WORD: Final = re.compile(r"[\w'-]+", re.U)


@dataclass(frozen=True, slots=True)
class Book:
    locale: str
    name: str
    """The ZIM file's name without `.zim`: kiwix-serve's name for the book."""


def search_terms(question: str, locale: str) -> str:
    """The question without the words every question has."""
    stop = STOPWORDS.get(locale, STOPWORDS["en"])
    words = [w for w in _WORD.findall(question.lower()) if w not in stop and len(w) > 1]
    return " ".join(words[:6])


def plain_text(page: str) -> str:
    """The article's opening paragraphs as plain text.

    Only `<p>` elements: an infobox or a taxobox is a table of names and
    numbers - "Alt gruplar † Amygdalodon †..." on the VM - that tells a model
    nothing and crowds out the sentences that do.
    """
    paragraphs: list[str] = []
    for raw in _PARAGRAPH.findall(page):
        text = html.unescape(_TAG.sub("", raw))
        text = " ".join(re.sub(r"\[\d+\]", "", text).split())
        if len(text) >= MIN_PARAGRAPH:
            paragraphs.append(text)
        if len(paragraphs) == 2:
            break
    return " ".join(paragraphs)


SUFFIXES: Final = {
    "tr": (
        "lerin",
        "ların",
        "leri",
        "ları",
        "nın",
        "nin",
        "nun",
        "nün",
        "dan",
        "den",
        "tan",
        "ten",
        "lar",
        "ler",
        "ın",
        "in",
        "un",
        "ün",
        "da",
        "de",
        "ta",
        "te",
        "ya",
        "ye",
        "yı",
        "yi",
        "yu",
        "yü",
        "ı",
        "i",
        "u",
        "ü",
        "a",
        "e",
    ),
    "en": ("es", "s"),
}


def title_case(word: str, locale: str) -> str:
    """A word as an article title starts: Turkish dotted and dotless i kept apart."""
    if not word:
        return word
    turkish = {"i": "İ", "ı": "I"} if locale == "tr" else {}
    return turkish.get(word[0], word[0].upper()) + word[1:]


def title_guesses(question: str, locale: str) -> list[str]:
    """Article titles the question is probably about, best first.

    The subject of a child's question is nearly always its first content word
    ("Ay neyin etrafında döner?", "Dinozorlar ne zaman yaşadı?"). It is tried as
    written and then without the endings Turkish adds, so "dinozorlar" finds
    "Dinozor" and "Dünya'nın" finds "Dünya".
    """
    language = locale if locale in SUFFIXES else "en"
    guesses: list[str] = []
    for word in search_terms(question, language).split()[:3]:
        word = word.split("'", 1)[0]
        forms = [word]
        for suffix in SUFFIXES[language]:
            if word.endswith(suffix) and len(word) - len(suffix) >= 2:
                forms.append(word[: -len(suffix)])
        for form in forms:
            title = title_case(form, language)
            if title not in guesses:
                guesses.append(title)
    return guesses


class Knowledge:
    def __init__(self, server: ManagedServer, books: Sequence[Book]) -> None:
        self.server = server
        self.books = {book.locale: book for book in books}

    @property
    def endpoint(self) -> Endpoint:
        return self.server.endpoint

    async def lookup(self, question: str, locale: str) -> str:
        """A few paragraphs about the question's subject, or "" when there are none."""
        book = self.books.get(locale) or self.books.get("en")
        terms = search_terms(question, book.locale if book else locale)
        if book is None or not terms:
            return ""
        try:
            await self.server.ensure()
            # The article the question is about, by its title; a full-text
            # search only when no title fits.
            for title in title_guesses(question, book.locale):
                path = f"/content/{book.name}/{urllib.parse.quote(title.replace(' ', '_'))}"
                text = await self._article(path)
                if text:
                    return text[:MAX_CHARS]
            links = await self._search(book, terms)
            texts = [await self._article(link) for link in links[:MAX_ARTICLES]]
        except (ServerUnavailable, HttpError) as exc:
            log.info("no encyclopedia lookup: %s", exc)
            return ""
        joined = "\n\n".join(text for text in texts if text)
        return joined[:MAX_CHARS]

    async def _search(self, book: Book, terms: str) -> list[str]:
        query = urllib.parse.urlencode(
            {"books.name": book.name, "pattern": terms, "format": "xml", "pageLength": "3"}
        )
        status, body = await request(
            self.endpoint, "GET", f"/search?{query}", wait_s=LOOKUP_TIMEOUT_S
        )
        if status != 200:
            return []
        feed = body.decode("utf-8", "replace")
        links: list[str] = []
        for item in _ITEM.findall(feed):
            found = _LINK.search(item)
            if found:
                link = html.unescape(found.group(1).strip())
                path = urllib.parse.urlsplit(link).path or link
                links.append(path)
        return links

    async def _article(self, path: str) -> str:
        if not path.startswith("/"):
            return ""
        status, body = await request(self.endpoint, "GET", path, wait_s=LOOKUP_TIMEOUT_S)
        if status != 200:
            return ""
        return plain_text(body.decode("utf-8", "replace"))
