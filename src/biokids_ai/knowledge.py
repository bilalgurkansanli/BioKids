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
    """Readable text from an article page: no markup, no reference marks."""
    opened = re.search(r"<body[^>]*>", page, flags=re.I)
    body = page[opened.end() :] if opened else page
    body = re.sub(r"<(script|style|table)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
    text = html.unescape(_TAG.sub(" ", body))
    text = re.sub(r"\[\d+\]", "", text)
    return " ".join(text.split())


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
        return plain_text(body.decode("utf-8", "replace"))[: MAX_CHARS // MAX_ARTICLES]
