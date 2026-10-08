"""Bugünün sürprizi: one small new thing on the launcher's home screen every day.

The owner (D-203): a child who sees the same screen every time gets bored, and a
screen that changes is nicer. So the home screen carries one card a day - a fact,
an animal, a riddle, a small task, a word - inside a theme that changes each
week: space, the ocean, dinosaurs, the seasons...

Chosen, never generated. Every card is a sentence people wrote and checked, in
`data/daily/daily.toml`: a parental control must not teach a child something
untrue, and a model on a child's screen is the assistant's job, behind its own
gate (D-165). Chosen offline and deterministically, from the date and the child:
the same card all day, another one tomorrow, nothing stored and nothing sent.

How a day is chosen:

- The week (Monday to Sunday) picks the theme, in the order the file lists them,
  the same for every child in the family, so siblings share a topic.
- Inside the theme the cards are dealt with each kind spread evenly - a fact, a
  riddle, an animal, a task, a word, a fact... - so days in a row differ.
- The day of the week walks along that deal; each time the theme comes round
  again the walk starts seven cards further on, so a child meets the theme's
  other cards next time. The child's id shifts the start, so siblings sharing a
  theme do not get the same card on the same day.
- A task that points at an app is dealt only to a child who has that tile and
  whose machine can start it: a card must never send a child to an app that is
  not there.
"""

from __future__ import annotations

import datetime as dt
import functools
import logging
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from biokids_core.errors import ConfigError
from biokids_core.types import AgeBand

log = logging.getLogger(__name__)

BUNDLED_CORPUS = Path(__file__).resolve().parent / "data" / "daily" / "daily.toml"

KINDS: tuple[str, ...] = ("fact", "riddle", "animal", "challenge", "word")
"""Every kind of card, in the order a theme deals them."""

BANDS: tuple[str, ...] = (AgeBand.YOUNG.value, AgeBand.OLDER.value)
ACCENTS: tuple[str, ...] = ("coral", "amber", "teal", "indigo", "ok")
"""The brand tokens a theme may tint its card with (UX.md §1)."""

RIDDLE_EMOJI = "🤔"
"""A riddle's picture. Its own would give the answer away."""

TEXT_MAX = 120
"""One short sentence: everything a child of four to seven sees, and what Zio reads."""
MORE_MAX = 140
TITLE_MAX = 24
ANSWER_MAX = 32
LABEL_MAX = 28

DAYS_PER_WEEK = 7
CHILD_STRIDE = 3
"""How far apart siblings start in a theme's deal."""


@dataclass(frozen=True, slots=True)
class Theme:
    slug: str
    emoji: str
    label_tr: str
    label_en: str
    accent: str

    def label(self, locale: str) -> str:
        return self.label_tr if locale.startswith("tr") else self.label_en


@dataclass(frozen=True, slots=True)
class Surprise:
    """One card as written in the corpus, in both languages."""

    id: str
    theme: str
    kind: str
    emoji: str
    text_tr: str
    text_en: str
    more_tr: str = ""
    more_en: str = ""
    title_tr: str = ""
    title_en: str = ""
    answer_tr: str = ""
    answer_en: str = ""
    app: str = ""
    bands: tuple[str, ...] = BANDS

    def text(self, locale: str) -> str:
        return self.text_tr if locale.startswith("tr") else self.text_en

    def more(self, locale: str) -> str:
        return self.more_tr if locale.startswith("tr") else self.more_en

    def title(self, locale: str) -> str:
        return self.title_tr if locale.startswith("tr") else self.title_en

    def answer(self, locale: str) -> str:
        return self.answer_tr if locale.startswith("tr") else self.answer_en


@dataclass(frozen=True, slots=True)
class Corpus:
    themes: tuple[Theme, ...]
    items: tuple[Surprise, ...]

    def theme(self, slug: str) -> Theme | None:
        return next((t for t in self.themes if t.slug == slug), None)

    def dealt(self, theme: str) -> list[Surprise]:
        """The theme's cards in the order they are dealt.

        Each kind is spread evenly over the whole deal - each card sits in the
        middle of its kind's share, the first of four riddles at an eighth, the
        first of two words at a quarter - rather than one of each in turn,
        which would leave the kind a theme has most of piled up at the end,
        and a week of nothing but facts.
        """
        placed: list[tuple[Fraction, int, Surprise]] = []
        for order, kind in enumerate(KINDS):
            group = [i for i in self.items if i.theme == theme and i.kind == kind]
            placed += [
                (Fraction(2 * n + 1, 2 * len(group)), order, i) for n, i in enumerate(group)
            ]
        return [item for _at, _order, item in sorted(placed, key=lambda p: (p[0], p[1]))]


@dataclass(frozen=True, slots=True)
class DailyCard:
    """What the home screen draws: one card, in the child's language and band."""

    day: str
    """The date, ISO. Also what a dismissal is remembered by: tomorrow is new."""
    kind: str
    theme: str
    theme_label: str
    theme_emoji: str
    accent: str
    emoji: str
    title: str
    text: str
    answer: str = ""
    app: Mapping[str, str] | None = None
    """For a task: the child's own tile it points at (slug, name, icon)."""
    item_id: str = ""


# ------------------------------------------------------------------ loading


def load_corpus(path: Path | None = None) -> Corpus:
    """The corpus inside the package, parsed once per process."""
    return _load(path or BUNDLED_CORPUS)


@functools.cache
def _load(path: Path) -> Corpus:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc
    return parse(raw, source=str(path))


def parse(raw: Mapping[str, Any], *, source: str = "daily.toml") -> Corpus:
    """Build and check the corpus. A card that breaks a rule is refused here, so
    a mistake in the file shows up in the test suite, never on a child's screen."""
    themes = tuple(_theme(t, source) for t in _tables(raw, "theme", source))
    if not themes:
        raise ConfigError(f"{source}: no [[theme]]")
    if len({t.slug for t in themes}) != len(themes):
        raise ConfigError(f"{source}: a theme slug is used twice")
    known = {t.slug for t in themes}

    items: list[Surprise] = []
    seen: set[str] = set()
    for table in _tables(raw, "item", source):
        item = _item(table, source)
        if item.id in seen:
            raise ConfigError(f"{source}: item id {item.id!r} is used twice")
        if item.theme not in known:
            raise ConfigError(f"{source}: {item.id}: unknown theme {item.theme!r}")
        seen.add(item.id)
        items.append(item)
    return Corpus(themes=themes, items=tuple(items))


def _tables(raw: Mapping[str, Any], key: str, source: str) -> list[Mapping[str, Any]]:
    tables = raw.get(key, [])
    if not isinstance(tables, list) or not all(isinstance(t, dict) for t in tables):
        raise ConfigError(f"{source}: [[{key}]] must be a list of tables")
    return tables


def _text(table: Mapping[str, Any], key: str, limit: int, where: str) -> str:
    value = " ".join(str(table.get(key, "")).split())
    if len(value) > limit:
        raise ConfigError(f"{where}: {key} is {len(value)} characters, more than {limit}")
    return value


def _theme(table: Mapping[str, Any], source: str) -> Theme:
    slug = str(table.get("slug", "")).strip()
    where = f"{source}: theme {slug or '?'}"
    theme = Theme(
        slug=slug,
        emoji=str(table.get("emoji", "")).strip(),
        label_tr=_text(table, "label_tr", LABEL_MAX, where),
        label_en=_text(table, "label_en", LABEL_MAX, where),
        accent=str(table.get("accent", "")).strip(),
    )
    if not (theme.slug and theme.emoji and theme.label_tr and theme.label_en):
        raise ConfigError(f"{where}: slug, emoji, label_tr and label_en are required")
    if theme.accent not in ACCENTS:
        raise ConfigError(f"{where}: accent must be one of {', '.join(ACCENTS)}")
    return theme


def _item(table: Mapping[str, Any], source: str) -> Surprise:
    item_id = str(table.get("id", "")).strip()
    where = f"{source}: {item_id or 'an item with no id'}"
    kind = str(table.get("kind", "")).strip()
    if not item_id:
        raise ConfigError(f"{where}: every item needs an id")
    if kind not in KINDS:
        raise ConfigError(f"{where}: kind must be one of {', '.join(KINDS)}")
    bands = tuple(str(b) for b in table.get("bands", BANDS))
    if not bands or any(b not in BANDS for b in bands):
        raise ConfigError(f"{where}: bands must be among {', '.join(BANDS)}")

    item = Surprise(
        id=item_id,
        theme=str(table.get("theme", "")).strip(),
        kind=kind,
        emoji=RIDDLE_EMOJI if kind == "riddle" else str(table.get("emoji", "")).strip(),
        text_tr=_text(table, "text_tr", TEXT_MAX, where),
        text_en=_text(table, "text_en", TEXT_MAX, where),
        more_tr=_text(table, "more_tr", MORE_MAX, where),
        more_en=_text(table, "more_en", MORE_MAX, where),
        title_tr=_text(table, "title_tr", TITLE_MAX, where),
        title_en=_text(table, "title_en", TITLE_MAX, where),
        answer_tr=_text(table, "answer_tr", ANSWER_MAX, where),
        answer_en=_text(table, "answer_en", ANSWER_MAX, where),
        app=str(table.get("app", "")).strip(),
        bands=bands,
    )
    _check(item, table, where)
    return item


def _check(item: Surprise, table: Mapping[str, Any], where: str) -> None:
    """Each kind's own rule, in both languages at once: a card half translated
    would show a child a blank."""

    def both(name: str) -> bool:
        tr, en = getattr(item, f"{name}_tr"), getattr(item, f"{name}_en")
        if bool(tr) != bool(en):
            raise ConfigError(f"{where}: {name}_tr and {name}_en go together")
        return bool(tr)

    if not both("text"):
        raise ConfigError(f"{where}: text_tr and text_en are required")
    both("more")
    has_title, has_answer = both("title"), both("answer")
    if item.kind == "riddle":
        if not has_answer:
            raise ConfigError(f"{where}: a riddle needs answer_tr and answer_en")
        if "emoji" in table:
            raise ConfigError(f"{where}: a riddle's emoji would give the answer away")
    elif has_answer:
        raise ConfigError(f"{where}: only a riddle has an answer")
    elif not item.emoji:
        raise ConfigError(f"{where}: an emoji is required")
    if (item.kind in {"animal", "word"}) != has_title:
        raise ConfigError(f"{where}: an animal or a word has a title, and nothing else does")
    if item.app and item.kind != "challenge":
        raise ConfigError(f"{where}: only a task points at an app")


# ---------------------------------------------------------------- choosing


def week_number(day: dt.date) -> int:
    """Weeks since 1 January of year 1, a Monday: every week starts on a Monday."""
    return (day.toordinal() - 1) // DAYS_PER_WEEK


def theme_for(day: dt.date, corpus: Corpus) -> Theme:
    """This week's theme. The same for every child in the family."""
    return corpus.themes[week_number(day) % len(corpus.themes)]


def choose(
    day: dt.date,
    *,
    child_id: int,
    band: str,
    startable: Iterable[str],
    corpus: Corpus,
) -> Surprise | None:
    """Today's card for this child, or None when there is nothing to show."""
    can_start = set(startable)

    def fits(item: Surprise) -> bool:
        return band in item.bands and (not item.app or item.app in can_start)

    theme = theme_for(day, corpus)
    deal = [item for item in corpus.dealt(theme.slug) if fits(item)]
    if not deal:
        # A theme with nothing for this band would leave a gap in the week;
        # any card at all is better than none.
        deal = [item for t in corpus.themes for item in corpus.dealt(t.slug) if fits(item)]
    if not deal:
        return None
    visit = week_number(day) // len(corpus.themes)
    step = visit * DAYS_PER_WEEK + day.weekday() + max(child_id, 0) * CHILD_STRIDE
    return deal[step % len(deal)]


def card_for(
    day: dt.date,
    *,
    child_id: int,
    band: str,
    locale: str,
    tiles: Iterable[Mapping[str, Any]],
    corpus: Corpus | None = None,
) -> DailyCard | None:
    """Today's card, in this child's language, sized for their age band.

    `tiles` are the child's own tiles that can start on this machine: a task is
    only ever dealt for one of them.
    """
    corpus = corpus or load_corpus()
    own = {str(t.get("slug", "")): t for t in tiles if t.get("slug")}
    item = choose(day, child_id=child_id, band=band, startable=own, corpus=corpus)
    if item is None:
        return None
    theme = corpus.theme(item.theme) or theme_for(day, corpus)
    text = item.text(locale)
    if band != AgeBand.YOUNG.value and item.more(locale):
        # Eight to ten read a little more; four to seven get the one sentence.
        text = f"{text} {item.more(locale)}"
    app: dict[str, str] | None = None
    if item.app:
        tile = own[item.app]
        app = {
            "slug": item.app,
            "name": str(tile.get("name", item.app)),
            "icon": str(tile.get("icon", "app")),
        }
    return DailyCard(
        day=day.isoformat(),
        kind=item.kind,
        theme=theme.slug,
        theme_label=theme.label(locale),
        theme_emoji=theme.emoji,
        accent=theme.accent,
        emoji=item.emoji,
        title=item.title(locale),
        text=text,
        answer=item.answer(locale),
        app=app,
        item_id=item.id,
    )


def for_launcher(
    state: Mapping[str, Any],
    day: dt.date,
    can_start: Callable[[dict[str, Any]], bool],
) -> DailyCard | None:
    """The card for the launcher's home screen, from the daemon's shell state.

    Never a reason for the home screen to fail: whatever goes wrong here, the
    child gets their tiles without a card, and the journal says why.
    """
    try:
        tiles = [t for t in state.get("tiles", []) if isinstance(t, dict) and can_start(t)]
        return card_for(
            day,
            child_id=int(state.get("child_id") or 0),
            band=str(state.get("age_band") or AgeBand.YOUNG.value),
            locale=str(state.get("locale") or "tr"),
            tiles=tiles,
        )
    except (ConfigError, ValueError, TypeError, KeyError) as exc:
        log.warning("no surprise card today: %s", exc)
        return None


__all__ = [
    "BANDS",
    "BUNDLED_CORPUS",
    "KINDS",
    "Corpus",
    "DailyCard",
    "Surprise",
    "Theme",
    "card_for",
    "choose",
    "for_launcher",
    "load_corpus",
    "parse",
    "theme_for",
    "week_number",
]
