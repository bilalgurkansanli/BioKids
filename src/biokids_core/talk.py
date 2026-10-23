"""Bugün ne konuşalım?: a sentence or two for the evening, from the child's day (D-234).

The README says none of BioKids replaces talking to your child. This makes that
concrete: on Today, for each child who used the computer today, at most three
things a parent can ask about at dinner - the fact or riddle on the child's
home screen this morning (with the riddle's answer, for the parent), the
English words they met for the first time, the app they spent most time in,
and the sticker they earned.

Nothing new is collected. Every sentence is made from what the panel already
keeps and already shows elsewhere (`daily`, `english_item`, `usage_tick`, the
album), worded as an invitation rather than a report.
"""

from __future__ import annotations

import datetime as dt
import logging
import shlex
from dataclasses import dataclass
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from biokids_core import album, daily, english, reading, repo
from biokids_core.catalog import program_installed
from biokids_core.db.models import Child, EnglishItem, LedgerDay, ReadingItem, UsageTick
from biokids_core.i18n import Translator
from biokids_core.types import Category

log = logging.getLogger(__name__)

LIMIT: Final = 3
"""Sentences per child: enough for one evening, few enough to read on a phone."""

WORDS_NAMED: Final = 3
"""English words named in one sentence; more than three is a list, not a question."""

APP_MIN_SECONDS: Final = 5 * 60
"""An app counts as "most of the day" from five minutes, not from a glance."""

IGNORED_APPS: Final = frozenset({english.SLUG, reading.SLUG})
"""The English and reading corners have sentences of their own."""


@dataclass(frozen=True, slots=True)
class Prompt:
    icon: str
    text: str


def prompts(
    session: Session, child: Child, day: dt.date, tr: Translator, *, limit: int = LIMIT
) -> list[Prompt]:
    """Things to talk about tonight, for a child who used the computer today."""
    ledger = session.get(LedgerDay, {"child_id": child.id, "day": day})
    if ledger is None or ledger.general_s <= 0:
        return []
    found: list[Prompt] = []
    for make in (_surprise, _english, _reading, _app, _sticker):
        if len(found) >= limit:
            break
        try:
            prompt = make(session, child, day, tr)
        except Exception:
            # A sentence that cannot be worked out is one sentence fewer.
            log.exception("could not work out a talk prompt for child %s", child.id)
            continue
        if prompt is not None:
            found.append(prompt)
    return found


def _surprise(session: Session, child: Child, day: dt.date, tr: Translator) -> Prompt | None:
    """The card on the child's home screen today (D-203), dealt the way the
    launcher deals it, in the parent's language."""
    tiles = [
        {"slug": app.slug, "name": app.name(tr.locale), "icon": app.icon}
        for _link, app in repo.list_child_apps(session, child.id)
        if _can_start(app.kind, app.exec_cmd)
    ]
    card = daily.card_for(
        day,
        child_id=child.id,
        band=child.band(day).value,
        locale=tr.locale,
        tiles=tiles,
    )
    if card is None:
        return None
    name = child.display_name
    if card.kind == "challenge":
        return Prompt(card.emoji or "🎯", tr("talk.challenge", name=name, text=card.text))
    if card.kind == "riddle":
        return Prompt("🤔", tr("talk.riddle", name=name, riddle=card.text, answer=card.answer))
    subject = f"{card.title}: {card.text}" if card.title else card.text
    return Prompt(card.emoji or "💡", tr("talk.surprise", name=name, text=subject))


def _english(session: Session, child: Child, day: dt.date, tr: Translator) -> Prompt | None:
    """The English words the child met for the first time today (D-225)."""
    rows = session.scalars(
        select(EnglishItem.item)
        .where(EnglishItem.child_id == child.id, EnglishItem.first_day == day)
        .order_by(EnglishItem.item)
    ).all()
    units = english.load_units()
    words = [
        card
        for item in rows
        if english.is_word(item) and (card := english.card_for(item, units))
    ]
    if not words:
        return None
    named = [card.en for card in words[:WORDS_NAMED]]
    listed = (
        tr("talk.and").join([", ".join(named[:-1]), named[-1]]) if len(named) > 1 else named[0]
    )
    first = words[0]
    key = "talk.english" if len(named) > 1 else "talk.english_one"
    return Prompt("🇬🇧", tr(key, name=child.display_name, words=listed, tr_word=first.tr))


def _reading(session: Session, child: Child, day: dt.date, tr: Translator) -> Prompt | None:
    """The letters the child met for the first time today, in the reading corner (D-241)."""
    rows = session.scalars(
        select(ReadingItem.item).where(
            ReadingItem.child_id == child.id, ReadingItem.first_day == day
        )
    ).all()
    letters = [item.split("/letter-", 1)[1] for item in rows if "/letter-" in item]
    if not letters:
        return None
    shown = [reading.upper(char) + char for char in sorted(letters)][:WORDS_NAMED]
    listed = (
        tr("talk.and").join([", ".join(shown[:-1]), shown[-1]]) if len(shown) > 1 else shown[0]
    )
    key = "talk.reading" if len(shown) > 1 else "talk.reading_one"
    return Prompt("✏️", tr(key, name=child.display_name, letters=listed))


def _app(session: Session, child: Child, day: dt.date, tr: Translator) -> Prompt | None:
    """The app the child spent most of today in, and a question that fits it."""
    row = session.execute(
        select(UsageTick.app_slug, UsageTick.category, func.sum(UsageTick.seconds).label("s"))
        .where(
            UsageTick.child_id == child.id,
            UsageTick.day == day,
            UsageTick.app_slug != "",
            UsageTick.app_slug.not_in(IGNORED_APPS),
        )
        .group_by(UsageTick.app_slug, UsageTick.category)
        .order_by(func.sum(UsageTick.seconds).desc(), UsageTick.app_slug)
        .limit(1)
    ).first()
    if row is None or int(row.s or 0) < APP_MIN_SECONDS:
        return None
    found = repo.child_app(session, child.id, row.app_slug)
    if found is None:
        return None
    _link, app = found
    try:
        kind = Category(row.category)
    except ValueError:
        kind = Category.FREE
    return Prompt(
        "🧩",
        tr(
            "talk.app",
            name=child.display_name,
            app=app.name(tr.locale),
            minutes=int(row.s) // 60,
            hint=tr(f"talk.app.hint.{kind.value}"),
        ),
    )


def _sticker(session: Session, child: Child, day: dt.date, tr: Translator) -> Prompt | None:
    """A sticker earned today (D-211): something the child will love to show."""
    seconds = album.learning_seconds(session, child.id, since=day).get(day, 0)
    if day not in album.earned_days({day: seconds}):
        return None
    return Prompt("⭐", tr("talk.sticker", name=child.display_name))


def _can_start(kind: str, exec_cmd: str) -> bool:
    """As the launcher decides it for a tile, close enough for a challenge card."""
    if kind != "app":
        return True
    try:
        argv = shlex.split(exec_cmd)
    except ValueError:
        return False
    return bool(argv) and program_installed(argv[0])
