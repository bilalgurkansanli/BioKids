"""D-211: Çıkartma albümüm, a sticker for each day a child spent time learning.

What is worth pinning is what a child and a parent would notice: that a
quarter of an hour of learning apps earns the day's sticker and a minute less
does not; that games never do; that a day is the family's own calendar day, so
ten minutes either side of midnight are not one sticker; that the sticker is
never minutes; that the collection is varied and every sticker has a name in
both languages; that the launcher shows it all without a script; and that
Zio celebrates a new sticker once a day - not on every load, and not again
after a restart.
"""

from __future__ import annotations

import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from biokids_core import album, insights
from biokids_core.album import GOAL_SECONDS, STICKERS, sticker_for
from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import Child, Database, Policy
from biokids_core.db.models import UsageTick
from biokids_core.i18n import translator
from biokids_core.insights import Context, Facts, Period, Tone, highlights, span_for, summarize
from biokids_core.types import AgeBand, Reason
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer, RecordingRunner
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.supervisor import Supervisor
from biokids_shell.app import CHEER_FILE, create_app
from helpers import give_app
from test_shell import LAUNCHER, FakeShellDaemon, _RecordingLauncher, _settle, a_state

WEDNESDAY = dt.date(2026, 10, 21)
MONDAY = dt.date(2026, 10, 19)
TICK = 10


def iso(*days: dt.date) -> list[str]:
    return [day.isoformat() for day in days]


def album_state(
    today: dt.date = WEDNESDAY,
    days: list[dt.date] | None = None,
    today_seconds: int = 0,
    **overrides: Any,
) -> dict[str, Any]:
    """The launcher's state with an album in it, as the daemon sends it."""
    earned = list(days or [])
    if today_seconds >= GOAL_SECONDS:
        earned.append(today)
    state = a_state(**overrides)
    state["album"] = {
        "today": today.isoformat(),
        "days": iso(*sorted(earned, reverse=True)),
        "today_seconds": today_seconds,
        "goal_seconds": GOAL_SECONDS,
    }
    return state


# ---------------------------------------------------------------- the rule


def test_a_day_has_one_sticker_and_the_next_day_another() -> None:
    assert sticker_for(WEDNESDAY) == sticker_for(WEDNESDAY)
    week = [sticker_for(MONDAY + dt.timedelta(days=n)) for n in range(7)]
    assert len({s.slug for s in week}) == 7
    assert all(a.theme != b.theme for a, b in itertools.pairwise(week)), (
        "neighbouring days come from different sets"
    )


def test_no_sticker_comes_back_until_every_one_has_come() -> None:
    """A growing album looks varied: the whole set before the first double."""
    assert len({s.slug for s in STICKERS}) == len(STICKERS) >= 50
    assert len({s.emoji for s in STICKERS}) == len(STICKERS)
    run = [sticker_for(WEDNESDAY + dt.timedelta(days=n)).slug for n in range(len(STICKERS))]
    assert sorted(run) == sorted(s.slug for s in STICKERS)


def test_every_sticker_has_a_name_zio_can_say() -> None:
    for locale in ("tr", "en"):
        tr = translator(locale)
        names = [tr(sticker.key) for sticker in STICKERS]
        assert all(name and not name.startswith("album.") for name in names), locale
        assert len(set(names)) == len(names), f"two stickers share a name in {locale}"


def test_a_quarter_of_an_hour_earns_the_days_sticker() -> None:
    seconds = {MONDAY: GOAL_SECONDS - 1, WEDNESDAY: GOAL_SECONDS, dt.date(2026, 10, 1): 3600}
    assert album.earned_days(seconds) == [WEDNESDAY, dt.date(2026, 10, 1)]
    assert GOAL_SECONDS == 15 * 60


def test_the_album_is_read_from_the_launchers_state() -> None:
    found = album.from_state(album_state(days=[MONDAY], today_seconds=600))
    assert found is not None
    assert [e.day for e in found.earned] == [MONDAY]
    assert not found.earned_today and found.count == 1
    assert found.minutes_left == 5 and found.goal_minutes == 15

    found = album.from_state(album_state(days=[MONDAY], today_seconds=GOAL_SECONDS))
    assert found is not None and found.earned_today
    assert found.earned[0] == album.Earned(WEDNESDAY, sticker_for(WEDNESDAY))


def test_the_album_trusts_todays_seconds_and_never_a_day_to_come() -> None:
    state = album_state(today_seconds=60)
    state["album"]["days"] = iso(WEDNESDAY + dt.timedelta(days=1), WEDNESDAY, MONDAY)
    found = album.from_state(state)
    assert found is not None
    assert [e.day for e in found.earned] == [MONDAY], "not tomorrow, and today is not done"


@pytest.mark.parametrize(
    "broken",
    [
        None,
        "no",
        {"days": []},
        {"today": "yesterday", "days": []},
        {"today": "2026-10-21", "days": "2026-10-20"},
        {"today": "2026-10-21", "days": ["soon"]},
        {"today": "2026-10-21", "days": [], "goal_seconds": 0},
    ],
)
def test_a_broken_album_is_no_album(broken: Any) -> None:
    state = a_state()
    if broken is not None:
        state["album"] = broken
    assert album.from_state(state) is None
    assert album.from_state(None) is None


def test_the_week_runs_monday_to_sunday() -> None:
    found = album.from_state(album_state(days=[MONDAY, MONDAY - dt.timedelta(days=1)]))
    assert found is not None
    week = found.week
    assert [d.day.weekday() for d in week] == list(range(7))
    assert week[0].sticker == sticker_for(MONDAY), "this week's Monday"
    assert week[1].sticker is None and not week[1].future
    assert week[2].today and not week[2].future
    assert all(d.future for d in week[3:])
    assert found.week_count == 1, "last Sunday is last week's"
    assert found.count == 2


def test_the_minutes_left_are_rounded_up() -> None:
    found = album.from_state(album_state(today_seconds=GOAL_SECONDS - 30))
    assert found is not None and found.minutes_left == 1
    assert found.progress == pytest.approx((GOAL_SECONDS - 30) / GOAL_SECONDS)


# ----------------------------------------------------------- the database


def tick(
    session: Session, child_id: int, day: dt.date, category: str, slug: str, s: int
) -> None:
    session.add(
        UsageTick(child_id=child_id, day=day, category=category, app_slug=slug, seconds=s)
    )


def test_only_learning_counts_and_apps_add_up(session: Session, child: Child) -> None:
    other = Child(unix_user="kerem", display_name="Kerem", birth_year=2017)
    session.add(other)
    session.flush()
    monday, tuesday = MONDAY, MONDAY + dt.timedelta(days=1)
    tick(session, child.id, monday, "learning", "gcompris", 8 * 60)
    tick(session, child.id, monday, "learning", "tuxmath", 8 * 60)
    tick(session, child.id, tuesday, "games", "kdiamond", 3600)
    tick(session, child.id, tuesday, "learning", "gcompris", 14 * 60)
    tick(session, other.id, tuesday, "learning", "gcompris", 3600)
    tick(session, child.id, WEDNESDAY, "learning", "gcompris", 3600)
    session.commit()

    sent = album.for_shell(session, child.id, WEDNESDAY, today_seconds=120)
    assert sent == {
        "today": "2026-10-21",
        "days": ["2026-10-19"],
        "today_seconds": 120,
        "goal_seconds": GOAL_SECONDS,
    }, "two apps add up; games, a sibling, and today's rows are not counted here"
    assert album.for_shell(session, child.id, WEDNESDAY, GOAL_SECONDS)["days"] == iso(
        WEDNESDAY, MONDAY
    )


# ---------------------------------------------------------------- the daemon


@pytest.fixture
def source() -> FakeSessionSource:
    return FakeSessionSource()


@pytest.fixture
def supervisor(
    db: Database, config: Config, clock: FakeClock, child: Child, source: FakeSessionSource
) -> Supervisor:
    return Supervisor(
        database=db,
        bus=EventBus(queue_size=256, history_size=256),
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, RecordingRunner()),
        clock=clock,
        config=config,
    )


async def use(
    supervisor: Supervisor, clock: FakeClock, seconds: int, category: str = "learning"
) -> None:
    """Run the loop with one app in front, re-reported as the shell does.

    The kind of app is the child's own apps' to say (D-228), so each kind is
    its own app here: GCompris for learning, SuperTux for games.
    """
    slug = {"learning": "gcompris", "games": "supertux"}.get(category, category)
    give_app(supervisor.session, 1, slug, category)
    for _ in range(seconds // TICK):
        clock.advance(TICK)
        supervisor.report_activity(1, slug)
        await supervisor.tick()


async def test_the_launcher_hears_of_todays_sticker(
    supervisor: Supervisor, clock: FakeClock, session: Session, source: FakeSessionSource
) -> None:
    policy = session.get(Policy, 1)
    assert policy is not None
    policy.weekday_quota_min = policy.weekend_quota_min = 120
    policy.earn_enabled = False
    session.commit()
    await supervisor.start()
    source.log_in("elif")

    await use(supervisor, clock, 10 * 60)
    state = supervisor.shell_state(1)["album"]
    assert state["today"] == "2026-09-21" and state["days"] == []
    assert 0 < state["today_seconds"] < GOAL_SECONDS

    await use(supervisor, clock, 30 * 60, category="games")
    assert supervisor.shell_state(1)["album"]["days"] == [], "games never earn one"

    await use(supervisor, clock, 6 * 60)
    state = supervisor.shell_state(1)
    assert state["album"]["days"] == ["2026-09-21"]
    assert state["album"]["today_seconds"] >= GOAL_SECONDS
    assert state["earned_seconds"] == 0, "a sticker is never minutes"


async def test_a_sticker_is_a_day_on_the_familys_own_calendar(
    supervisor: Supervisor, clock: FakeClock, session: Session, source: FakeSessionSource
) -> None:
    """Ten minutes before midnight and ten after are two days of ten minutes."""
    policy = session.get(Policy, 1)
    assert policy is not None
    policy.weekday_quota_min = policy.weekend_quota_min = 120
    session.commit()
    clock.wall = dt.datetime(2026, 9, 21, 23, 50, 0)
    await supervisor.start()
    source.log_in("elif")

    await use(supervisor, clock, 20 * 60)
    state = supervisor.shell_state(1)["album"]
    assert state["today"] == "2026-09-22"
    assert state["days"] == []
    assert state["today_seconds"] < GOAL_SECONDS
    assert album.learning_seconds(session, 1, before=dt.date(2026, 9, 22)) == {
        dt.date(2026, 9, 21): pytest.approx(10 * 60, abs=2 * TICK)
    }


# ------------------------------------------------------------- the parent


def test_the_parents_insights_count_the_stickers() -> None:
    def learning(*minutes: int) -> dict[tuple[dt.date, str, str], int]:
        return {
            (MONDAY + dt.timedelta(days=n), "learning", "gcompris"): m * 60
            for n, m in enumerate(minutes)
        }

    span = span_for(Period.WEEK, WEDNESDAY)
    found = summarize(
        Facts(general={MONDAY: 3600}, apps=learning(20, 14, 16)), Period.WEEK, span, WEDNESDAY
    )
    assert found.stickers == 2
    said = next(h for h in highlights(found, Context(child_id=1)) if h.key.endswith("stickers"))
    assert said.tone is Tone.GOOD and said.params == {"count": 2, "minutes": 15}
    for locale in ("tr", "en"):
        text = insights.render(said, translator(locale)).text
        assert "2" in text and "15" in text and "{" not in text
    assert "albümüne 2 çıkartma" in insights.render(said, translator("tr")).text.lower()

    one = summarize(
        Facts(general={MONDAY: 3600}, apps=learning(20)), Period.WEEK, span, WEDNESDAY
    )
    assert one.stickers == 1
    assert "insights.h.stickers" not in [h.key for h in highlights(one, Context(child_id=1))]


# ------------------------------------------------------------ the launcher


class SaidAloud:
    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []

    async def __call__(self, text: str, locale: str) -> None:
        self.said.append((text, locale))


def launcher(config: Config, state: dict[str, Any] | None, home: Path) -> TestClient:
    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(state)  # type: ignore[assignment]
    app.state.shell.launcher = _RecordingLauncher(config)
    app.state.shell.home = home
    app.state.shell.say = SaidAloud()
    return TestClient(app, base_url=LAUNCHER)


def said(client: TestClient) -> list[tuple[str, str]]:
    _settle(client)
    voice: SaidAloud = client.app.state.shell.say  # type: ignore[attr-defined]
    return voice.said


FROM_LAUNCHER = {"origin": LAUNCHER, "sec-fetch-site": "same-origin"}


def test_the_chip_counts_the_stickers_and_opens_the_album(
    config: Config, tmp_path: Path
) -> None:
    page = launcher(config, album_state(days=[MONDAY, dt.date(2026, 10, 2)]), tmp_path)
    home = page.get("/").text
    chip = home.split('id="album-chip"')[1].split("</a>")[0]
    assert 'href="/album"' in chip
    assert (
        '<span class="album-chip__count" id="album-chip-count" aria-hidden="true">2</span>'
        in chip
    )
    assert 'aria-label="Çıkartma albümüm: 2 çıkartma"' in chip
    assert 'data-speak="Çıkartma albümüm"' in chip, "4-7 hear what it is (D-175)"
    assert (
        home.index('class="hello"')
        < home.index('id="album-chip"')
        < home.index('id="ring-wrap"')
    )


def test_no_chip_without_an_album_or_a_way_to_earn_one(config: Config, tmp_path: Path) -> None:
    assert 'id="album-chip"' not in launcher(config, a_state(), tmp_path).get("/").text
    state = album_state()
    state["tiles"] = [t for t in state["tiles"] if t["category"] != "learning"]
    assert 'id="album-chip"' not in launcher(config, state, tmp_path).get("/").text
    state = album_state(days=[MONDAY])
    state["tiles"] = []
    assert 'id="album-chip"' in launcher(config, state, tmp_path).get("/").text, (
        "a sticker already earned stays reachable"
    )


def test_the_album_reads_without_a_script(config: Config, tmp_path: Path) -> None:
    client = launcher(config, album_state(days=[MONDAY, dt.date(2026, 10, 2)]), tmp_path)
    page = client.get("/album").text
    tr = translator("tr")
    for day in (MONDAY, dt.date(2026, 10, 2)):
        sticker = sticker_for(day)
        assert sticker.emoji in page and tr(sticker.key) in page
    assert "19.10.2026" in page and "02.10.2026" in page
    assert "Bu hafta bir çıkartma kazandın!" in page
    assert "Bugün öğrenme oyunlarında 15 dakika geçirirsen bir çıkartma kazanırsın." in page
    assert 'href="/launch/eba"' in page and "EBA" in page, "the child's own learning tile"
    assert 'href="/launch/tuxpaint"' not in page, "Boya is free time, not learning"
    assert '<script src="/static/album.js' in page
    assert 'href="/"' in page, "a way home"


def test_an_empty_album_encourages(config: Config, tmp_path: Path) -> None:
    page = launcher(config, album_state(today_seconds=300), tmp_path).get("/album").text
    assert "Albümün seni bekliyor!" in page
    assert "Öğrenme oyunlarında 15 dakika geçirdiğin her gün" in page
    assert "Bu hafta henüz çıkartma yok." in page
    assert "10 dakika daha öğrenme oyunu oynarsan bugünün çıkartmasını kazanırsın." in page
    assert 'class="album-grid"' not in page


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "Bugün öğrenme oyunlarında 15 dakika geçirirsen bir çıkartma kazanırsın."),
        (
            14 * 60 + 10,
            "Bir dakika daha öğrenme oyunu oynarsan bugünün çıkartmasını kazanırsın.",
        ),
        (GOAL_SECONDS, "Bugünün çıkartmasını aldın: {sticker}. Yarın yenisi seni bekliyor."),
    ],
)
def test_today_in_words(config: Config, tmp_path: Path, seconds: int, expected: str) -> None:
    page = launcher(config, album_state(today_seconds=seconds), tmp_path).get("/album").text
    name = translator("tr")(sticker_for(WEDNESDAY).key)
    assert expected.format(sticker=name) in page


def test_this_weeks_circles(config: Config, tmp_path: Path) -> None:
    page = (
        launcher(config, album_state(days=[MONDAY], today_seconds=450), tmp_path)
        .get("/album")
        .text
    )
    week = page.split('class="album-week__days"')[1].split("</ol>")[0]
    assert week.count('class="album-day ') == 7
    monday = translator("tr")(sticker_for(MONDAY).key)
    assert f'aria-label="Pazartesi: {monday}"' in week
    assert 'aria-label="Salı: çıkartma yok"' in week
    assert 'aria-label="Çarşamba: 15 dakikanın 7 dakikası"' in week
    assert "--progress: 50%" in week
    assert 'aria-label="Pazar: henüz gelmedi"' in week


def test_english_for_an_english_child(config: Config, tmp_path: Path) -> None:
    state = album_state(
        days=[MONDAY, MONDAY - dt.timedelta(days=3)], today_seconds=GOAL_SECONDS
    )
    state["locale"] = "en"
    page = launcher(config, state, tmp_path).get("/album").text
    assert "My sticker album" in page and "You earned 2 stickers this week!" in page
    assert translator("en")(sticker_for(WEDNESDAY).key) in page


def test_the_young_band_hears_a_stickers_name(config: Config, tmp_path: Path) -> None:
    young = launcher(config, album_state(days=[MONDAY]), tmp_path).get("/album").text
    name = translator("tr")(sticker_for(MONDAY).key)
    assert f'data-speak="{name}"' in young
    older = (
        launcher(config, album_state(days=[MONDAY], age_band=AgeBand.OLDER.value), tmp_path)
        .get("/album")
        .text
    )
    assert "data-speak" not in older.split('class="album-grid"')[1]


def test_no_album_page_without_an_album_or_when_the_day_is_over(
    config: Config, tmp_path: Path
) -> None:
    for state in (
        a_state(),
        album_state(allow=False, reason_code=Reason.QUOTA_EXHAUSTED),
    ):
        response = launcher(config, state, tmp_path).get("/album", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"


# ------------------------------------------------------------ celebration


def test_todays_sticker_is_celebrated_once(config: Config, tmp_path: Path) -> None:
    state = album_state(days=[MONDAY], today_seconds=GOAL_SECONDS)
    name = translator("tr")(sticker_for(WEDNESDAY).key)
    sentence = f"Yaşasın! Bugünün çıkartmasını kazandın: {name}!"
    with launcher(config, state, tmp_path) as client:
        first = client.get("/").text
        assert sentence in first and 'id="cheer" role="status"' in first
        assert 'id="cheer" role="status" aria-live="polite"\n       hidden' not in first
        assert said(client) == [(sentence, "tr")], "Zio says it, once"

        again = client.get("/").text
        assert sentence not in again, "not on every load"
        assert client.get("/album").text.count(sentence) == 0
        assert said(client) == [(sentence, "tr")]

    assert (tmp_path / CHEER_FILE).read_text(encoding="utf-8").strip() == "2026-10-21"
    with launcher(config, state, tmp_path) as restarted:
        assert sentence not in restarted.get("/").text, "nor after a restart or a second login"
        assert said(restarted) == []


def test_a_new_day_is_a_new_celebration(config: Config, tmp_path: Path) -> None:
    (tmp_path / CHEER_FILE).parent.mkdir(parents=True)
    (tmp_path / CHEER_FILE).write_text("2026-10-20\n", encoding="utf-8")
    client = launcher(config, album_state(today_seconds=GOAL_SECONDS), tmp_path)
    assert "Yaşasın!" in client.get("/album").text


def test_nothing_to_celebrate_before_the_sticker(config: Config, tmp_path: Path) -> None:
    with launcher(config, album_state(today_seconds=GOAL_SECONDS - 1), tmp_path) as client:
        page = client.get("/").text
        assert "Yaşasın!" not in page
        assert 'id="cheer"' in page, "the empty box album.js fills in"
        assert said(client) == []
    assert not (tmp_path / CHEER_FILE).exists()


def test_the_launcher_coming_back_asks_for_news(config: Config, tmp_path: Path) -> None:
    """The sticker is usually earned with a learning app in front."""
    daemon_state = album_state(days=[MONDAY], today_seconds=GOAL_SECONDS - 60)
    with launcher(config, daemon_state, tmp_path) as client:
        page = client.get("/").text
        assert "Yaşasın!" not in page
        quiet = client.post("/album/cheer", json={}, headers=FROM_LAUNCHER).json()
        assert quiet == {"count": 1, "label": "Çıkartma albümüm: 1 çıkartma", "cheer": None}

        daemon: FakeShellDaemon = client.app.state.shell.client  # type: ignore[attr-defined]
        daemon.data = album_state(days=[MONDAY], today_seconds=GOAL_SECONDS)
        news = client.post("/album/cheer", json={}, headers=FROM_LAUNCHER).json()
        assert news["count"] == 2
        assert news["cheer"]["sticker"] == sticker_for(WEDNESDAY).emoji
        assert news["cheer"]["text"].startswith("Yaşasın!")
        assert (
            client.post("/album/cheer", json={}, headers=FROM_LAUNCHER).json()["cheer"] is None
        )
        assert len(said(client)) == 1


def test_a_website_cannot_ask_for_the_celebration(config: Config, tmp_path: Path) -> None:
    client = launcher(config, album_state(today_seconds=GOAL_SECONDS), tmp_path)
    response = client.post(
        "/album/cheer",
        content="x=1",
        headers={"content-type": "application/x-www-form-urlencoded", "origin": LAUNCHER},
    )
    assert response.status_code == 403
    forged = client.post("/album/cheer", json={}, headers={"origin": "https://example.com"})
    assert forged.status_code == 403
    assert not (tmp_path / CHEER_FILE).exists(), "nothing was taken"


def test_a_home_that_cannot_be_written_still_celebrates(config: Config, tmp_path: Path) -> None:
    blocked = tmp_path / "file-not-a-folder"
    blocked.write_text("", encoding="utf-8")
    client = launcher(config, album_state(today_seconds=GOAL_SECONDS), blocked)
    assert "Yaşasın!" in client.get("/").text
    assert "Yaşasın!" not in client.get("/").text, "still once, in memory"
