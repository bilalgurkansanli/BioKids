"""The English corner's lessons, boxes and words (D-225)."""

from __future__ import annotations

import datetime as dt
import random

import pytest

from biokids_core import english
from biokids_core.english import ItemState

TODAY = dt.date(2026, 10, 22)
UNITS = english.load_units()


def _met(
    *items: str, box: int = 1, due: dt.date = TODAY, first: dt.date | None = None
) -> dict[str, ItemState]:
    return {item: ItemState(box, due, first or TODAY - dt.timedelta(days=10)) for item in items}


def _all_items(unit: english.Unit, box: int = 3) -> dict[str, ItemState]:
    return _met(*unit.item_ids(), box=box, due=TODAY + dt.timedelta(days=5))


# ------------------------------------------------------------------ content


def test_the_units_load_and_every_picture_is_one_the_font_can_draw() -> None:
    assert len(UNITS) >= 12
    for unit in UNITS:
        assert unit.words and unit.lines and unit.story and unit.questions, unit.slug
        for card in (*unit.words, *unit.lines, *unit.story):
            assert card.en and card.tr and card.pic, (unit.slug, card)
            assert not card.pic.isascii(), f"{card.pic!r} is a word, not a picture"
        pictures = [card.pic for card in unit.words]
        assert len(set(pictures)) == len(pictures), f"{unit.slug}: two words, one picture"
        for question in unit.questions:
            assert question.answer in question.choices


def test_every_id_is_unique_and_stable_in_form() -> None:
    ids = [item for unit in UNITS for item in unit.item_ids()]
    assert len(ids) == len(set(ids))
    assert "animals/cat" in ids and "animals/line-1" in ids and "animals/story" in ids
    assert all(len(item) <= 64 for item in ids), "the column is 64 characters"


def test_a_question_whose_answer_is_not_a_choice_is_refused() -> None:
    text = """
[[unit]]
slug = "x"
title_tr = "X"
title_en = "X"
icon = "x"
words = [{ en = "cat", tr = "kedi", pic = "🐱" }]
questions = [{ en = "Q", tr = "S", answer = "🐶", choices = ["🐱"] }]
"""
    with pytest.raises(ValueError, match="answer"):
        english.parse_units(text)


def test_only_the_corners_own_ids_are_items() -> None:
    assert english.is_item("animals/cat")
    assert english.is_item("food/story")
    assert not english.is_item("animals/unicorn")
    assert not english.is_item("../../etc/passwd")
    card = english.card_for("food/line-3")
    assert card is not None and card.en == "Can I have water, please?"
    assert english.card_for("food/story") is None


# ----------------------------------------------------------------- the boxes


def test_a_new_item_goes_to_the_first_box_whichever_way() -> None:
    for right in (True, False):
        state = english.answered(None, right, TODAY)
        assert state.box == 1
        assert state.due == TODAY + dt.timedelta(days=1)
        assert state.first_day == TODAY


def test_right_goes_up_a_box_and_wrong_goes_back_to_the_first() -> None:
    state = ItemState(2, TODAY, TODAY - dt.timedelta(days=5))
    up = english.answered(state, True, TODAY)
    assert up.box == 3 and up.due == TODAY + dt.timedelta(days=english.BOX_DAYS[3])
    assert up.first_day == state.first_day, "the day it was first met stays"
    down = english.answered(up, False, TODAY)
    assert down.box == 1 and down.due == TODAY + dt.timedelta(days=1)
    top = english.answered(ItemState(english.MAX_BOX, TODAY, TODAY), True, TODAY)
    assert top.box == english.MAX_BOX


# ---------------------------------------------------------------- the lesson


def test_the_first_lesson_meets_four_words_and_finds_them() -> None:
    lesson = english.plan_lesson(UNITS, {}, TODAY, rng=random.Random(1))
    kinds = [step.kind for step in lesson.steps]
    assert kinds == ["meet"] * 4 + ["find"] * 4
    assert lesson.unit is not None and lesson.unit.slug == "animals"
    assert lesson.new_words == 4
    met = [step.item for step in lesson.steps if step.kind == "meet"]
    assert met == ["animals/cat", "animals/dog", "animals/bird", "animals/fish"]
    for step in lesson.steps:
        if step.kind == "find":
            assert step.card is not None and step.card.pic in step.choices
            assert len(step.choices) == english.CHOICES
            assert len(set(step.choices)) == english.CHOICES


def test_due_words_come_first_the_most_overdue_first() -> None:
    progress = {
        **_met("animals/cat", due=TODAY - dt.timedelta(days=3)),
        **_met("animals/dog", due=TODAY),
        **_met("animals/bird", due=TODAY + dt.timedelta(days=2)),
    }
    lesson = english.plan_lesson(UNITS, progress, TODAY, rng=random.Random(2))
    reviewed = [s.item for s in lesson.steps if s.kind == "find" and s.item in progress]
    assert sorted(reviewed) == ["animals/cat", "animals/dog"], "not the bird: it is not due"
    assert lesson.steps[0].kind == "find"


def test_no_more_than_a_days_worth_of_new_words() -> None:
    progress = _met(
        *(f"animals/{w}" for w in ("cat", "dog", "bird", "fish", "rabbit", "duck")),
        first=TODAY,
        due=TODAY + dt.timedelta(days=1),
    )
    progress |= _met(
        *(f"colours/{w}" for w in ("red", "blue")),
        first=TODAY,
        due=TODAY + dt.timedelta(days=1),
    )
    lesson = english.plan_lesson(UNITS, progress, TODAY)
    assert lesson.new_words == 0, "eight met today already"


def test_once_the_words_are_met_the_lines_and_then_the_story() -> None:
    animals = UNITS[0]
    words = _met(
        *(animals.word_id(c) for c in animals.words), box=2, due=TODAY + dt.timedelta(days=3)
    )
    lesson = english.plan_lesson(UNITS, words, TODAY)
    assert [s.kind for s in lesson.steps] == ["line", "line"]
    assert lesson.steps[0].item == "animals/line-1"

    lines = _met(
        *(animals.line_id(n) for n in range(len(animals.lines))),
        box=1,
        due=TODAY + dt.timedelta(days=1),
    )
    lesson = english.plan_lesson(UNITS, words | lines, TODAY)
    assert [s.kind for s in lesson.steps] == ["story"]
    story = lesson.steps[0]
    assert story.item == "animals/story" and len(story.story) == len(animals.story)
    assert story.questions == animals.questions


def test_a_finished_unit_leads_to_the_next() -> None:
    lesson = english.plan_lesson(UNITS, _all_items(UNITS[0]), TODAY, rng=random.Random(3))
    assert lesson.unit is not None and lesson.unit.slug == UNITS[1].slug
    assert next(s.item for s in lesson.steps if s.kind == "meet") == "colours/red"


def test_a_story_comes_back_when_it_is_due() -> None:
    progress = _all_items(UNITS[0])
    progress["animals/story"] = ItemState(
        2, TODAY - dt.timedelta(days=1), TODAY - dt.timedelta(days=9)
    )
    lesson = english.plan_lesson(UNITS, progress, TODAY)
    assert lesson.steps[-1].kind == "story" and lesson.steps[-1].item == "animals/story"


def test_every_unit_done_leaves_review_only() -> None:
    progress: dict[str, ItemState] = {}
    for unit in UNITS:
        progress |= _all_items(unit)
    lesson = english.plan_lesson(UNITS, progress, TODAY)
    assert lesson.unit is None and lesson.steps == () and lesson.new_words == 0
    assert len(english.practice(UNITS, progress, rng=random.Random(4))) == 6


def test_a_step_says_what_the_page_needs_in_json() -> None:
    lesson = english.plan_lesson(UNITS, {}, TODAY, rng=random.Random(5))
    out = lesson.steps[0].as_json()
    assert out == {
        "kind": "meet",
        "item": "animals/cat",
        "unit": "animals",
        "card": {"en": "cat", "tr": "kedi", "pic": "🐱"},
    }


# ------------------------------------------------------------- what was said


@pytest.mark.parametrize(
    ("heard", "target", "matches"),
    [
        ("cat", "cat", True),
        ("A cat.", "cat", True),
        ("Kat!", "cat", True),
        ("the elephant", "cat", False),
        ("", "cat", False),
        ("grandmother", "grandmother", True),
        ("grand mother", "grandmother", False),
        ("Can I have water please", "Can I have water, please?", True),
        ("I like dog", "I like dogs.", True),
        ("hello", "Can I have water, please?", False),
    ],
)
def test_what_was_heard_is_compared_generously(heard: str, target: str, matches: bool) -> None:
    assert english.heard_matches(heard, target) is matches


# ---------------------------------------------------------------- the summary


def test_the_summary_counts_met_known_and_new_this_week() -> None:
    progress = {
        "animals/cat": ItemState(5, TODAY, TODAY - dt.timedelta(days=20)),
        "animals/dog": ItemState(2, TODAY, TODAY - dt.timedelta(days=2)),
        "animals/bird": ItemState(1, TODAY, TODAY),
        "animals/line-1": ItemState(1, TODAY, TODAY),
    }
    summary = english.summarize(
        UNITS, progress, since=TODAY - dt.timedelta(days=6), until=TODAY
    )
    assert summary.words_met == 3
    assert summary.words_known == 1 and summary.known_words[0].en == "cat"
    assert summary.new_in_period == 2, "lines are not words"
    assert summary.units_done == 0 and summary.current is UNITS[0]
    assert summary.words_total == sum(len(u.words) for u in UNITS)


def test_the_word_of_the_day_is_one_the_child_knows_and_changes_daily() -> None:
    assert english.word_of_the_day(UNITS, {}, TODAY) == UNITS[0].words[0]
    progress = _met(*(UNITS[0].word_id(c) for c in UNITS[0].words))
    today = english.word_of_the_day(UNITS, progress, TODAY)
    assert today in UNITS[0].words
    assert today == english.word_of_the_day(UNITS, progress, TODAY), "the same all day"
    days = {
        english.word_of_the_day(UNITS, progress, TODAY + dt.timedelta(days=n)) for n in range(8)
    }
    assert len(days) > 1
