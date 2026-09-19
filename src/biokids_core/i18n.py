"""Translation catalogs for Turkish and English.

The engine emits keys; this module resolves them. `reason_code` from a `Decision`
is a key, a voice event is a key, and every template string is a key — that is
the rule from `ARCHITECTURE.md` §8, and it is what stops one language quietly
falling behind the other.

Catalogs are gettext `.po` files. A compiled `.mo` is used when the package build
has produced one; otherwise the `.po` is read directly, so a developer editing a
string sees it immediately without a compile step. `dev/compile_i18n.py` writes
the `.mo` files and `dev/check_i18n.py` fails CI if the catalogs drift apart.

Turkish is the primary voice of the product (UX.md §4): Turkish is written first
and English is the translation, not the other way round.
"""

from __future__ import annotations

import gettext as _gettext
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

DOMAIN: Final = "biokids"
LOCALE_DIR: Final = Path(__file__).resolve().parent / "locale"
DEFAULT_LOCALE: Final = "tr"
FALLBACK_LOCALE: Final = "en"

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class Catalog:
    """One locale's messages, with `{placeholder}` substitution."""

    def __init__(self, locale: str, messages: dict[str, str]) -> None:
        self.locale = locale
        self._messages = messages

    def has(self, key: str) -> bool:
        return key in self._messages

    def get(self, key: str, /, **variables: Any) -> str:
        """Translate `key`, substituting `{name}`-style placeholders.

        An unknown key returns the key itself rather than raising. A missing
        string must never be the reason a child's screen fails to render; the
        key is ugly on screen, which is exactly the visibility we want, and
        `dev/check_i18n.py` catches it before release anyway.
        """
        template = self._messages.get(key, key)
        if not variables:
            return template
        return _PLACEHOLDER.sub(lambda m: str(variables.get(m.group(1), m.group(0))), template)

    def keys(self) -> frozenset[str]:
        return frozenset(self._messages)


class Translator:
    """A locale plus its fallback. This is what components hold."""

    def __init__(self, locale: str) -> None:
        self.locale = normalise_locale(locale)
        self._primary = load_catalog(self.locale)
        self._fallback = load_catalog(FALLBACK_LOCALE)

    def __call__(self, key: str, /, **variables: Any) -> str:
        if self._primary.has(key) or self.locale == FALLBACK_LOCALE:
            return self._primary.get(key, **variables)
        if self._fallback.has(key):
            return self._fallback.get(key, **variables)
        return self._primary.get(key, **variables)

    # An explicit alias, because `_` is easy to shadow in a template context.
    gettext = __call__


def normalise_locale(locale: str | None) -> str:
    """`tr_TR.UTF-8`, `TR`, `tr-tr` and `tr` all mean Turkish."""
    if not locale:
        return DEFAULT_LOCALE
    head = re.split(r"[._@-]", locale.strip())[0].lower()
    return head if head in available_locales() else DEFAULT_LOCALE


@lru_cache(maxsize=8)
def available_locales() -> tuple[str, ...]:
    if not LOCALE_DIR.is_dir():  # pragma: no cover - only if the package is broken
        return (DEFAULT_LOCALE, FALLBACK_LOCALE)
    found = sorted(p.name for p in LOCALE_DIR.iterdir() if (p / "LC_MESSAGES").is_dir())
    return tuple(found) or (DEFAULT_LOCALE, FALLBACK_LOCALE)


@lru_cache(maxsize=8)
def load_catalog(locale: str) -> Catalog:
    base = LOCALE_DIR / locale / "LC_MESSAGES"
    mo_path = base / f"{DOMAIN}.mo"
    po_path = base / f"{DOMAIN}.po"

    if mo_path.is_file():
        with mo_path.open("rb") as handle:
            translations = _gettext.GNUTranslations(handle)
        catalogue = {
            key: value
            for key, value in translations._catalog.items()  # type: ignore[attr-defined]
            if isinstance(key, str) and key
        }
        return Catalog(locale, catalogue)

    if po_path.is_file():
        return Catalog(locale, parse_po(po_path))

    return Catalog(locale, {})


def parse_po(path: Path) -> dict[str, str]:
    """A small `.po` reader: msgid/msgstr pairs, no plural forms, no contexts.

    BioKids has no plural-form strings — Turkish does not inflect the noun after
    a numeral, and the English catalogue is written to match. If that ever
    changes, this is the function that grows, not the call sites.
    """
    entries: dict[str, str] = {}
    msgid: str | None = None
    msgstr: str | None = None
    current: str | None = None

    def flush() -> None:
        nonlocal msgid, msgstr, current
        if msgid:
            entries[msgid] = msgstr or ""
        msgid = msgstr = current = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line.startswith("#"):
            continue
        if line.startswith("msgid "):
            flush()
            msgid = _unquote(line[6:])
            current = "id"
        elif line.startswith("msgstr "):
            msgstr = _unquote(line[7:])
            current = "str"
        elif line.startswith('"'):
            if current == "id" and msgid is not None:
                msgid += _unquote(line)
            elif current == "str" and msgstr is not None:
                msgstr += _unquote(line)
    flush()
    return entries


def _unquote(chunk: str) -> str:
    text = chunk.strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = text[1:-1]
    return (
        text.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t").replace("\\\\", "\\")
    )


@lru_cache(maxsize=8)
def translator(locale: str = DEFAULT_LOCALE) -> Translator:
    """Cached per locale. Translators are immutable."""
    return Translator(locale)


def reset_cache() -> None:
    """Drop cached catalogs. Used after the panel writes an edited voice line."""
    load_catalog.cache_clear()
    translator.cache_clear()
    available_locales.cache_clear()
