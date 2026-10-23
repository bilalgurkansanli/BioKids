"""D-234: Bugün ne konuşalım? - a few things to ask about this evening.

Made only from what the panel already keeps. What matters: nothing for a child
who did not use the computer today, the home screen's card is the very one the
launcher dealt, a riddle comes with its answer for the parent, English words
and the day's main app are named, and at most three sentences.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from biokids_core import daily, talk
from biokids_core.db import Child
from biokids_core.db.models import EnglishItem, LedgerDay, UsageTick
from biokids_core.i18n import translator
from biokids_core.types import Category
from helpers import give_app

DAY = dt.date(2026, 10, 23)
TR = translator("tr")


def used_today(session: Session, child: Child, seconds: int = 1800) -> None:
    session.add(LedgerDay(child_id=child.id, day=DAY, general_s=seconds))
    session.commit()


def test_nothing_for_a_child_who_was_not_at_the_computer(
    session: Session, child: Child
) -> None:
    assert talk.prompts(session, child, DAY, TR) == []


def test_the_home_screens_card_is_the_one_the_launcher_dealt(
    session: Session, child: Child
) -> None:
    used_today(session, child)
    card = daily.card_for(
        DAY, child_id=child.id, band=child.band(DAY).value, locale="tr", tiles=[]
    )
    assert card is not None
    found = talk.prompts(session, child, DAY, TR)
    assert card.text in found[0].text
    if card.kind == "riddle":
        assert card.answer in found[0].text, "the parent gets the answer"


def test_every_kind_of_card_makes_a_sentence(session: Session, child: Child) -> None:
    """A week of days covers the kinds; none of them leaves the parent nothing."""
    for n in range(14):
        day = DAY + dt.timedelta(days=n)
        session.add(LedgerDay(child_id=child.id, day=day, general_s=600))
        session.commit()
        assert talk.prompts(session, child, day, TR), day


def test_new_english_words_are_named(session: Session, child: Child) -> None:
    used_today(session, child)
    for item in ("animals/cat", "animals/dog"):
        session.add(
            EnglishItem(child_id=child.id, item=item, due=DAY, first_day=DAY, last_day=DAY)
        )
    session.add(
        EnglishItem(
            child_id=child.id,
            item="animals/bird",
            due=DAY,
            first_day=DAY - dt.timedelta(days=3),
            last_day=DAY,
        )
    )
    session.commit()
    texts = [p.text for p in talk.prompts(session, child, DAY, TR, limit=5)]
    english = [t for t in texts if "İngilizce" in t]
    assert english, texts
    assert "cat ve dog" in english[0]
    assert "bird" not in english[0], "met before today"
    assert "“kedi”" in english[0]


def test_the_days_main_app_with_a_question_that_fits(session: Session, child: Child) -> None:
    used_today(session, child)
    give_app(session, child.id, "supertux", Category.GAMES)
    give_app(session, child.id, "tuxpaint", Category.FREE)
    session.add_all(
        [
            UsageTick(
                child_id=child.id, day=DAY, category="games", app_slug="supertux", seconds=1500
            ),
            UsageTick(
                child_id=child.id, day=DAY, category="free", app_slug="tuxpaint", seconds=400
            ),
        ]
    )
    session.commit()
    texts = [p.text for p in talk.prompts(session, child, DAY, TR, limit=5)]
    app = [t for t in texts if "Supertux" in t]
    assert app and "25 dk" in app[0] and "Hangi bölüme geldiğini sorun." in app[0]


def test_a_glance_is_not_the_days_main_app(session: Session, child: Child) -> None:
    used_today(session, child)
    give_app(session, child.id, "supertux", Category.GAMES)
    session.add(
        UsageTick(
            child_id=child.id, day=DAY, category="games", app_slug="supertux", seconds=120
        )
    )
    session.commit()
    assert not [
        p for p in talk.prompts(session, child, DAY, TR, limit=5) if "Supertux" in p.text
    ]


def test_a_sticker_earned_today(session: Session, child: Child) -> None:
    used_today(session, child)
    give_app(session, child.id, "gcompris", Category.LEARNING)
    session.add(
        UsageTick(
            child_id=child.id,
            day=DAY,
            category="learning",
            app_slug="gcompris",
            seconds=16 * 60,
        )
    )
    session.commit()
    texts = [p.text for p in talk.prompts(session, child, DAY, TR, limit=5)]
    assert any("çıkartma" in t for t in texts)


def test_at_most_three(session: Session, child: Child) -> None:
    used_today(session, child)
    give_app(session, child.id, "gcompris", Category.LEARNING)
    session.add(
        UsageTick(
            child_id=child.id,
            day=DAY,
            category="learning",
            app_slug="gcompris",
            seconds=20 * 60,
        )
    )
    session.add(
        EnglishItem(child_id=child.id, item="animals/cat", due=DAY, first_day=DAY, last_day=DAY)
    )
    session.commit()
    assert len(talk.prompts(session, child, DAY, TR)) <= talk.LIMIT


def test_english_for_an_english_panel(session: Session, child: Child) -> None:
    used_today(session, child)
    session.add(
        EnglishItem(child_id=child.id, item="animals/cat", due=DAY, first_day=DAY, last_day=DAY)
    )
    session.commit()
    texts = [p.text for p in talk.prompts(session, child, DAY, translator("en"), limit=5)]
    assert any("learned the English word cat" in t for t in texts)
