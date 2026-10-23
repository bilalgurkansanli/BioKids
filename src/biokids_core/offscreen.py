"""Şimdi ne yapsak?: something to do away from the screen, when it goes (D-231).

When the day's time is up, when a window closes until later, at night and in a
forced break, the launcher used to say only what was being taken away. Now Zio
also says what comes next: draw your favourite animal, find five blue things,
jump ten times, look at the farthest thing out of the window. The end of screen
time becomes a hand-over to something else, which is easier on a child than a
door shutting, and is what a parental control is for.

Chosen, never generated, like the day's surprise (`daily.py`): every card is a
sentence people wrote and checked, in `data/offscreen/offscreen.toml`, safe in
a room and never pointing back at a screen. Chosen offline and
deterministically from the moment, the age band, the date and the child: the
same card all day for one moment, another one tomorrow, siblings apart, nothing
stored and nothing sent.
"""

from __future__ import annotations

import datetime as dt
import functools
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from biokids_core.errors import ConfigError
from biokids_core.types import AgeBand

BUNDLED_CARDS = Path(__file__).resolve().parent / "data" / "offscreen" / "offscreen.toml"

MOMENTS: tuple[str, ...] = ("end", "night", "break")
"""When a card is shown: the day's time is up or a window closed (`end`), the day
ended in the evening (`night`, D-214), and a forced break (`break`, D-46)."""

KINDS: tuple[str, ...] = ("make", "play", "talk", "help", "read", "move", "eyes", "calm")
BANDS: tuple[str, ...] = (AgeBand.YOUNG.value, AgeBand.OLDER.value)

TEXT_MAX = 90
"""One short sentence a child of four hears from Zio and a child of eight reads."""

BREAK_MOVES = 3
"""How many cards a break shows: a few moves and something for the eyes, enough
for a five-minute break without a list to read."""

CHILD_STRIDE = 5
"""How far apart siblings start, so two children at one computer get different
cards on the same evening."""


@dataclass(frozen=True, slots=True)
class Card:
    id: str
    moment: str
    kind: str
    emoji: str
    text_tr: str
    text_en: str
    bands: tuple[str, ...] = BANDS

    def text(self, locale: str) -> str:
        return self.text_tr if locale.startswith("tr") else self.text_en

    def as_dict(self, locale: str) -> dict[str, str]:
        return {
            "id": self.id,
            "kind": self.kind,
            "emoji": self.emoji,
            "text": self.text(locale),
        }


def _card(raw: Mapping[str, Any], where: str) -> Card:
    try:
        card = Card(
            id=str(raw["id"]),
            moment=str(raw["moment"]),
            kind=str(raw["kind"]),
            emoji=str(raw["emoji"]),
            text_tr=str(raw["text_tr"]).strip(),
            text_en=str(raw["text_en"]).strip(),
            bands=tuple(str(b) for b in raw.get("bands", BANDS)),
        )
    except KeyError as missing:
        raise ConfigError(f"{where}: a card needs {missing}") from None
    if card.moment not in MOMENTS:
        raise ConfigError(f"{where}: {card.id} has no such moment: {card.moment}")
    if card.kind not in KINDS:
        raise ConfigError(f"{where}: {card.id} has no such kind: {card.kind}")
    if not card.bands or any(band not in BANDS for band in card.bands):
        raise ConfigError(f"{where}: {card.id} names an unknown age band")
    for text in (card.text_tr, card.text_en):
        if not text or len(text) > TEXT_MAX:
            raise ConfigError(f"{where}: {card.id} needs a sentence of 1-{TEXT_MAX} characters")
    if not card.emoji:
        raise ConfigError(f"{where}: {card.id} needs an emoji")
    return card


def load(path: Path = BUNDLED_CARDS) -> tuple[Card, ...]:
    """Every card, checked. A file that breaks a rule is refused, whole."""
    return _load(str(path))


@functools.cache
def _load(path: str) -> tuple[Card, ...]:
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    cards = tuple(_card(entry, path) for entry in raw.get("card", []))
    ids = [card.id for card in cards]
    if len(set(ids)) != len(ids):
        raise ConfigError(f"{path}: two cards share an id")
    for moment in MOMENTS:
        for band in BANDS:
            if len([c for c in cards if c.moment == moment and band in c.bands]) < BREAK_MOVES:
                raise ConfigError(f"{path}: too few {moment} cards for {band}")
    return cards


def choose(
    moment: str,
    band: str,
    day: dt.date,
    child_id: int,
    *,
    count: int = 1,
    cards: tuple[Card, ...] | None = None,
) -> list[Card]:
    """`count` cards for this moment, this child and this day, all different.

    The day walks through the moment's cards in the file's order, so tomorrow
    brings the next ones; the child's id shifts the start so siblings differ.
    A break deals moves and eyes before calm: the point of a break is to get
    up, and the breath at the end is the last thing said.
    """
    pool = [c for c in (cards if cards is not None else load()) if c.moment == moment]
    pool = [c for c in pool if band in c.bands] or pool
    if not pool:
        return []
    start = (day.toordinal() * count + child_id * CHILD_STRIDE) % len(pool)
    picked = [pool[(start + n) % len(pool)] for n in range(min(count, len(pool)))]
    if moment == "break":
        picked.sort(key=lambda card: card.kind == "calm")
    return picked


def for_screen(screen: str, state: Mapping[str, Any], day: dt.date) -> list[dict[str, str]]:
    """The cards a full screen of the launcher shows, ready for its template.

    `screen` is the template `_screen_for` chose. A launcher whose card file is
    broken shows the screen without them: a missing suggestion must never cost
    the child the screen that tells them the time is up.
    """
    moment = {
        "time_up.html": "end",
        "window_closed.html": "end",
        "good_night.html": "night",
        "break.html": "break",
    }.get(screen)
    if moment is None:
        return []
    band = str(state.get("age_band", AgeBand.YOUNG.value))
    locale = str(state.get("locale", "tr"))
    try:
        child_id = int(state.get("child_id", 0) or 0)
    except (TypeError, ValueError):
        child_id = 0
    try:
        picked = choose(
            moment, band, day, child_id, count=BREAK_MOVES if moment == "break" else 1
        )
    except ConfigError:
        return []
    return [card.as_dict(locale) for card in picked]
