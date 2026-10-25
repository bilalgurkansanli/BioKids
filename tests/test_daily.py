"""D-203: Bugünün sürprizi, the launcher's card of the day.

A child who sees the same screen every day gets bored, so the home screen
carries one small new thing a day inside a theme that changes every week. What
is worth pinning: that every card was written in both languages and fits the
strip, that a card is the same all day and different tomorrow, that siblings
get different cards on the same theme, that a task never sends a child to an
app they do not have, and that the home page draws it for both age bands with
or without a script.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from biokids_core import daily
from biokids_core.catalog import BUNDLED_CATALOG, load_catalog
from biokids_core.config import Config
from biokids_core.errors import ConfigError
from biokids_core.i18n import translator
from biokids_core.types import AgeBand
from biokids_shell.app import create_app
from test_shell import LAUNCHER, FakeShellDaemon, _RecordingLauncher, a_state

MONDAY = dt.date(2026, 10, 5)
YOUNG, OLDER = AgeBand.YOUNG.value, AgeBand.OLDER.value
EVERY_APP = [
    {"slug": e.slug, "name": e.name_tr, "icon": e.icon}
    for e in load_catalog(BUNDLED_CATALOG)
    if e.kind == "app"
]


@pytest.fixture(scope="module")
def corpus() -> daily.Corpus:
    return daily.load_corpus()


def days(start: dt.date, count: int) -> Iterator[dt.date]:
    for n in range(count):
        yield start + dt.timedelta(days=n)


def chosen(
    day: dt.date,
    *,
    child: int = 1,
    band: str = YOUNG,
    tiles: list[dict[str, str]] | None = None,
) -> daily.Surprise:
    item = daily.choose(
        day,
        child_id=child,
        band=band,
        startable=[t["slug"] for t in (EVERY_APP if tiles is None else tiles)],
        corpus=daily.load_corpus(),
    )
    assert item is not None
    return item


# ------------------------------------------------------------- the corpus


def test_there_is_plenty_to_find(corpus: daily.Corpus) -> None:
    """The owner asked for at least 120 cards."""
    assert len(corpus.items) >= 120
    assert {item.kind for item in corpus.items} == set(daily.KINDS)
    assert len(corpus.themes) >= 8


def test_every_card_is_in_both_languages_and_fits(corpus: daily.Corpus) -> None:
    for item in corpus.items:
        for language in ("tr", "en"):
            text = getattr(item, f"text_{language}")
            assert text and len(text) <= daily.TEXT_MAX, (item.id, language)
            assert len(getattr(item, f"more_{language}")) <= daily.MORE_MAX, item.id
            assert len(getattr(item, f"title_{language}")) <= daily.TITLE_MAX, item.id
            assert len(getattr(item, f"answer_{language}")) <= daily.ANSWER_MAX, item.id
            assert text[-1] in ".!?…", f"{item.id}: {language} text is not a sentence"
        assert bool(item.more_tr) == bool(item.more_en), item.id
        assert item.emoji, item.id


def test_the_older_bands_card_stays_two_lines(corpus: daily.Corpus) -> None:
    """Measured at 1280 x 800: two lines of the 8-10 card leave room for three
    rows of tiles under it; a third line makes the grid scroll. About a hundred
    characters fit on a line beside the day's name, fewer beside a button."""
    for locale in ("tr", "en"):
        tr = translator(locale)
        for item in corpus.items:
            heading = tr(f"daily.kind.{item.kind}")
            if item.title(locale):
                heading += f": {item.title(locale)}"
            body = " ".join(filter(None, (heading, item.text(locale), item.more(locale))))
            limit = 140 if (item.answer(locale) or item.app) else 185
            assert len(body) <= limit, f"{item.id} ({locale}) is {len(body)} characters"


def test_every_theme_fills_a_week_for_both_bands(corpus: daily.Corpus) -> None:
    """Seven cards a theme for each band, without counting tasks for apps a child
    may not have: a week never shows the same card twice."""
    for theme in corpus.themes:
        assert theme.label_tr and theme.label_en and theme.emoji
        for band in daily.BANDS:
            own = [i for i in corpus.dealt(theme.slug) if band in i.bands and not i.app]
            assert len(own) >= 7, (theme.slug, band)


def test_the_three_newer_themes_are_as_full_as_the_first_twelve(corpus: daily.Corpus) -> None:
    """D-213: online safety, feelings and jobs, after the first twelve, each
    with the mix of the others - a dozen and more cards, every kind but the
    animal, and a task that opens one of the child's tiles."""
    order = [t.slug for t in corpus.themes]
    assert order[-3:] == ["internet", "feelings", "jobs"]
    assert len(order) == 15
    sizes = [len([i for i in corpus.items if i.theme == t]) for t in order]
    for slug in order[-3:]:
        cards = [i for i in corpus.items if i.theme == slug]
        assert min(sizes[:12]) <= len(cards) <= max(sizes[:12]), slug
        assert {i.kind for i in cards} == {"fact", "riddle", "word", "challenge"}, slug
        assert sum(i.kind == "riddle" for i in cards) >= 2, slug
        assert any(i.app for i in cards), f"{slug}: no task opens a tile"


def test_online_safety_is_said_without_frightening_anyone(corpus: daily.Corpus) -> None:
    """What to do and who to tell, never what could happen (D-213)."""
    scary = re.compile(
        r"tehlik|korkunç|kötü|kaçır|zarar|yabancı|dikkat et|asla|"
        r"danger|scary|bad |kidnap|harm|stranger|never ",
        re.IGNORECASE,
    )
    for item in corpus.items:
        if item.theme != "internet":
            continue
        for text in (item.text_tr, item.more_tr, item.text_en, item.more_en):
            assert not scary.search(text), f"{item.id}: {text}"


def test_a_riddle_has_an_answer_and_does_not_give_it_away(corpus: daily.Corpus) -> None:
    for item in corpus.items:
        if item.kind != "riddle":
            assert not item.answer_tr and not item.answer_en, item.id
            continue
        assert item.emoji == daily.RIDDLE_EMOJI, f"{item.id}: its picture would be a hint"
        for language in ("tr", "en"):
            question = getattr(item, f"text_{language}")
            answer = re.sub(
                r"^(a|an|the|your) ", "", getattr(item, f"answer_{language}").lower()
            )
            assert question.endswith("?"), item.id
            assert not re.search(rf"\b{re.escape(answer)}\b", question.lower()), (
                f"{item.id}: the answer is in the question"
            )


def test_animals_and_words_carry_their_name(corpus: daily.Corpus) -> None:
    for item in corpus.items:
        named = item.kind in {"animal", "word"}
        assert bool(item.title_tr) is named and bool(item.title_en) is named, item.id


def test_every_task_names_the_tile_it_opens(corpus: daily.Corpus) -> None:
    """A task points at a catalogue app, by the name on the child's tile, at an
    age that app is offered for."""
    apps = {e.slug: e for e in load_catalog(BUNDLED_CATALOG)}
    for item in corpus.items:
        if not item.app:
            continue
        assert item.kind == "challenge", item.id
        entry = apps.get(item.app)
        assert entry is not None and entry.kind == "app", f"{item.id}: no app {item.app!r}"
        assert entry.name_tr in item.text_tr, f"{item.id}: says nothing of {entry.name_tr}"
        assert entry.name_en in item.text_en, f"{item.id}: says nothing of {entry.name_en}"


def test_the_child_is_addressed_as_sen(corpus: daily.Corpus) -> None:
    """UX.md §4: "siz" is for the parent. A task is in the second person singular."""
    formal = re.compile(
        r"\b(siz|sizin|size|sizi|bakın|deneyin|çizin|seçin|dinleyin)\b|\w+(ınız|iniz|unuz|ünüz)\b"
    )
    for item in corpus.items:
        for text in (item.text_tr, item.more_tr):
            assert not formal.search(text.lower()), f"{item.id}: {text}"


def test_a_broken_card_is_refused_by_the_loader() -> None:
    """A mistake in the file must fail the suite, not reach a child's screen."""
    theme = {
        "slug": "s",
        "emoji": "🚀",
        "label_tr": "Uzay",
        "label_en": "Space",
        "accent": "teal",
    }
    good = {
        "id": "x",
        "theme": "s",
        "kind": "fact",
        "emoji": "☀️",
        "text_tr": "A.",
        "text_en": "A.",
    }
    daily.parse({"theme": [theme], "item": [good]})
    for broken in (
        {**good, "text_en": ""},
        {**good, "kind": "joke"},
        {**good, "theme": "nowhere"},
        {**good, "text_tr": "a" * (daily.TEXT_MAX + 1)},
        {**good, "answer_tr": "B", "answer_en": "B"},
        {**good, "kind": "riddle", "text_tr": "A?", "text_en": "A?"},
        {**good, "kind": "animal"},
        {**good, "app": "tuxpaint"},
        {**good, "bands": ["4-6"]},
        {**good, "more_tr": "Yalnız Türkçe."},
    ):
        with pytest.raises(ConfigError):
            daily.parse({"theme": [theme], "item": [broken]})
    with pytest.raises(ConfigError):
        daily.parse({"theme": [theme], "item": [good, good]})


# ------------------------------------------------------------- the choice


def test_a_day_has_one_card_and_tomorrow_another(corpus: daily.Corpus) -> None:
    assert chosen(MONDAY) == chosen(MONDAY), "the same all day"
    week = [chosen(day).id for day in days(MONDAY, 7)]
    assert len(set(week)) == 7, "seven different cards in a week"
    kinds = {chosen(day).kind for day in days(MONDAY, 7)}
    assert len(kinds) >= 4, f"a week of only {kinds}"


def test_the_theme_changes_every_monday(corpus: daily.Corpus) -> None:
    themes = [daily.theme_for(day, corpus).slug for day in days(MONDAY, 7 * len(corpus.themes))]
    for week in range(len(corpus.themes)):
        assert len(set(themes[week * 7 : week * 7 + 7])) == 1, "one theme all week"
    order = [t.slug for t in corpus.themes]
    start = order.index(themes[0])
    assert [themes[w * 7] for w in range(len(order))] == order[start:] + order[:start]
    assert daily.theme_for(MONDAY - dt.timedelta(days=1), corpus).slug != themes[0]
    for day in days(MONDAY, 7 * len(corpus.themes)):
        assert chosen(day).theme == daily.theme_for(day, corpus).slug


def test_nothing_repeats_until_every_theme_has_come_round(corpus: daily.Corpus) -> None:
    span = 7 * len(corpus.themes)
    for band in daily.BANDS:
        for tiles in ([], EVERY_APP):
            seen = [chosen(day, band=band, tiles=tiles).id for day in days(MONDAY, span)]
            assert len(set(seen)) == span, (band, len(tiles))


def test_a_year_shows_most_of_the_corpus(corpus: daily.Corpus) -> None:
    seen = {chosen(day, band=OLDER).id for day in days(dt.date(2026, 1, 1), 365)}
    assert len(seen) >= 120


def test_siblings_share_the_theme_but_not_the_card(corpus: daily.Corpus) -> None:
    for day in days(MONDAY, 28):
        first, second = chosen(day, child=1), chosen(day, child=2)
        assert first.theme == second.theme
        assert first.id != second.id


def test_a_task_only_points_at_an_app_the_child_has() -> None:
    paint = [{"slug": "tuxpaint", "name": "Boya", "icon": "brush"}]
    for band in daily.BANDS:
        for day in days(MONDAY, 365):
            assert not chosen(day, band=band, tiles=[]).app
            assert chosen(day, band=band, tiles=paint).app in {"", "tuxpaint"}
    pointed = [
        card
        for day in days(MONDAY, 84)
        if (card := daily.card_for(day, child_id=1, band=YOUNG, locale="tr", tiles=paint))
        and card.app
    ]
    assert pointed, "Boya's tasks do come up"
    assert all(
        card.app == {"slug": "tuxpaint", "name": "Boya", "icon": "brush", "kind": "app"}
        for card in pointed
    )


def test_the_young_band_gets_one_sentence_and_the_older_a_little_more(
    corpus: daily.Corpus,
) -> None:
    item = next(i for i in corpus.items if i.id == "space-sun-star")
    day = next(d for d in days(MONDAY, 365) if chosen(d, band=YOUNG).id == item.id)
    young = daily.card_for(day, child_id=1, band=YOUNG, locale="tr", tiles=EVERY_APP)
    assert young is not None and young.text == item.text_tr
    older_day = next(d for d in days(MONDAY, 365) if chosen(d, band=OLDER).id == item.id)
    older = daily.card_for(older_day, child_id=1, band=OLDER, locale="en", tiles=EVERY_APP)
    assert older is not None and older.text == f"{item.text_en} {item.more_en}"
    assert older.theme_label == "Space week"


def test_a_broken_state_costs_the_card_and_nothing_else() -> None:
    state: dict[str, Any] = {"child_id": "not a number", "tiles": []}
    assert daily.for_launcher(state, MONDAY, lambda _tile: True) is None
    card = daily.for_launcher(a_state(), MONDAY, lambda _tile: False)
    assert card is not None and not card.app, "no tile can start, so no task points at one"


# ----------------------------------------------------------- the home page


def escaped(text: str) -> str:
    """As the template writes it: Jinja escapes apostrophes and quotes."""
    return str(escape(text))


def card_of(page: str) -> str:
    return page.split('id="daily"')[1].split("</section>")[0]


@pytest.fixture
def home(config: Config, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The launcher's home page on a chosen day, for a chosen band."""
    today = {"day": MONDAY}
    real = daily.for_launcher
    monkeypatch.setattr(
        daily,
        "for_launcher",
        lambda state, _today, can_start: real(state, today["day"], can_start),
    )

    def render(
        day: dt.date, band: str = YOUNG, tiles: list[dict[str, Any]] | None = None
    ) -> str:
        today["day"] = day
        state = a_state(age_band=band)
        if tiles is not None:
            state["tiles"] = tiles
        app = create_app(config)
        app.state.shell.client = FakeShellDaemon(state)  # type: ignore[assignment]
        app.state.shell.launcher = _RecordingLauncher(config)
        return TestClient(app, base_url=LAUNCHER).get("/").text

    return render


def _day_with(
    kind: str, band: str = YOUNG, tiles: list[dict[str, Any]] | None = None
) -> dt.date:
    tiles = a_state()["tiles"] if tiles is None else tiles
    return next(d for d in days(MONDAY, 365) if chosen(d, band=band, tiles=tiles).kind == kind)


def test_the_home_page_draws_the_card_under_the_header(home: Any) -> None:
    page = home(MONDAY)
    assert 'id="daily"' in page
    assert page.index("</header>") < page.index('id="daily"') < page.index('class="tiles"')
    assert "Bugünün sürprizi" in page
    assert "/static/daily.css?v=" in page and "/static/daily.js?v=" in page


def test_the_young_band_hears_it_and_the_older_reads_more(home: Any) -> None:
    tiles = a_state()["tiles"]
    day = _day_with("fact")
    item = chosen(day, band=YOUNG, tiles=tiles)
    young = card_of(home(day, YOUNG))
    assert f'data-speak="Biliyor muydun? {escaped(item.text_tr)}"' in young
    assert escaped(item.more_tr) not in young, "four to seven get the one sentence"

    older_day = next(
        d
        for d in days(MONDAY, 365)
        if (i := chosen(d, band=OLDER, tiles=tiles)).kind == "fact" and i.more_tr
    )
    older_item = chosen(older_day, band=OLDER, tiles=tiles)
    older = card_of(home(older_day, OLDER))
    assert escaped(f"{older_item.text_tr} {older_item.more_tr}") in older
    assert "data-speak" not in older, "eight to ten read for themselves (UX.md §2.2)"


def test_an_animal_is_named_before_it_is_described(home: Any) -> None:
    day = _day_with("animal")
    item = chosen(day, tiles=a_state()["tiles"])
    card = card_of(home(day))
    assert f"Günün hayvanı: <strong>{escaped(item.title_tr)}</strong>" in card
    assert f'data-speak="Günün hayvanı: {escaped(item.title_tr)}. ' in card


def test_a_riddle_opens_its_answer_without_a_script(home: Any) -> None:
    day = _day_with("riddle")
    item = chosen(day, tiles=a_state()["tiles"])
    card = card_of(home(day))
    assert "<details" in card and "<summary" in card and "Cevabı gör" in card
    assert escaped(item.answer_tr) in card
    assert f'data-speak="Cevap: {escaped(item.answer_tr)}"' in card
    assert daily.RIDDLE_EMOJI in card


def test_a_task_is_a_button_to_the_childs_own_tile(home: Any) -> None:
    paint = [t for t in a_state()["tiles"] if t["slug"] == "tuxpaint"]
    day = next(d for d in days(MONDAY, 365) if chosen(d, tiles=paint).app == "tuxpaint")
    item = chosen(day, tiles=paint)
    card = card_of(home(day, YOUNG, tiles=paint))
    assert 'href="/launch/tuxpaint"' in card
    assert 'aria-label="Aç: Boya"' in card
    assert "data-slug" not in card, "the card is not one of the tiles"
    assert escaped(item.text_tr) in card


def test_no_tile_means_no_task_pointing_at_one(home: Any) -> None:
    """D-164: the grid shows nothing a parent did not enable; nor does the card."""
    for day in days(MONDAY, 21):
        card = card_of(home(day, YOUNG, tiles=[]))
        assert "/launch/" not in card and "Boya" not in card


def test_the_card_reads_without_javascript(home: Any) -> None:
    """The sentence is in the page; only the put-away button needs the script."""
    page = home(MONDAY)
    item = chosen(MONDAY, tiles=a_state()["tiles"])
    assert escaped(item.text_tr) in card_of(page)
    assert 'id="daily-hide" type="button" hidden' in page


def test_english_for_an_english_child(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    real = daily.for_launcher
    monkeypatch.setattr(
        daily, "for_launcher", lambda state, _today, can_start: real(state, MONDAY, can_start)
    )
    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(a_state(locale="en"))  # type: ignore[assignment]
    page = TestClient(app, base_url=LAUNCHER).get("/").text
    item = chosen(MONDAY, tiles=a_state()["tiles"])
    assert escaped("Today's surprise") in page and escaped(item.text_en) in page


def test_a_card_that_cannot_be_chosen_leaves_the_tiles_alone(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any) -> daily.Corpus:
        raise ConfigError("unreadable")

    monkeypatch.setattr(daily, "load_corpus", broken)
    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(a_state())  # type: ignore[assignment]
    page = TestClient(app, base_url=LAUNCHER).get("/").text
    assert 'id="daily"' not in page and "Boya" in page
