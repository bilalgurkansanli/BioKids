"""D-214: an evening's ending is bedtime, not a cold lock.

The decision under test is small and easy to get subtly wrong: *is this day
ending at night, and when?* It reads the same two things that end a day - the
last window closing, the quota running out - and nothing that only pauses it:
a gap between two windows, a games limit, a break. So it is tested as a pure
function first, at the edges: the minute the evening starts, a pause before a
later window, a window past midnight, a category that runs out first.

The second half is the promise that this is presentation only. The same day
run at four in the afternoon and at a quarter to eight in the evening must lock,
count down and switch off at the same moments; only the words, the colours and
the brightness may differ.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import random
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from biokids_core import policy as policy_module
from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import Child, Database, Policy
from biokids_core.ipc import Event, EventKind
from biokids_core.policy import (
    BEDTIME_LEAD_SECONDS,
    bedtime_in,
    day_left_seconds,
    evaluate,
    is_evening,
)
from biokids_core.types import (
    Action,
    ActivityContext,
    Category,
    EndAction,
    Reason,
    SpecialDay,
    TimeWindow,
)
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.supervisor import Supervisor
from biokids_shell import device
from biokids_shell.app import create_app
from biokids_shell.dimmer import FLOOR_PERCENT, BedtimeDimmer, dim_percent
from biokids_shell.relay import SessionRelay
from helpers import MONDAY, make_policy, make_state
from test_daemon import LoginctlAwareRunner
from test_shell import LAUNCHER, FakeShellDaemon, a_state

TICK = 10


def at(hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime.combine(MONDAY, dt.time(hour, minute, second))


# ------------------------------------------------------- the evening, as such


@pytest.mark.parametrize(
    ("moment", "evening"),
    [
        (dt.time(18, 29, 59), False),
        (dt.time(18, 30), True),
        (dt.time(23, 59), True),
        (dt.time(0, 30), True),
        (dt.time(4, 59), True),
        (dt.time(5, 0), False),
        (dt.time(12, 0), False),
    ],
)
def test_the_evening_starts_at_half_past_six_and_lasts_the_night(
    moment: dt.time, evening: bool
) -> None:
    assert is_evening(dt.datetime.combine(MONDAY, moment)) is evening


# ------------------------------------------------------- when does the day end


def test_a_quota_running_out_ends_the_day_whatever_the_time() -> None:
    policy = make_policy(weekday_quota_min=60)
    assert day_left_seconds(policy, make_state(general_used_s=50 * 60), at(10)) == 600
    assert day_left_seconds(policy, make_state(general_used_s=60 * 60), at(10)) == 0


def test_the_last_window_closing_ends_the_day() -> None:
    policy = make_policy(
        weekday_quota_min=600, weekday_windows=(TimeWindow.parse("16:00-20:00"),)
    )
    assert day_left_seconds(policy, make_state(), at(19, 52)) == 8 * 60
    assert day_left_seconds(policy, make_state(), at(21)) == 0, "after the last window"


def test_a_window_with_another_still_ahead_is_a_pause_not_an_ending() -> None:
    policy = make_policy(
        weekday_quota_min=600,
        weekday_windows=(TimeWindow.parse("16:00-19:00"), TimeWindow.parse("20:00-21:00")),
    )
    # Only the quota could end this day before the last window: ten hours out.
    assert day_left_seconds(policy, make_state(), at(18, 55)) == 600 * 60
    assert day_left_seconds(policy, make_state(), at(19, 30)) is None, "between the two"
    assert day_left_seconds(policy, make_state(), at(20, 55)) == 300, "the last one"


def test_two_windows_that_touch_are_one_evening() -> None:
    """16-18 then 18-20: at 17:55 nothing ends at six."""
    policy = make_policy(
        weekday_quota_min=600,
        weekday_windows=(TimeWindow.parse("16:00-18:00"), TimeWindow.parse("18:00-20:00")),
    )
    assert day_left_seconds(policy, make_state(), at(17, 55)) == 600 * 60, "not 5 minutes"


def test_a_quota_ending_before_the_window_closes_is_what_ends_the_day() -> None:
    policy = make_policy(
        weekday_quota_min=60, weekday_windows=(TimeWindow.parse("16:00-21:00"),)
    )
    state = make_state(general_used_s=55 * 60)
    assert day_left_seconds(policy, state, at(19)) == 300


def test_nothing_ends_a_day_with_no_limit_and_no_hours() -> None:
    policy = make_policy(
        special_days=(SpecialDay(start=MONDAY, end=MONDAY, no_limit=True),),
    )
    assert day_left_seconds(policy, make_state(general_used_s=5 * 3600), at(20)) is None


def test_a_category_running_out_is_not_the_end_of_the_day() -> None:
    policy = make_policy(weekday_quota_min=120, category_limits_min={Category.GAMES: 30})
    state = make_state(general_used_s=40 * 60, category_used_s={Category.GAMES: 29 * 60})
    assert day_left_seconds(policy, state, at(19, 55)) == 80 * 60


# ------------------------------------------------------------- is it bedtime


def test_the_last_ten_minutes_of_an_evening_are_bedtime() -> None:
    policy = make_policy(
        weekday_quota_min=600, weekday_windows=(TimeWindow.parse("16:00-20:00"),)
    )
    assert bedtime_in(policy, make_state(), at(19, 49)) is None, "eleven minutes out"
    assert bedtime_in(policy, make_state(), at(19, 50)) == BEDTIME_LEAD_SECONDS
    assert bedtime_in(policy, make_state(), at(19, 59)) == 60
    assert bedtime_in(policy, make_state(), at(21)) == 0, "and after, it says good night"


def test_a_quota_running_out_late_is_bedtime_and_early_is_not() -> None:
    policy = make_policy(weekday_quota_min=60)
    almost = make_state(general_used_s=55 * 60)
    assert bedtime_in(policy, almost, at(19, 30)) == 300
    assert bedtime_in(policy, almost, at(15)) is None, "a daytime ending stays as it is"
    assert bedtime_in(policy, make_state(general_used_s=60 * 60), at(15)) is None


def test_what_counts_is_when_the_day_ends_not_when_the_warning_is() -> None:
    """At 18:25 with eight minutes left the day ends at 18:33: in the evening."""
    policy = make_policy(weekday_quota_min=60)
    state = make_state(general_used_s=52 * 60)
    assert bedtime_in(policy, state, at(18, 25)) == 8 * 60
    assert bedtime_in(policy, state, at(18, 20)) is None, "ends at 18:28"


def test_a_day_that_ends_after_midnight_ends_at_night() -> None:
    policy = make_policy(
        weekday_quota_min=600, weekday_windows=(TimeWindow.parse("22:00-23:59"),)
    )
    assert bedtime_in(policy, make_state(), at(23, 55)) == 4 * 60


def test_a_pause_before_a_later_window_is_not_bedtime() -> None:
    policy = make_policy(
        weekday_quota_min=600,
        weekday_windows=(TimeWindow.parse("16:00-19:00"), TimeWindow.parse("20:00-21:00")),
    )
    assert bedtime_in(policy, make_state(), at(18, 55)) is None
    assert bedtime_in(policy, make_state(), at(19, 30)) is None


def test_an_unmanaged_child_has_no_bedtime() -> None:
    assert bedtime_in(None, make_state(), at(21)) is None
    disabled = make_policy(enabled=False, weekday_quota_min=0)
    assert bedtime_in(disabled, make_state(), at(21)) is None


# ------------------------------------------------------ what the engine says


def test_the_evenings_warnings_are_the_night_ones_with_zio_sleepy() -> None:
    policy = make_policy(
        weekday_quota_min=600, weekday_windows=(TimeWindow.parse("16:00-20:00"),)
    )
    for minute, event in ((50, "bedtime_10"), (55, "bedtime_5"), (59, "bedtime_1")):
        decision = evaluate(policy, make_state(), ActivityContext(), at(19, minute))
        assert decision.speak is not None
        assert decision.speak.event == event
        assert decision.speak.expression == "sleepy"
        assert decision.speak.vars["name"] == "Elif"


def test_the_afternoons_warnings_are_unchanged() -> None:
    policy = make_policy(weekday_quota_min=60)
    decision = evaluate(policy, make_state(general_used_s=50 * 60), ActivityContext(), at(16))
    assert decision.speak is not None
    assert (decision.speak.event, decision.speak.expression) == ("warn_10", "neutral")


def test_the_end_of_an_evening_says_good_night() -> None:
    quota = make_policy(weekday_quota_min=60)
    decision = evaluate(quota, make_state(general_used_s=3600), ActivityContext(), at(20))
    assert decision.reason_code == Reason.QUOTA_EXHAUSTED
    assert decision.speak is not None
    assert (decision.speak.event, decision.speak.expression) == ("bedtime", "sleepy")

    window = make_policy(weekday_windows=(TimeWindow.parse("16:00-19:00"),))
    decision = evaluate(window, make_state(), ActivityContext(), at(19, 30))
    assert decision.reason_code == Reason.WINDOW_CLOSED
    assert decision.speak is not None and decision.speak.event == "bedtime"


def test_a_daytime_end_and_a_pause_keep_their_own_words() -> None:
    quota = make_policy(weekday_quota_min=60)
    decision = evaluate(quota, make_state(general_used_s=3600), ActivityContext(), at(15))
    assert decision.speak is not None and decision.speak.event == "time_up"

    pause = make_policy(
        weekday_windows=(TimeWindow.parse("16:00-19:00"), TimeWindow.parse("20:00-21:00"))
    )
    decision = evaluate(pause, make_state(), ActivityContext(), at(19, 30))
    assert decision.reason_code == Reason.WINDOW_WAITING
    assert decision.speak is not None and decision.speak.event == "window_waiting"


def test_a_games_limit_running_out_in_the_evening_is_not_bedtime() -> None:
    policy = make_policy(weekday_quota_min=120, category_limits_min={Category.GAMES: 30})
    state = make_state(general_used_s=110 * 60, category_used_s={Category.GAMES: 25 * 60})
    decision = evaluate(policy, state, ActivityContext(category=Category.GAMES), at(19, 55))
    assert decision.speak is not None
    assert decision.speak.event == "warn_5", "five minutes of games, not of the day"


@pytest.mark.parametrize(
    ("moment", "used_min"),
    [
        ((19, 50), 0),
        ((19, 55), 0),
        ((19, 59, 30), 0),
        ((20, 5), 0),
        ((19, 30), 55),
        ((20, 0), 60),
    ],
)
def test_bedtime_changes_the_words_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, moment: tuple[int, ...], used_min: int
) -> None:
    """The same moment with the evening switched off decides exactly the same."""
    policy = make_policy(
        weekday_quota_min=60, weekday_windows=(TimeWindow.parse("16:00-20:00"),)
    )
    state = make_state(general_used_s=used_min * 60)
    night = evaluate(policy, state, ActivityContext(), at(*moment))
    monkeypatch.setattr(policy_module, "EVENING_STARTS", dt.time(23, 59, 59))
    monkeypatch.setattr(policy_module, "NIGHT_ENDS", dt.time(0, 0))
    day = evaluate(policy, state, ActivityContext(), at(*moment))

    assert dataclasses.replace(night, speak=None) == dataclasses.replace(day, speak=None)
    assert night.speak is not None and day.speak is not None
    assert night.speak.event != day.speak.event
    assert night.speak.vars == day.speak.vars and night.speak.priority == day.speak.priority


# --------------------------------------------- the daemon: same end, other words


@pytest.fixture
def source() -> FakeSessionSource:
    return FakeSessionSource()


def _supervisor(
    db: Database, config: Config, clock: FakeClock, source: FakeSessionSource
) -> tuple[Supervisor, LoginctlAwareRunner, EventBus]:
    runner = LoginctlAwareRunner(source)
    bus = EventBus(queue_size=4096, history_size=4096)
    supervisor = Supervisor(
        database=db,
        bus=bus,
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, runner),
        clock=clock,
        config=config,
    )
    return supervisor, runner, bus


def _quota(session: Session, minutes: int) -> None:
    row = session.get(Policy, 1)
    assert row is not None
    row.weekday_quota_min = minutes
    row.weekend_quota_min = minutes
    row.end_action = EndAction.POWEROFF.value
    row.lock_countdown_seconds = 120
    session.commit()


async def _run_the_day_out(
    db: Database, config: Config, clock: FakeClock, session: Session
) -> tuple[list[tuple[int, str]], list[str], list[dict[str, Any]]]:
    """Fifteen minutes of a twelve-minute day: what happened when, and what was said."""
    _quota(session, 12)
    source = FakeSessionSource()
    supervisor, runner, bus = _supervisor(db, config, clock, source)
    await supervisor.start()
    source.log_in("elif")
    timeline: list[tuple[int, str]] = []
    for step in range(15 * 60 // TICK):
        clock.advance(TICK)
        before = len(runner.calls)
        await supervisor.tick()
        for call in runner.calls[before:]:
            timeline.append((step, " ".join(call[:2])))
    spoken = [str(e.payload.get("event")) for e in bus.history if e.kind == EventKind.SPEAK]
    warnings = [e.payload for e in bus.history if e.kind == EventKind.WARNING]
    await supervisor.stop()
    return timeline, spoken, warnings


async def test_an_evening_locks_and_switches_off_exactly_when_an_afternoon_does(
    db: Database, config: Config, session: Session, child: Child
) -> None:
    """D-214's promise: presentation only. Same steps, same ticks."""
    afternoon = FakeClock(wall=at(16))
    day_timeline, day_spoken, day_warnings = await _run_the_day_out(
        db, config, afternoon, session
    )

    # A fresh ledger day for the second run: the next day, a Tuesday, at 19:50.
    evening = FakeClock(wall=at(19, 50) + dt.timedelta(days=1))
    night_timeline, night_spoken, night_warnings = await _run_the_day_out(
        db, config, evening, session
    )

    assert night_timeline == day_timeline
    assert any("poweroff" in call for _, call in night_timeline)
    assert [w["minutes"] for w in night_warnings] == [w["minutes"] for w in day_warnings]
    assert [w["bedtime"] for w in day_warnings] == [False, False, False]
    assert [w["bedtime"] for w in night_warnings] == [True, True, True]
    assert [e for e in day_spoken if e != "welcome"][-4:] == [
        "warn_10",
        "warn_5",
        "warn_1",
        "time_up",
    ]
    assert [e for e in night_spoken if e != "welcome"][-4:] == [
        "bedtime_10",
        "bedtime_5",
        "bedtime_1",
        "bedtime",
    ]


async def test_the_ticks_and_the_launcher_hear_how_far_bedtime_is(
    db: Database, config: Config, session: Session, child: Child
) -> None:
    _quota(session, 600)
    row = session.get(Policy, 1)
    assert row is not None
    row.windows = {"weekday": ["16:00-20:00"], "weekend": []}
    session.commit()
    clock = FakeClock(wall=at(19, 45))
    source = FakeSessionSource()
    supervisor, _runner, bus = _supervisor(db, config, clock, source)
    await supervisor.start()
    source.log_in("elif")

    await supervisor.tick()
    assert supervisor.shell_state(1)["bedtime"] is False
    ticks = [e.payload for e in bus.history if e.kind == EventKind.TICK]
    assert ticks[-1]["bedtime_seconds"] is None

    clock.advance(6 * 60)
    await supervisor.tick()
    state = supervisor.shell_state(1)
    assert state["bedtime"] is True
    assert state["bedtime_seconds"] == 9 * 60
    ticks = [e.payload for e in bus.history if e.kind == EventKind.TICK]
    assert ticks[-1]["bedtime_seconds"] == 9 * 60
    await supervisor.stop()


# ---------------------------------------------------------- the launcher draws it


def _launcher(config: Config, state: dict[str, Any]) -> str:
    app = create_app(config)
    app.state.shell.client = FakeShellDaemon(state)  # type: ignore[assignment]
    return TestClient(app, base_url=LAUNCHER).get("/").text


def test_an_evening_ending_is_a_good_night(config: Config) -> None:
    page = _launcher(
        config,
        a_state(
            allow=False,
            reason_code=Reason.QUOTA_EXHAUSTED,
            end_action="poweroff",
            countdown_seconds=90,
            bedtime=True,
        ),
    )
    assert "İyi geceler, Elif." in page
    assert "Yarın görüşürüz." in page
    assert "kapanacak" in page, "the same end action, said as plainly"
    assert 'data-seconds="90"' in page, "and the same countdown"
    assert "full--night" in page
    assert "is-sleepy" in page, "Zio is sleepy"
    assert "Bugünlük bu kadar" not in page


def test_the_last_window_closing_at_night_is_a_good_night_too(config: Config) -> None:
    page = _launcher(
        config,
        a_state(allow=False, reason_code=Reason.WINDOW_CLOSED, bedtime=True, end_action="lock"),
    )
    assert "İyi geceler, Elif." in page


def test_a_daytime_ending_and_a_pause_keep_their_screens(config: Config) -> None:
    day = _launcher(
        config,
        a_state(allow=False, reason_code=Reason.QUOTA_EXHAUSTED, countdown_seconds=90),
    )
    assert "Bugünlük bu kadar" in day and "İyi geceler" not in day

    pause = _launcher(
        config, a_state(allow=False, reason_code=Reason.WINDOW_WAITING, bedtime=True)
    )
    assert "İyi geceler" not in pause


def test_good_night_in_english(config: Config) -> None:
    page = _launcher(
        config,
        a_state(locale="en", allow=False, reason_code=Reason.QUOTA_EXHAUSTED, bedtime=True),
    )
    assert "Good night, Elif." in page
    assert "See you tomorrow." in page


def test_the_page_carries_the_nights_warning_words(config: Config) -> None:
    """As data for the notice, like the daytime ones (JSON-escaped, hence ASCII)."""
    page = _launcher(config, a_state())
    for key in ('"bedtime_10"', '"bedtime_5"', '"bedtime_1"', '"bedtime_warning"'):
        assert key in page
    assert "Yatma vakti yakla" in page


def test_the_notice_knows_the_night_tone() -> None:
    """The stylesheet has the night's notice and does not pulse it red."""
    css = Path(__file__).resolve().parents[1] / "src" / "biokids_shell" / "static" / "shell.css"
    text = css.read_text(encoding="utf-8")
    assert '.notice[data-tone="night"]' in text
    assert ':not([data-tone="night"]) .notice__ring' in text
    js = css.with_name("shell.js").read_text(encoding="utf-8")
    assert 'indexOf("bedtime_")' in js, "the spoken night line replaces the notice's"


# ------------------------------------------------------------------ dimming


class FakeLight:
    def __init__(self, level: int | None = 80, *, works: bool = True) -> None:
        self.level = level
        self.works = works
        self.set_calls: list[int] = []

    async def get(self) -> int | None:
        return self.level

    async def set(self, level: int) -> bool:
        self.set_calls.append(level)
        if self.works:
            self.level = level
        return self.works


def dimmer(tmp_path: Path, light: FakeLight) -> BedtimeDimmer:
    return BedtimeDimmer(tmp_path / "state" / "brightness", get=light.get, set_level=light.set)


@pytest.mark.parametrize(
    ("seconds", "percent"),
    [(None, 100), (601, 100), (600, 90), (451, 90), (450, 80), (300, 70), (150, 60), (0, 60)],
)
def test_the_dimming_goes_down_in_four_gentle_steps(seconds: int | None, percent: int) -> None:
    assert dim_percent(seconds) == percent


async def test_the_screen_dims_step_by_step_and_comes_back(tmp_path: Path) -> None:
    light = FakeLight(80)
    subject = dimmer(tmp_path, light)
    for seconds in (600, 590, 440, 290, 140, 0):
        await subject.on_tick({"bedtime_seconds": seconds, "online": True})
    assert light.set_calls == [72, 64, 56, 48], "one change per step, from the child's 80"
    assert (tmp_path / "state" / "brightness").read_text().strip() == "80"

    # A parent's "+30 dk": the day no longer ends within ten minutes.
    await subject.on_tick({"bedtime_seconds": None, "online": True})
    assert light.level == 80
    assert not (tmp_path / "state" / "brightness").exists()


async def test_a_tick_that_says_nothing_about_bedtime_changes_nothing(tmp_path: Path) -> None:
    light = FakeLight(100)
    subject = dimmer(tmp_path, light)
    await subject.on_tick({"bedtime_seconds": 300, "online": True})
    await subject.on_tick({"remaining_seconds": 900, "granted": 600})
    assert light.set_calls == [70]


async def test_a_session_not_on_the_screen_is_not_dimmed(tmp_path: Path) -> None:
    light = FakeLight(100)
    await dimmer(tmp_path, light).on_tick({"bedtime_seconds": 300, "online": False})
    assert light.set_calls == []


async def test_the_next_session_puts_back_what_the_last_one_dimmed(tmp_path: Path) -> None:
    """Switched off at sixty percent: systemd-backlight brings that back at boot."""
    light = FakeLight(100)
    first = dimmer(tmp_path, light)
    await first.on_tick({"bedtime_seconds": 60, "online": True})
    assert light.level == FLOOR_PERCENT

    second = dimmer(tmp_path, light)
    await second.restore()
    assert light.level == 100
    assert not (tmp_path / "state" / "brightness").exists()

    await dimmer(tmp_path, light).restore()
    assert light.set_calls == [60, 100], "nothing to put back the time after"


async def test_a_level_not_yet_put_back_is_still_the_childs_own(tmp_path: Path) -> None:
    """Last evening's 60% was never put back; tonight dims from the 100% before it."""
    saved = tmp_path / "state" / "brightness"
    saved.parent.mkdir(parents=True)
    saved.write_text("100\n")
    light = FakeLight(60)
    subject = dimmer(tmp_path, light)
    await subject.on_tick({"bedtime_seconds": 500, "online": True})
    assert light.set_calls == [90]
    await subject.restore()
    assert light.level == 100


async def test_a_machine_that_cannot_dim_is_left_alone(tmp_path: Path) -> None:
    light = FakeLight(None, works=False)
    subject = dimmer(tmp_path, light)
    for seconds in (600, 450, 300):
        await subject.on_tick({"bedtime_seconds": seconds, "online": True})
    assert light.set_calls == [90], "tried once, not every tick"
    assert not subject.dimmed
    assert not (tmp_path / "state" / "brightness").exists()


async def test_the_relay_hands_the_ticks_to_the_dimmer(tmp_path: Path) -> None:
    light = FakeLight(100)
    relay = SessionRelay(None, dimmer=dimmer(tmp_path, light))  # type: ignore[arg-type]
    await relay.handle(Event(EventKind.TICK, payload={"bedtime_seconds": 400, "online": True}))
    assert light.set_calls == [80]


async def test_a_dimming_that_fails_never_stops_the_relay(tmp_path: Path) -> None:
    class Broken(BedtimeDimmer):
        async def on_tick(self, payload: Any) -> None:
            raise RuntimeError("xrandr exploded")

    relay = SessionRelay(None, dimmer=Broken(tmp_path / "b"))  # type: ignore[arg-type]
    await relay.handle(Event(EventKind.TICK, payload={"bedtime_seconds": 1}))


async def test_the_brightness_is_read_the_way_it_is_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: dict[str, str | None] = {
        "brightnessctl": "intel_backlight,backlight,1200,50%,2400\n",
        "xrandr": None,
    }

    async def output(argv: list[str]) -> str | None:
        return answers[argv[0]]

    monkeypatch.setattr(device, "_output", output)
    assert await device.get_brightness() == 50

    answers.update(
        brightnessctl=None, xrandr="Screen 0\nHDMI-1 connected\n\tBrightness: 0.80\n"
    )
    assert await device.get_brightness() == 80

    answers.update(xrandr=None)
    assert await device.get_brightness() is None


# --------------------------------------------------- the voice: night or day


def test_a_night_line_stands_in_for_its_daytime_one() -> None:
    from biokids_voice.service import daytime_event

    assert daytime_event("bedtime") == "time_up"
    assert daytime_event("bedtime_10") == "warn_10"
    assert daytime_event("warn_10") is None


async def test_a_parents_own_recording_is_heard_at_night_too(config: Config) -> None:
    """D-174's recording of "five minutes" is the parent's voice; it wins over the
    package's night wording, and the night face stays."""
    from biokids_core import recorded_voice
    from biokids_voice.lines import LineBook
    from biokids_voice.player import NullPlayer
    from biokids_voice.service import VoiceService
    from test_recorded_voice import a_wav
    from test_voice import speak

    directory = config.paths.recorded_voice_dir
    recorded_voice.save(directory, "tr", "warn_5", a_wav(1.0), "Beş dakikan kaldı canım.")
    service = VoiceService(
        config, lines=LineBook(config, rng=random.Random(7)), player=NullPlayer()
    )
    try:
        reply = await service.handle(speak("bedtime_5", vars={"minutes": 5}))
        assert reply.data["recorded"] is True
        assert reply.data["text"] == "Beş dakikan kaldı canım."
        assert reply.data["expression"] == "sleepy"

        line = await service.handle(speak("bedtime", vars={"name": "Elif"}))
        assert "İyi geceler" in line.data["text"]
        assert line.data["priority"] == "high", "the last line of the day is never dropped"
    finally:
        await service.aclose()


def test_the_night_lines_exist_in_both_languages() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "biokids_core" / "data" / "voice"
    for locale in ("tr", "en"):
        for event in ("bedtime_10", "bedtime_5", "bedtime_1", "bedtime"):
            assert (root / locale / f"{event}.txt").is_file(), f"{locale}/{event}"
    said = (root / "tr" / "bedtime.txt").read_text(encoding="utf-8")
    assert "İyi geceler, {name}. Yarın görüşürüz." in said, "what the screen says"


def test_the_brightness_is_only_ever_lowered_to_a_readable_level() -> None:
    assert min(dim_percent(s) for s in range(0, 601, 10)) == FLOOR_PERCENT >= 50


def test_the_action_of_an_evening_decision_is_never_changed() -> None:
    """A broad sweep: every minute of an evening, the action is what the day's is."""
    policy = make_policy(
        weekday_quota_min=45, weekday_windows=(TimeWindow.parse("17:00-20:30"),)
    )
    for minute in range(0, 4 * 60, 5):
        moment = at(17) + dt.timedelta(minutes=minute)
        for used in (0, 30 * 60, 44 * 60, 45 * 60):
            decision = evaluate(
                policy, make_state(general_used_s=used), ActivityContext(), moment
            )
            assert decision.action in {Action.NONE, Action.WARN, Action.LOCK}
            if decision.speak is not None and decision.speak.event.startswith("bedtime"):
                assert bedtime_in(policy, make_state(general_used_s=used), moment) is not None
