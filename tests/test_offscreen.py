"""D-231: Şimdi ne yapsak? - something to do away from the screen, when it goes.

The cards are people's sentences, so most of what can go wrong is in the file:
a sentence too long for Zio to say in one breath, a card that sends the child
back to a screen, an age band left with nothing to show. Then the choice: the
same card all day, another tomorrow, siblings apart, a break that gets the
child up before it calms them, and a broken file that costs the suggestion and
never the screen.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from biokids_core import offscreen
from biokids_core.errors import ConfigError

DAY = dt.date(2026, 10, 23)
SCREEN_WORDS = (
    "ekran",
    "bilgisayar",
    "tablet",
    "telefon",
    "screen",
    "computer",
    "tablet",
    "phone",
    "video",
)


def test_the_bundled_cards_load() -> None:
    cards = offscreen.load()
    assert len(cards) >= 30
    assert {card.moment for card in cards} == set(offscreen.MOMENTS)


@pytest.mark.parametrize("card", offscreen.load(), ids=lambda card: card.id)
def test_every_card_keeps_the_rules(card: offscreen.Card) -> None:
    for text in (card.text_tr, card.text_en):
        assert 0 < len(text) <= offscreen.TEXT_MAX
        assert text.endswith((".", "!", "?")), "a sentence Zio can end"
        lowered = text.lower()
        assert not any(word in lowered for word in SCREEN_WORDS), "never back to a screen"
    assert " siz " not in f" {card.text_tr.lower()} ", "the child is sen (UX.md §4)"


@pytest.mark.parametrize("moment", offscreen.MOMENTS)
@pytest.mark.parametrize("band", offscreen.BANDS)
def test_every_moment_has_cards_for_every_age(moment: str, band: str) -> None:
    pool = [c for c in offscreen.load() if c.moment == moment and band in c.bands]
    assert len(pool) >= offscreen.BREAK_MOVES


def test_the_same_card_all_day_and_another_tomorrow() -> None:
    today = offscreen.choose("end", "4-7", DAY, 1)
    assert today == offscreen.choose("end", "4-7", DAY, 1)
    days = {
        offscreen.choose("end", "4-7", DAY + dt.timedelta(days=n), 1)[0].id for n in range(7)
    }
    assert len(days) == 7, "a week without the same suggestion twice"


def test_siblings_at_one_computer_get_different_cards() -> None:
    first = offscreen.choose("end", "4-7", DAY, 1)[0]
    second = offscreen.choose("end", "4-7", DAY, 2)[0]
    assert first.id != second.id


def test_a_break_gets_the_child_up_before_it_calms_them() -> None:
    for n in range(14):
        moves = offscreen.choose("break", "8-10", DAY + dt.timedelta(days=n), 1, count=3)
        assert len(moves) == 3
        assert len({card.id for card in moves}) == 3
        kinds = [card.kind for card in moves]
        assert kinds == sorted(kinds, key=lambda kind: kind == "calm"), "calm comes last"


def test_a_card_for_older_children_is_not_dealt_to_the_young() -> None:
    older_only = {c.id for c in offscreen.load() if c.bands == ("8-10",)}
    assert older_only
    for n in range(60):
        dealt = offscreen.choose("end", "4-7", DAY + dt.timedelta(days=n), n % 4)[0]
        assert dealt.id not in older_only


@pytest.mark.parametrize(
    ("screen", "moment", "count"),
    [
        ("time_up.html", "end", 1),
        ("window_closed.html", "end", 1),
        ("good_night.html", "night", 1),
        ("break.html", "break", 3),
        ("timeout.html", None, 0),
        ("home.html", None, 0),
    ],
)
def test_each_screen_gets_its_moment(screen: str, moment: str | None, count: int) -> None:
    state = {"child_id": 1, "age_band": "4-7", "locale": "tr"}
    cards = offscreen.for_screen(screen, state, DAY)
    assert len(cards) == count
    by_id = {c.id: c for c in offscreen.load()}
    for card in cards:
        assert by_id[card["id"]].moment == moment
        assert card["text"] == by_id[card["id"]].text_tr
        assert card["emoji"]


def test_english_for_an_english_child() -> None:
    cards = offscreen.for_screen("time_up.html", {"child_id": 1, "locale": "en"}, DAY)
    assert cards[0]["text"] == {c.id: c for c in offscreen.load()}[cards[0]["id"]].text_en


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "cards.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_broken_file_is_refused_whole(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[[card]]\nid = "x"\nmoment = "sometime"\nkind = "play"\nemoji = "🧸"\n'
        'text_tr = "Oyna."\ntext_en = "Play."\n',
    )
    with pytest.raises(ConfigError):
        offscreen.load(path)


def test_a_sentence_too_long_is_refused(tmp_path: Path) -> None:
    long = "Çok " * 40 + "uzun."
    path = _write(
        tmp_path,
        f'[[card]]\nid = "x"\nmoment = "end"\nkind = "play"\nemoji = "🧸"\n'
        f'text_tr = "{long}"\ntext_en = "Long."\n',
    )
    with pytest.raises(ConfigError):
        offscreen.load(path)


def test_a_broken_file_costs_the_cards_not_the_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> tuple[offscreen.Card, ...]:
        raise ConfigError("cards.toml: not toml")

    monkeypatch.setattr(offscreen, "load", broken)
    assert offscreen.for_screen("time_up.html", {"child_id": 1}, DAY) == []
