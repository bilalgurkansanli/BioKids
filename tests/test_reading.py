"""D-241: Okuma köşesi - learning to read Turkish in first grade's order.

The promise that matters most is the one the file makes: a child never sees a
letter they have not met. So the content is checked first - every group, every
syllable, word and sentence - then the lesson: letters met before they are
asked for, choices only from what is known, words read before they are heard,
and the boxes moving as the English corner's do. Then the launcher, the
daemon, and the parent's card.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import test_panel
from biokids_core import reading, repo
from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import Child, Database
from biokids_core.english import ItemState
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer, RecordingRunner
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.supervisor import Supervisor
from test_panel import login
from test_shell import LAUNCHER, FakeShellDaemon, a_state

client = test_panel.client
daemon = test_panel.daemon
family_ready = test_panel.family_ready

TODAY = dt.date(2026, 10, 23)
GROUPS = reading.load_groups()


# ---------------------------------------------------------------- the content


def test_the_five_groups_in_first_grades_order() -> None:
    assert [g.letters for g in GROUPS] == [
        tuple(reading.Letter(c, *_rest(g, c)) for c in chars)
        for g, chars in zip(
            GROUPS, ("elakin", "omutüy", "örıdsb", "zçgşcp", "hğvfj"), strict=True
        )
    ]
    every = "".join(letter.char for g in GROUPS for letter in g.letters)
    assert sorted(every) == sorted(reading.TURKISH_LETTERS), (
        "every letter of the alphabet, once"
    )


def _rest(group: reading.Group, char: str) -> tuple[str, str, str]:
    letter = next(letter for letter in group.letters if letter.char == char)
    return letter.hint, letter.word, letter.pic


def test_no_letter_is_seen_before_it_is_met() -> None:
    known: set[str] = set()
    for group in GROUPS:
        known |= {letter.char for letter in group.letters}
        for text in (
            *group.syllables,
            *(w.text for w in group.words),
            *(s.text for s in group.sentences),
            *(letter.word for letter in group.letters),
        ):
            letters = {ch for ch in reading.lower(text) if ch not in reading.PUNCTUATION}
            assert letters <= known, f"{group.slug}: {text!r}"


def test_a_file_that_breaks_the_rule_is_refused() -> None:
    bad = (
        '[[group]]\nslug = "g1"\ntitle = "a l"\nletters = ["a", "l"]\nsyllables = ["la"]\n'
        '[[group.letter]]\nchar = "a"\nhint = "a"\nword = "al"\npic = "🖐️"\n'
        '[[group.letter]]\nchar = "l"\nhint = "la"\nword = "al"\npic = "🖐️"\n'
        '[[group.word]]\ntext = "kal"\nsyllables = "kal"\npic = "🏠"\n'
    )
    with pytest.raises(ValueError, match="before it is met"):
        reading.parse_groups(bad)


def test_every_word_is_split_as_it_is_read() -> None:
    for group in GROUPS:
        for word in group.words:
            assert word.syllables.replace("-", "") == word.text
            assert word.pic


def test_turkish_capitals() -> None:
    assert reading.upper("i") == "İ" and reading.upper("ı") == "I"
    assert reading.lower("İI") == "iı"


# ----------------------------------------------------------------- the lesson


def test_the_first_lesson_meets_two_letters_and_asks_only_about_them() -> None:
    lesson = reading.plan_lesson(GROUPS, {}, TODAY, rng=random.Random(3))
    kinds = [step.kind for step in lesson.steps]
    assert kinds == ["letter", "letter", "sound", "sound"]
    for step in lesson.steps:
        assert set(step.choices) <= {"e", "l"}, "never a letter not met"
    sound = next(step for step in lesson.steps if step.kind == "sound" and step.answer == "l")
    assert sound.say.startswith("l"), "the syllable asked about starts with the letter"


def _met(*items: str, box: int = 2, first: dt.date | None = None) -> dict[str, ItemState]:
    return {
        item: ItemState(
            box, TODAY + dt.timedelta(days=5), first or TODAY - dt.timedelta(days=3)
        )
        for item in items
    }


def test_syllables_come_once_a_groups_letters_are_met() -> None:
    letters = [GROUPS[0].letter_id(letter) for letter in GROUPS[0].letters]
    lesson = reading.plan_lesson(GROUPS, _met(*letters), TODAY, rng=random.Random(1))
    assert {step.kind for step in lesson.steps} == {"syllable"}
    assert len(lesson.steps) == reading.NEW_SYLLABLES_PER_LESSON


def test_a_word_is_read_before_it_is_heard() -> None:
    group = GROUPS[0]
    progress = _met(
        *(group.letter_id(letter) for letter in group.letters),
        *(group.syllable_id(s) for s in group.syllables),
    )
    lesson = reading.plan_lesson(GROUPS, progress, TODAY, rng=random.Random(2))
    words = [step for step in lesson.steps if step.kind == "word"]
    assert words
    for step in words:
        assert step.show and step.answer in step.choices
        known = {letter.char for letter in group.letters}
        for pic in step.choices:
            word = next(w for g in GROUPS for w in g.words if w.pic == pic)
            assert set(word.text) <= known, "a picture of a word the child could read"


def test_whats_due_comes_back_first() -> None:
    group = GROUPS[0]
    due = {group.letter_id(group.letters[0]): ItemState(1, TODAY, TODAY - dt.timedelta(days=1))}
    lesson = reading.plan_lesson(GROUPS, due, TODAY, rng=random.Random(4))
    assert lesson.steps[0].kind == "sound" and lesson.steps[0].answer == "e"


def test_a_day_has_a_limit_of_new_things() -> None:
    met_today = {f"g9/x{n}": ItemState(1, TODAY, TODAY) for n in range(reading.NEW_PER_DAY)}
    lesson = reading.plan_lesson(GROUPS, met_today, TODAY, rng=random.Random(5))
    assert lesson.new_items == 0


def test_the_summary() -> None:
    group = GROUPS[0]
    progress = _met(*(group.letter_id(letter) for letter in group.letters), box=4)
    summary = reading.summarize(GROUPS, progress)
    assert summary.letters_met == 6 and summary.letters_total == 29
    assert summary.letters_known == tuple("elakin")
    assert summary.current is group


# --------------------------------------------------------------------- daemon


@pytest.fixture
def supervisor(db: Database, clock: FakeClock, config: Config, child: Child) -> Supervisor:
    return Supervisor(
        database=db,
        bus=EventBus(queue_size=64, history_size=64),
        watcher=SessionWatcher(FakeSessionSource()),
        enforcer=Enforcer(config, RecordingRunner()),
        clock=clock,
        config=config,
    )


async def test_an_answer_moves_the_box_and_only_for_real_items(
    supervisor: Supervisor, session: Session
) -> None:
    await supervisor.start()
    assert supervisor.reading_answer(1, "g1/letter-e", True) == 1
    # Met and asked in the same lesson: still the first box that day (D-250).
    assert supervisor.reading_answer(1, "g1/letter-e", True) == 1
    assert supervisor.reading_answer(1, "../../etc", True) is None
    state = supervisor.reading_state(1)
    assert state["items"]["g1/letter-e"][0] == 1
    assert repo.reading_progress(session, 1)["g1/letter-e"].box == 1


# ---------------------------------------------------------------------- shell


@pytest.fixture
def shell(config: Config) -> tuple[TestClient, FakeShellDaemon]:
    from biokids_shell.app import create_app

    state = a_state()
    state["tiles"].append(
        {
            "slug": "okuma",
            "name": "Okuma",
            "icon": "letters",
            "kind": "builtin",
            "category": "learning",
        }
    )
    fake = FakeShellDaemon(state)
    fake.answers: list[tuple[str, bool]] = []  # type: ignore[misc]

    async def reading_state() -> dict[str, object]:
        return {"today": TODAY.isoformat(), "items": {}}

    async def reading_answer(item: str, right: bool) -> int:
        fake.answers.append((item, right))  # type: ignore[attr-defined]
        return 1

    fake.reading_state = reading_state  # type: ignore[method-assign]
    fake.reading_answer = reading_answer  # type: ignore[method-assign]
    app = create_app(config)
    app.state.shell.client = fake  # type: ignore[assignment]
    return TestClient(app, base_url=LAUNCHER), fake


def test_the_door_and_the_lesson(shell: tuple[TestClient, FakeShellDaemon]) -> None:
    client_, fake = shell
    door = client_.get("/okuma").text
    assert "Bugünün dersi" in door and door.count("okuma__group") >= 5
    lesson = client_.get("/okuma/lesson").text
    assert 'id="okuma-data"' in lesson and '"kind": "letter"' in lesson
    assert client_.post(
        "/okuma/answer", json={"item": "g1/letter-e", "right": True}
    ).json() == {
        "ok": True,
        "box": 1,
    }
    assert fake.answers == [("g1/letter-e", True)]  # type: ignore[attr-defined]
    forged = client_.post(
        "/okuma/answer",
        json={"item": "g1/letter-e"},
        headers={"Origin": "https://evil.example"},
    )
    assert forged.status_code == 403


def test_minutes_in_the_corner_are_reported_as_its_own(
    shell: tuple[TestClient, FakeShellDaemon],
) -> None:
    client_, fake = shell
    assert client_.post("/okuma/active", json={}).json() == {"ok": True}
    assert fake.activity[-1][0] == "okuma"


def test_no_tile_no_corner(config: Config) -> None:
    from biokids_shell.app import create_app

    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(a_state())  # type: ignore[assignment]
    assert (
        TestClient(app, base_url=LAUNCHER).get("/okuma", follow_redirects=False).status_code
        == 303
    )


# ---------------------------------------------------------------------- panel


def test_the_parent_reads_how_far_the_child_is(
    client: TestClient, family_ready: None, db: Database
) -> None:
    # Today, not TODAY: Analiz shows the week the real clock is in, and a fixed
    # date fell out of it the Monday after.
    with db.session() as session:
        for item in ("g1/letter-e", "g1/letter-l"):
            repo.record_reading_answer(session, 1, item, True, dt.date.today())
        session.commit()
    login(client)
    page = client.get("/insights?child=1").text
    assert "Okuma köşesi" in page and "29 harften 2 tanesiyle" in page


def test_a_letter_card_writes_no_letter_not_yet_met() -> None:
    """The picture is always there; the word is written only when every letter
    of it is known, and Zio says it either way."""
    lesson = reading.plan_lesson(GROUPS, {}, TODAY, rng=random.Random(7))
    cards = [step for step in lesson.steps if step.kind == "letter"]
    known = {"e", "l"}
    for card in cards:
        written = {ch for ch in card.syllables if ch not in reading.PUNCTUATION}
        assert written <= known, card
        assert card.pic
    assert any("lale" in card.say for card in cards), "the word is still heard"


def test_a_meeting_card_writes_no_letter_still_to_come() -> None:
    """D-250: "el" was written on e's card before l was met in the same lesson."""
    for seed in range(40):
        lesson = reading.plan_lesson(GROUPS, {}, TODAY, rng=random.Random(seed))
        met: set[str] = set()
        for step in lesson.steps:
            if step.kind == "letter":
                met.add(step.show)
                if step.syllables:
                    letters = {ch for ch in step.syllables.lower() if ch.isalpha()}
                    assert letters <= met, (seed, step.show, step.syllables)
