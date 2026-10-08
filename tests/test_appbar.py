"""D-197, D-201: the app bar - home, the app's own volume, and a close that works.

The owner opened Frozen Bubble and could not close it: its window cannot be
maximised, so the title bar's close button was not where a child looks, and a
title bar's close button only asks. These pin what can be pinned without an X
server: which scope a window's process is in, which sound streams are the app's
and how they step or slide, when a close becomes a stop and what may be
stopped, where a window's frame is and when a dragged window has stopped, the
minutes and the clock, how the launcher's tile brings a sent-away app back and
which apps it lists as open, and that none of it needs GTK. The window itself
(`biokids_shell/appbar/ui.py`) is checked on the VM (`docs/VM-RUNBOOK.md` §8.9).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import struct
import subprocess
import sys
import time
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from biokids_core.config import Config
from biokids_core.i18n import translator
from biokids_shell import appbar
from biokids_shell import launcher as launcher_module
from biokids_shell.appbar import (
    BAR_CSS,
    BUTTON_FACE_PX,
    BUTTON_TARGET_PX,
    CLOSE_GRACE_S,
    DOUBLE_TAP_S,
    EXIT_BAD_UNIT,
    EXIT_NOT_FOUND,
    EXIT_UNAVAILABLE,
    HELD_AT_MOST_S,
    KNOB_PX,
    NO_EXTENTS,
    NO_SOUND,
    QUESTION_WAIT_S,
    RESTLESS_S,
    SETTLE_S,
    SLIDER_PAD_PX,
    SLIDER_PX,
    STOP_GIVE_UP_S,
    TITLE_LINE_PX,
    TRACK_PX,
    AppTarget,
    AudioState,
    CloseAction,
    Closer,
    CloseStep,
    Extents,
    Follower,
    FollowStep,
    LevelSender,
    Rect,
    SinkInput,
    TimeLeft,
    TimeTone,
    WindowInfo,
    app_inputs,
    bar_origin,
    change_volume,
    clock_text,
    decode_cardinals,
    extents_from,
    fetch_state,
    frame_rect,
    implied_extents,
    is_app_scope,
    is_app_window,
    level_at,
    list_sink_inputs,
    lost_maximize,
    parse_sink_inputs_json,
    parse_sink_inputs_text,
    read_audio,
    scope_from_cgroup,
    scope_of_pid,
    scope_unit,
    set_volume,
    shows_clock,
    state_url,
    stepped,
    stop_command,
    stop_scope,
    time_left,
    window_extents,
)
from biokids_shell.appbar import __main__ as appbar_main
from biokids_shell.launcher import AppLauncher, LaunchRequest

ROOT = Path(__file__).resolve().parent.parent
THEME_DIR = ROOT / "src" / "biokids_shell" / "xfce" / "theme" / "BioKids" / "xfwm4"

UID = 1001
MANAGER = f"/user.slice/user-{UID}.slice/user@{UID}.service"
GAME = "biokids-app-frozen-bubble-3.scope"
SITE = "biokids-app-eba-4.scope"

CGROUPS = {
    4242: f"0::{MANAGER}/app.slice/{GAME}\n",
    4243: f"0::{MANAGER}/app.slice/{GAME}\n",
    5000: f"0::{MANAGER}/app.slice/biokids-voice.service\n",
    6001: f"0::{MANAGER}/app.slice/{SITE}\n",
    7000: f"0::{MANAGER}/app.slice/biokids-shell.service\n",
}


def scope_of(pid: int) -> str | None:
    text = CGROUPS.get(pid)
    return scope_from_cgroup(text, UID) if text else None


# ------------------------------------------------------------------ scopes


def test_a_process_is_found_in_the_scope_the_launcher_started_it_in() -> None:
    assert scope_from_cgroup(CGROUPS[4242], UID) == GAME


def test_the_launcher_and_the_voice_are_in_no_app_scope() -> None:
    """What the bar must never stop: they are services, not apps."""
    assert scope_from_cgroup(CGROUPS[7000], UID) is None
    assert scope_from_cgroup(CGROUPS[5000], UID) is None


def test_another_users_scope_is_not_this_childs() -> None:
    other = "0::/user.slice/user-1002.slice/user@1002.service/app.slice/biokids-app-x-1.scope"
    assert scope_from_cgroup(other, UID) is None
    assert scope_from_cgroup(other, 1002) == "biokids-app-x-1.scope"


def test_a_hybrid_hierarchy_is_read_too() -> None:
    text = (
        "12:cpuset:/\n"
        f"1:name=systemd:{MANAGER}/app.slice/{GAME}\n"
        f"0::{MANAGER}/app.slice/{GAME}\n"
    )
    assert scope_from_cgroup(text, UID) == GAME


def test_garbage_is_no_scope() -> None:
    assert scope_from_cgroup("", UID) is None
    assert scope_from_cgroup("nonsense\n", UID) is None


def test_the_scope_is_read_from_proc(tmp_path: Path) -> None:
    (tmp_path / "4242").mkdir()
    (tmp_path / "4242" / "cgroup").write_text(CGROUPS[4242], encoding="utf-8")
    assert scope_of_pid(4242, UID, proc=tmp_path) == GAME
    assert scope_of_pid(4243, UID, proc=tmp_path) is None, "a process that is gone"
    assert scope_of_pid(0, UID, proc=tmp_path) is None, "a window with no _NET_WM_PID"


def test_the_unit_systemd_run_was_given_is_named_as_its_scope() -> None:
    assert scope_unit("biokids-app-frozen-bubble-3") == GAME
    assert scope_unit(GAME) == GAME


@pytest.mark.parametrize(
    "unit",
    [
        "biokids-shell.service",
        "biokids-voice.service",
        "graphical-session.target",
        "biokids-app-x-1.service",
        "biokids-app-.scope",
        "biokids-app-x/../y.scope",
        "biokids-app-x-1.scope --all",
        "app-biokids-app-x-1.scope",
        "",
    ],
)
def test_nothing_but_an_app_scope_can_be_stopped(unit: str) -> None:
    """The bar runs as the child and stops with --user, in the child's own
    manager; this keeps it from the launcher and the voice in there."""
    assert not is_app_scope(unit)
    with pytest.raises(ValueError, match="not a BioKids app scope"):
        stop_command(unit)


def test_an_app_scope_is_stopped_in_the_childs_own_manager() -> None:
    assert stop_command(GAME) == ["systemctl", "--user", "stop", GAME]


def test_a_stop_reports_what_systemd_said() -> None:
    ran: list[list[str]] = []

    def ok(argv: Sequence[str]) -> str | None:
        ran.append(list(argv))
        return ""

    assert stop_scope(GAME, ok) is True
    assert ran == [["systemctl", "--user", "stop", GAME]]
    assert stop_scope(GAME, lambda _argv: None) is False


# ----------------------------------------------------------------- windows


def window(**overrides: Any) -> WindowInfo:
    values: dict[str, Any] = {
        "xid": 0x3A00007,
        "pid": 4242,
        "res_class": "Frozen-bubble",
        "res_name": "frozen-bubble",
        "kind": "normal",
    }
    values.update(overrides)
    return WindowInfo(**values)


def test_the_bar_is_shown_over_an_app() -> None:
    assert is_app_window(window())
    assert is_app_window(window(kind="dialog")), "an app's question has a way out too"


def test_the_bar_is_never_over_the_launcher() -> None:
    """The launcher raises itself for the time warnings and the end of the day."""
    from biokids_shell.kiosk import KIOSK_WINDOW_CLASS

    assert not is_app_window(window(res_class=KIOSK_WINDOW_CLASS, res_name="chromium"))
    assert not is_app_window(window(res_class="Chromium", res_name=KIOSK_WINDOW_CLASS))
    assert not is_app_window(window(res_class=KIOSK_WINDOW_CLASS.upper()))
    assert not is_app_window(None), "nothing in front, no bar"


@pytest.mark.parametrize("kind", ["desktop", "dock", "menu", "splashscreen", "toolbar"])
def test_the_bar_is_not_shown_for_what_is_not_an_app_window(kind: str) -> None:
    assert not is_app_window(window(kind=kind))


def test_the_bar_sits_in_the_top_right_corner_of_the_apps_screen() -> None:
    assert bar_origin(Rect(0, 0, 1920, 1080), 304) == (1616, 0)
    assert bar_origin(Rect(1920, 0, 1280, 1024), 304) == (2896, 0), "a second monitor"
    assert bar_origin(Rect(0, 0, 200, 150), 304) == (0, 0), "never off the screen"
    # A window that cannot fill the screen keeps the bar on its own title bar,
    # not over the launcher's ring in the corner.
    assert bar_origin(Rect(0, 0, 1280, 800), 304, Rect(320, 160, 648, 548)) == (664, 160)
    assert bar_origin(Rect(0, 0, 1280, 800), 304, Rect(0, 0, 1280, 800)) == (976, 0)
    assert bar_origin(Rect(0, 0, 1280, 800), 304, Rect(10, 50, 200, 150)) == (0, 50)


def test_the_bar_goes_on_a_dragged_windows_own_title_bar() -> None:
    """D-201: a window as wide as the screen but lower down, one half off the
    screen, one dragged to the bottom: the bar is on its title bar, all of the
    bar on the screen."""
    screen = Rect(0, 0, 1920, 1080)
    assert bar_origin(screen, 532, Rect(0, 200, 1920, 700)) == (1388, 200)
    assert bar_origin(screen, 532, Rect(1500, 300, 800, 600)) == (1388, 300)
    assert bar_origin(screen, 532, Rect(300, 1050, 800, 600)) == (568, 1080 - BUTTON_TARGET_PX)
    assert bar_origin(screen, 532, Rect(-200, -30, 600, 400)) == (0, 0)


def test_every_button_is_big_enough_for_a_four_year_old() -> None:
    """UX.md section 2.4: 64 x 64. The border is transparent, and still the button."""
    widths = re.search(r"border-width: (\d+)px (\d+)px (\d+)px (\d+)px;", BAR_CSS.decode())
    assert widths is not None
    top, right, bottom, left = (int(value) for value in widths.groups())
    assert top + BUTTON_FACE_PX + bottom == BUTTON_TARGET_PX >= 64
    assert left + BUTTON_FACE_PX + right == BUTTON_TARGET_PX
    assert f"min-width: {BUTTON_FACE_PX}px" in BAR_CSS.decode()
    assert f"min-height: {BUTTON_FACE_PX}px" in BAR_CSS.decode()


def test_the_bar_is_as_tall_as_the_title_bar_it_sits_in() -> None:
    """Over a maximised app the bar covers the title bar's strip, not the app."""
    data = (THEME_DIR / "close-active.png").read_bytes()
    _width, height = struct.unpack(">II", data[16:24])
    assert height == BUTTON_TARGET_PX


def test_the_bar_sits_flush_in_the_title_bar() -> None:
    """D-201: the owner saw a pale box breaking the title bar's bottom line. The
    bar draws the same two-pixel line, and each button's face sits where the
    title bar's own close button sat: three pixels from the top, three above
    the line."""
    css = BAR_CSS.decode()
    assert f"linear-gradient(to top, #E7DED5 {TITLE_LINE_PX}px" in css
    assert "border-color: transparent" in css, "a solid border would cover the line"
    widths = re.search(r"border-width: (\d+)px \d+px (\d+)px", css)
    assert widths is not None
    assert int(widths.group(1)) == 3
    assert int(widths.group(2)) == 3 + TITLE_LINE_PX
    # The same line in the theme's title bar, which the bar now continues.
    data = (THEME_DIR / "title-1-active.png").read_bytes()
    assert struct.unpack(">II", data[16:24]) == (8, BUTTON_TARGET_PX)


def test_the_slider_has_a_knob_for_a_fingertip() -> None:
    """D-201: a big knob, and the track's ends far enough in for it at 0 and 100."""
    css = BAR_CSS.decode()
    assert f"min-width: {KNOB_PX}px" in css and f"min-height: {KNOB_PX}px" in css
    assert KNOB_PX >= 40
    assert SLIDER_PAD_PX >= (KNOB_PX + 6 - TRACK_PX) // 2
    assert SLIDER_PX - 2 * SLIDER_PAD_PX >= 100, "a track a finger can travel along"


GTK3_PROPERTIES = {
    "background-color",
    "background-clip",
    "background-image",
    "border",
    "border-color",
    "border-image",
    "border-radius",
    "border-style",
    "border-width",
    "box-shadow",
    "color",
    "font-size",
    "font-weight",
    "margin",
    "min-height",
    "min-width",
    "opacity",
    "padding",
    "text-shadow",
}


def test_the_style_sheet_uses_only_what_gtk_3_reads() -> None:
    """GTK 3 refuses a whole sheet over one property it does not know, and the
    bar would then have buttons too small to hit. Checked here, since the suite
    has no GTK to load it with."""
    used = set(re.findall(r"^\s*([a-z-]+)\s*:", BAR_CSS.decode(), flags=re.MULTILINE))
    used |= set(re.findall(r"\{\s*([a-z-]+)\s*:", BAR_CSS.decode()))
    assert used <= GTK3_PROPERTIES, used - GTK3_PROPERTIES


def test_the_buttons_have_names_a_screen_reader_can_say() -> None:
    for locale in ("tr", "en"):
        tr = translator(locale)
        for key in ("appbar.home", "appbar.quieter", "appbar.louder", "appbar.close"):
            assert tr(key) != key
        assert "60" in tr("appbar.level", percent=60)
    assert translator("tr")("appbar.home") == "Ana ekran"
    assert translator("tr")("shell.open_apps") == "Açık"
    assert translator("en")("shell.open_apps") == "Open"


# ------------------------------------------------------------------- sound

PACTL_JSON = """[
 {"index":57,"driver":"PipeWire","client":"56","sink":48,
  "format":"pcm, format.sample_format = \\"\\\\\\"s16le\\\\\\"\\"",
  "corked":false,"mute":false,
  "volume":{"front-left":{"value":45875,"value_percent":"70%","db":"-9.29 dB"},
            "front-right":{"value":45875,"value_percent":"70%","db":"-9.29 dB"}},
  "properties":{"application.name":"frozen-bubble","application.process.id":"4242"}},
 {"index":60,"driver":"PipeWire","client":"59","sink":48,"corked":false,"mute":false,
  "volume":{"mono":{"value":65536,"value_percent":"100%","db":"0.00 dB"}},
  "properties":{"application.name":"pw-play","application.process.id":"5000"}},
 {"index":61,"driver":"PipeWire","client":"60","sink":48,"corked":false,"mute":true,
  "volume":{"front-left":{"value":32768,"value_percent":"50%","db":"-18 dB"}},
  "properties":{"application.name":"Chromium","application.process.id":"6001"}}
]"""

PACTL_TEXT = """Sink Input #57
\tDriver: PipeWire
\tOwner Module: n/a
\tClient: 56
\tSink: 48
\tSample Specification: s16le 2ch 44100Hz
\tFormat: pcm, format.sample_format = "\\"s16le\\""
\tCorked: no
\tMute: no
\tVolume: front-left: 45875 /  70% / -9.29 dB,   front-right: 45875 /  70% / -9.29 dB
\t        balance 0.00
\tProperties:
\t\tapplication.name = "frozen-bubble"
\t\tapplication.process.id = "4242"

Sink Input #60
\tMute: no
\tVolume: mono: 65536 / 100% / 0.00 dB
\tProperties:
\t\tapplication.process.id = "5000"

Sink Input #61
\tMute: yes
\tVolume: front-left: 32768 /  50% / -18.06 dB
\tProperties:
\t\tapplication.process.id = "6001"
"""

EXPECTED = [
    SinkInput(index=57, pid=4242, level=70, muted=False),
    SinkInput(index=60, pid=5000, level=100, muted=False),
    SinkInput(index=61, pid=6001, level=50, muted=True),
]

GAME_WINDOW = AppTarget(xid=0x3A00007, pid=4242, scope=GAME)
SITE_WINDOW = AppTarget(xid=0x4000001, pid=6000, scope=SITE)


def test_pactls_json_is_read() -> None:
    assert parse_sink_inputs_json(PACTL_JSON) == EXPECTED


def test_pactls_text_is_read_when_its_json_is_not() -> None:
    assert parse_sink_inputs_text(PACTL_TEXT) == EXPECTED


def test_json_that_is_not_a_list_of_streams_is_not_read() -> None:
    assert parse_sink_inputs_json("{not json") is None
    assert parse_sink_inputs_json('{"index": 1}') is None
    assert parse_sink_inputs_json('[{"index": "x"}, 3]') == []


class Pactl:
    """A sound server with three streams: the game, Zio's voice, a muted site."""

    def __init__(self, *, json_works: bool = True, answers: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.json_works = json_works
        self.answers = answers

    def __call__(self, argv: Sequence[str]) -> str | None:
        self.calls.append(list(argv))
        if not self.answers:
            return None
        if list(argv) == ["pactl", "-f", "json", "list", "sink-inputs"]:
            return PACTL_JSON if self.json_works else "[{broken"
        if list(argv) == ["pactl", "list", "sink-inputs"]:
            return PACTL_TEXT
        return ""

    def changes(self) -> list[list[str]]:
        return [call for call in self.calls if call[1].startswith("set-")]


def test_the_streams_are_listed_from_json_and_from_text() -> None:
    assert list_sink_inputs(Pactl()) == EXPECTED
    assert list_sink_inputs(Pactl(json_works=False)) == EXPECTED
    assert list_sink_inputs(Pactl(answers=False)) is None


def test_an_apps_streams_are_found_by_its_scope_not_its_window() -> None:
    """Chromium plays from a helper process, not the one that owns the window."""
    assert [i.index for i in app_inputs(EXPECTED, GAME_WINDOW, scope_of)] == [57]
    assert [i.index for i in app_inputs(EXPECTED, SITE_WINDOW, scope_of)] == [61]


def test_zio_and_sio_are_never_one_of_an_apps_streams() -> None:
    for target in (GAME_WINDOW, SITE_WINDOW):
        assert 60 not in [i.index for i in app_inputs(EXPECTED, target, scope_of)]


def test_a_window_with_no_scope_is_matched_by_its_own_process() -> None:
    loose = AppTarget(xid=1, pid=4242, scope=None)
    assert [i.index for i in app_inputs(EXPECTED, loose, scope_of)] == [57]
    assert app_inputs(EXPECTED, AppTarget(xid=1, pid=0, scope=None), scope_of) == []


@pytest.mark.parametrize(
    ("level", "louder", "expected"),
    [
        (70, True, 80),
        (70, False, 60),
        (65, True, 70),
        (65, False, 60),
        (95, True, 100),
        (100, True, 100),
        (150, True, 150),
        (150, False, 140),
        (5, False, 0),
        (0, False, 0),
        (0, True, 10),
    ],
)
def test_the_volume_steps_by_ten_onto_the_tens(level: int, louder: bool, expected: int) -> None:
    assert stepped(level, louder=louder) == expected


def test_louder_turns_only_the_apps_own_sound_up() -> None:
    """The machine's volume and the voice stay where they were: a warning must
    be as audible after the child turned the game down as before."""
    pactl = Pactl()
    state = change_volume(GAME_WINDOW, louder=True, scope_of=scope_of, run=pactl)
    assert state == AudioState(has_sound=True, level=80)
    assert pactl.changes() == [["pactl", "set-sink-input-volume", "57", "80%"]]


def test_quieter_turns_it_down() -> None:
    pactl = Pactl()
    assert change_volume(GAME_WINDOW, louder=False, scope_of=scope_of, run=pactl).level == 60
    assert pactl.changes() == [["pactl", "set-sink-input-volume", "57", "60%"]]


def test_louder_on_a_muted_app_unmutes_it() -> None:
    pactl = Pactl()
    state = change_volume(SITE_WINDOW, louder=True, scope_of=scope_of, run=pactl)
    assert state.level == 10, "it was silent, so it starts from silence"
    assert ["pactl", "set-sink-input-volume", "61", "10%"] in pactl.changes()
    assert ["pactl", "set-sink-input-mute", "61", "0"] in pactl.changes()


def test_an_app_that_makes_no_sound_is_left_alone() -> None:
    pactl = Pactl()
    quiet = AppTarget(xid=9, pid=8000, scope="biokids-app-tuxmath-5.scope")
    assert change_volume(quiet, louder=True, scope_of=scope_of, run=pactl) == NO_SOUND
    assert pactl.changes() == []
    assert read_audio(quiet, scope_of, run=pactl) == NO_SOUND
    assert read_audio(GAME_WINDOW, scope_of, run=pactl) == AudioState(True, 70)


def test_no_sound_server_is_no_sound_not_a_crash() -> None:
    assert read_audio(GAME_WINDOW, scope_of, run=Pactl(answers=False)) == NO_SOUND


def test_the_slider_sets_the_apps_own_sound_where_it_was_dragged() -> None:
    """D-201: the slider, not a step - and still only the app's own streams."""
    pactl = Pactl()
    assert set_volume(GAME_WINDOW, 35, scope_of=scope_of, run=pactl) == AudioState(True, 35)
    assert pactl.changes() == [["pactl", "set-sink-input-volume", "57", "35%"]]


def test_a_slider_dragged_up_on_a_muted_app_unmutes_it() -> None:
    pactl = Pactl()
    assert set_volume(SITE_WINDOW, 40, scope_of=scope_of, run=pactl).level == 40
    assert ["pactl", "set-sink-input-mute", "61", "0"] in pactl.changes()


def test_the_slider_never_goes_past_full_or_below_silence() -> None:
    pactl = Pactl()
    assert set_volume(GAME_WINDOW, 180, scope_of=scope_of, run=pactl).level == 100
    assert set_volume(GAME_WINDOW, -5, scope_of=scope_of, run=pactl).level == 0
    quiet = AppTarget(xid=9, pid=8000, scope=None)
    assert set_volume(quiet, 50, scope_of=scope_of, run=pactl) == NO_SOUND


def test_the_level_under_the_finger() -> None:
    """The knob's centre travels from half a knob in at one end to the other."""
    track_x, width = SLIDER_PAD_PX, SLIDER_PX - 2 * SLIDER_PAD_PX
    start = track_x + TRACK_PX / 2
    end = track_x + width - TRACK_PX / 2
    assert level_at(start, track_x, width, TRACK_PX) == 0
    assert level_at(end, track_x, width, TRACK_PX) == 100
    assert level_at((start + end) / 2, track_x, width, TRACK_PX) == 50
    assert level_at(0, track_x, width, TRACK_PX) == 0, "left of the track"
    assert level_at(SLIDER_PX, track_x, width, TRACK_PX) == 100, "right of it"
    assert level_at(50, 0, 0) == 0, "a slider with no width yet"


def test_a_drag_sends_only_the_latest_level_one_at_a_time() -> None:
    """Dozens of positions a second, and a `pactl` run for each would pile up."""
    sender = LevelSender()
    sender.want(30)
    sender.want(40)
    assert sender.next() == 40
    sender.want(50)
    sender.want(60)
    assert sender.next() is None, "one is on its way"
    assert sender.settling()
    sender.done(40)
    assert sender.waiting()
    assert sender.next() == 60
    sender.done(60)
    assert not sender.waiting() and not sender.settling()
    assert sender.next() is None, "nothing new"


# --------------------------------------------------------------- the frame


def test_a_format_32_property_is_read_as_gdk_hands_it_over() -> None:
    """Xlib keeps 32-bit items in C longs: eight bytes each on amd64."""
    longs = b"".join(v.to_bytes(8, sys.byteorder) for v in (4, 4, 64, 4))
    ints = b"".join(v.to_bytes(4, sys.byteorder) for v in (4, 4, 64, 4))
    assert decode_cardinals(longs) == (4, 4, 64, 4)
    assert decode_cardinals(ints) == (4, 4, 64, 4)
    assert decode_cardinals(b"") is None
    assert decode_cardinals(b"\x00" * 10) is None


def test_frame_extents_that_are_not_a_frame_are_not_used() -> None:
    assert extents_from((4, 4, 64, 4)) == Extents(left=4, right=4, top=64, bottom=4)
    assert extents_from((0, 0, 64)) is None
    assert extents_from((0, 0, 99999, 0)) is None
    assert extents_from(None) is None


DRAGGED_CLIENT = Rect(304, 264, 1272, 716)
"""A Chromium window dragged out of maximisation: its contents, under a 64-pixel
title bar and inside 4-pixel borders."""
XFWM4_FRAME = Extents(left=4, right=4, top=64, bottom=4)


def test_the_frame_is_the_contents_and_the_window_managers_frame() -> None:
    """The owner saw the bar a title bar's height too low over a dragged window:
    placed by the window's contents, not by its frame."""
    extents = window_extents(XFWM4_FRAME, None, fallback=NO_EXTENTS)
    assert frame_rect(DRAGGED_CLIENT, extents) == Rect(300, 200, 1280, 784)


def test_libwnck_is_the_fallback_when_the_x_server_cannot_say() -> None:
    reported = Rect(300, 200, 1280, 784)
    assert implied_extents(DRAGGED_CLIENT, reported) == XFWM4_FRAME
    fallback = implied_extents(DRAGGED_CLIENT, reported)
    assert window_extents(None, None, fallback) == XFWM4_FRAME


def test_a_window_with_its_own_title_bar_loses_its_invisible_shadow() -> None:
    """A window that draws its own title bar has no frame from the window manager
    and a transparent shadow inside its edges, which is not where its corner is."""
    shadow = Extents(left=23, right=23, top=17, bottom=29)
    extents = window_extents(NO_EXTENTS, shadow, fallback=NO_EXTENTS)
    assert frame_rect(Rect(100, 100, 846, 646), extents) == Rect(123, 117, 800, 600)


def test_an_undecorated_window_is_its_contents() -> None:
    extents = window_extents(NO_EXTENTS, None, fallback=XFWM4_FRAME)
    assert frame_rect(DRAGGED_CLIENT, extents) == DRAGGED_CLIENT
    assert frame_rect(Rect(0, 0, 10, 10), Extents(-6, -6, 0, 0)) is None


# ------------------------------------------------------ a window that moves

MAXIMIZED_BITS = 0b11


def test_losing_maximisation_is_seen_in_libwncks_state_change() -> None:
    assert lost_maximize(changed=0b11, new_state=0b00, maximized_bits=MAXIMIZED_BITS)
    assert not lost_maximize(changed=0b11, new_state=0b11, maximized_bits=MAXIMIZED_BITS)
    assert not lost_maximize(changed=0b100, new_state=0b00, maximized_bits=MAXIMIZED_BITS)


def test_the_bar_leaves_a_window_being_dragged_and_comes_back_when_it_stops() -> None:
    """D-201: "the close button and the sound controls arrive late"."""
    follower = Follower()
    assert follower.changed(7, 100.0, maximized=False, pressed=True) is True
    assert follower.hiding(7, 100.1)
    assert follower.changed(7, 100.2, maximized=False, pressed=True) is True
    step, wait = follower.settle(100.3, maximized=False, can_maximize=False, pressed=True)
    assert step is FollowStep.WAIT and wait == pytest.approx(SETTLE_S - 0.1)
    step, _ = follower.settle(100.5, maximized=False, can_maximize=False, pressed=True)
    assert step is FollowStep.WAIT, "still under the finger"
    step, _ = follower.settle(100.8, maximized=False, can_maximize=False, pressed=False)
    assert step is FollowStep.SHOW, "a fixed-size window is only re-placed"
    assert not follower.hiding(7, 100.8)


def test_a_maximised_window_dragged_out_of_it_fills_the_screen_again() -> None:
    """xfwm4 un-maximises a window dragged by its title bar; a child never needs
    a floating window."""
    follower = Follower()
    # libwnck still says maximised at the drag's first step...
    follower.changed(7, 100.0, maximized=True, pressed=True)
    follower.changed(7, 100.1, maximized=False, pressed=True)
    step, _ = follower.settle(100.6, maximized=False, can_maximize=True, pressed=False)
    assert step is FollowStep.MAXIMIZE
    assert follower.hiding(7, 100.6), "no bar on the floating window meanwhile"
    # ...and the window manager's answer is one more change.
    follower.changed(7, 100.65, maximized=True, pressed=False)
    step, _ = follower.settle(100.95, maximized=True, can_maximize=True, pressed=False)
    assert step is FollowStep.SHOW


def test_the_state_change_says_it_was_maximised_when_the_geometry_did_not() -> None:
    follower = Follower()
    follower.changed(7, 100.0, maximized=False, pressed=True)
    follower.changed(7, 100.0, maximized=False, pressed=True, unmaximized=True)
    step, _ = follower.settle(100.5, maximized=False, can_maximize=True, pressed=False)
    assert step is FollowStep.MAXIMIZE


def test_maximising_is_asked_once_and_not_fought_over() -> None:
    follower = Follower()
    follower.changed(7, 100.0, maximized=True, pressed=True)
    follower.settle(100.5, maximized=False, can_maximize=True, pressed=False)
    # The window manager said no: the bar goes back on the window as it is.
    step, _ = follower.settle(100.8, maximized=False, can_maximize=True, pressed=False)
    assert step is FollowStep.SHOW


def test_an_app_that_resizes_itself_is_not_maximised_by_force() -> None:
    """No finger on it: the app did it, and the bar does not argue."""
    follower = Follower()
    follower.changed(7, 100.0, maximized=True, pressed=False, unmaximized=True)
    step, _ = follower.settle(100.3, maximized=False, can_maximize=True, pressed=False)
    assert step is FollowStep.SHOW


def test_a_window_that_never_stops_gets_the_bar_back_anyway() -> None:
    """The way out is never gone for long: an animating window after a moment,
    a drag held on and on after a few seconds."""
    restless = Follower()
    restless.changed(7, 100.0, maximized=False, pressed=False)
    assert restless.changed(7, 100.0 + RESTLESS_S - 0.1, maximized=False, pressed=False)
    assert not restless.changed(7, 100.0 + RESTLESS_S, maximized=False, pressed=False)

    held = Follower()
    held.changed(7, 100.0, maximized=False, pressed=True)
    assert held.hiding(7, 100.0 + RESTLESS_S + 1)
    assert not held.hiding(7, 100.0 + HELD_AT_MOST_S)
    step, _ = held.settle(
        100.0 + HELD_AT_MOST_S, maximized=False, can_maximize=False, pressed=True
    )
    assert step is FollowStep.SHOW


def test_another_window_starts_afresh() -> None:
    follower = Follower()
    follower.changed(7, 100.0, maximized=True, pressed=True)
    assert not follower.hiding(8, 100.1), "the bar over another window is not held back"
    follower.changed(8, 100.2, maximized=False, pressed=False)
    step, _ = follower.settle(100.6, maximized=False, can_maximize=True, pressed=False)
    assert step is FollowStep.SHOW, "8 was never maximised, nor dragged"


# --------------------------------------------------------- time and clock


def test_the_minutes_come_from_the_daemon_rounded_up() -> None:
    assert time_left({"remaining_seconds": 12 * 60, "managed": True}) == TimeLeft(
        12, TimeTone.OK
    )
    assert time_left({"remaining_seconds": 61}) == TimeLeft(2, TimeTone.WARN)
    assert time_left({"remaining_seconds": 0}) == TimeLeft(0, TimeTone.DANGER)


@pytest.mark.parametrize(
    ("seconds", "tone"),
    [
        (301, TimeTone.OK),
        (300, TimeTone.OK),
        (299, TimeTone.WARN),
        (60, TimeTone.WARN),
        (59, TimeTone.DANGER),
        (0, TimeTone.DANGER),
    ],
)
def test_the_minutes_turn_orange_then_red(seconds: int, tone: TimeTone) -> None:
    """Under five minutes, under one: as the launcher's banners."""
    left = time_left({"remaining_seconds": seconds})
    assert left is not None and left.tone is tone


@pytest.mark.parametrize(
    "state",
    [
        None,
        {},
        {"offline": True},
        {"managed": False, "remaining_seconds": 600},
        {"remaining_seconds": "600"},
        {"remaining_seconds": True},
    ],
)
def test_no_minutes_when_the_launcher_cannot_say(state: dict[str, Any] | None) -> None:
    assert time_left(state) is None


def test_the_clock_is_for_8_to_10_as_on_the_launcher() -> None:
    """UX.md section 2.2: a number a four-year-old cannot read is noise."""
    assert shows_clock({"age_band": "8-10"})
    assert not shows_clock({"age_band": "4-7"})
    assert not shows_clock(None)
    assert clock_text(time.struct_time((2026, 10, 8, 9, 5, 0, 3, 281, 0))) == "09:05"


def test_the_minutes_are_asked_of_this_childs_own_launcher(config: Config) -> None:
    url = state_url(config)
    assert url.startswith(f"http://{config.shell.host}:") and url.endswith("/state")


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.body


def test_the_launchers_state_is_read_on_loopback_only() -> None:
    """Principle 1: the bar asks the launcher on this machine, nothing else."""
    asked: list[str] = []

    def opener(url: str, timeout: float) -> _Response:
        asked.append(url)
        return _Response(b'{"remaining_seconds": 600}')

    assert fetch_state("http://127.0.0.1:7880/state", open_url=opener) == {
        "remaining_seconds": 600
    }
    assert fetch_state("http://example.com/state", open_url=opener) is None
    assert fetch_state("https://127.0.0.1:7880/state", open_url=opener) is None
    assert asked == ["http://127.0.0.1:7880/state"]


def test_a_launcher_that_does_not_answer_is_no_minutes() -> None:
    def down(_url: str, timeout: float) -> _Response:
        raise ConnectionRefusedError

    assert fetch_state("http://127.0.0.1:7880/state", open_url=down) is None
    assert (
        fetch_state("http://127.0.0.1:7880/state", open_url=lambda *_a, **_k: _Response(b"[1]"))
        is None
    )
    assert (
        fetch_state("http://127.0.0.1:7880/state", open_url=lambda *_a, **_k: _Response(b"<"))
        is None
    )


# ------------------------------------------------------------------- close

OPEN = {GAME_WINDOW.xid: GAME, 0x5000001: None}
"""The game's window and the launcher's. The launcher is never an app window,
but a close must not depend on that."""


def test_a_tap_asks_the_window_to_close_first() -> None:
    """An app may want to ask "save your drawing?", and must be able to."""
    closer = Closer()
    assert closer.tap(GAME_WINDOW, 100.0, OPEN) is CloseAction.ASK
    assert closer.tick(101.0, OPEN) == (CloseStep.WAIT, GAME)


def test_an_app_that_closes_is_left_alone() -> None:
    closer = Closer()
    closer.tap(GAME_WINDOW, 100.0, OPEN)
    assert closer.tick(101.0, {0x5000001: None}) == (CloseStep.DONE, GAME)
    assert closer.pending is None


def test_an_app_still_there_after_three_seconds_is_stopped() -> None:
    """Frozen Bubble: the owner's session left it running."""
    closer = Closer()
    closer.tap(GAME_WINDOW, 100.0, OPEN)
    assert closer.tick(100.0 + CLOSE_GRACE_S - 0.1, OPEN)[0] is CloseStep.WAIT
    assert closer.tick(100.0 + CLOSE_GRACE_S, OPEN) == (CloseStep.STOP, GAME)
    # Stopped once, then waited for: TERM, the grace period, KILL.
    assert closer.tick(100.0 + CLOSE_GRACE_S + 5, OPEN)[0] is CloseStep.WAIT
    assert closer.tick(100.0 + CLOSE_GRACE_S + 6, {})[0] is CloseStep.DONE


def test_an_app_that_will_not_go_even_when_stopped_is_given_up_on() -> None:
    closer = Closer()
    closer.tap(GAME_WINDOW, 100.0, OPEN)
    closer.tick(100.0 + CLOSE_GRACE_S, OPEN)
    assert closer.tick(100.0 + CLOSE_GRACE_S + STOP_GIVE_UP_S, OPEN)[0] is CloseStep.GIVE_UP
    assert closer.pending is None


def test_an_app_that_asks_a_question_is_left_to_the_child() -> None:
    """It answered the close with a window of its own: "save your drawing?"."""
    closer = Closer()
    closer.tap(GAME_WINDOW, 100.0, OPEN)
    asking = {**OPEN, 0x3A00042: GAME}
    assert closer.tick(100.0 + CLOSE_GRACE_S + 10, asking)[0] is CloseStep.WAIT
    assert closer.tick(100.0 + QUESTION_WAIT_S, asking)[0] is CloseStep.GIVE_UP


def test_a_second_tap_stops_the_app_at_once() -> None:
    """The way a child says "I really mean it", even with the question open."""
    closer = Closer()
    closer.tap(GAME_WINDOW, 100.0, OPEN)
    question = AppTarget(xid=0x3A00042, pid=4242, scope=GAME)
    assert closer.tap(question, 100.0 + DOUBLE_TAP_S, OPEN) is CloseAction.STOP
    assert closer.tick(100.0 + DOUBLE_TAP_S + 1, OPEN)[0] is CloseStep.WAIT


def test_a_tap_that_bounced_is_one_tap() -> None:
    closer = Closer()
    closer.tap(GAME_WINDOW, 100.0, OPEN)
    assert closer.tap(GAME_WINDOW, 100.0 + DOUBLE_TAP_S / 2, OPEN) is CloseAction.NOTHING


def test_a_tap_on_another_app_asks_that_one() -> None:
    closer = Closer()
    closer.tap(GAME_WINDOW, 100.0, OPEN)
    assert closer.tap(SITE_WINDOW, 101.5, OPEN) is CloseAction.ASK
    assert closer.pending is not None and closer.pending.target == SITE_WINDOW


def test_a_window_with_no_scope_is_only_ever_asked() -> None:
    """Nothing to stop that is certainly the app's: the bar never guesses."""
    loose = AppTarget(xid=7, pid=4242, scope=None)
    closer = Closer()
    assert closer.tap(loose, 100.0, {7: None}) is CloseAction.ASK
    assert closer.tap(loose, 102.0, {7: None}) is CloseAction.ASK
    assert closer.tick(102.0 + CLOSE_GRACE_S, {7: None}) == (CloseStep.GIVE_UP, None)


# ------------------------------------------------------- the process itself


def test_the_launcher_does_not_load_gtk() -> None:
    """The launcher imports this package; GTK is loaded only in the bar's process."""
    probe = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "import biokids_shell.__main__, biokids_shell.appbar; "
        "print(sorted(m for m in sys.modules if m == 'gi' or m.endswith('appbar.ui')))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe, str(ROOT / "src")],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert done.stdout.strip() == "[]"
    assert appbar.MODULE == "biokids_shell.appbar"


# ---------------------------------------------- the window, on a fake GTK


class _Kind:
    def __init__(self, nick: str) -> None:
        self.value_nick = nick


class _Win:
    """A libwnck window, as much of it as the bar uses."""

    def __init__(self, xid: int, pid: int, res_class: str, kind: str = "normal") -> None:
        self.xid, self.pid, self.res_class, self.kind = xid, pid, res_class, kind
        self.transient: _Win | None = None
        self.calls: list[Any] = []
        self.frame = Rect(640, 300, 640, 480)
        self.extents = XFWM4_FRAME
        self.maximized = False
        self.fullscreen = False
        self.actions = 1
        """Wnck.WindowActions.MAXIMIZE is 1 on the fake GTK; 0 for a fixed size."""
        self.handlers: dict[str, Any] = {}

    def get_client_window_geometry(self) -> tuple[int, int, int, int]:
        e = self.extents
        f = self.frame
        return (
            f.x + e.left,
            f.y + e.top,
            f.width - e.left - e.right,
            f.height - e.top - e.bottom,
        )

    def is_maximized(self) -> bool:
        return self.maximized

    def is_fullscreen(self) -> bool:
        return self.fullscreen

    def get_actions(self) -> int:
        return self.actions

    def maximize(self) -> None:
        self.calls.append("maximize")

    def connect(self, name: str, handler: Any) -> int:
        self.handlers[name] = handler
        return len(self.handlers)

    def disconnect(self, _handler_id: int) -> None:
        return None

    def get_xid(self) -> int:
        return self.xid

    def get_pid(self) -> int:
        return self.pid

    def get_class_group_name(self) -> str:
        return self.res_class

    def get_class_instance_name(self) -> str:
        return self.res_class.lower()

    def get_window_type(self) -> _Kind:
        return _Kind(self.kind)

    def get_transient(self) -> _Win | None:
        return self.transient

    def get_geometry(self) -> tuple[int, int, int, int]:
        return (self.frame.x, self.frame.y, self.frame.width, self.frame.height)

    def minimize(self) -> None:
        self.calls.append("minimize")

    def activate(self, stamp: int) -> None:
        self.calls.append(("activate", stamp))

    def activate_transient(self, stamp: int) -> None:
        self.calls.append(("activate_transient", stamp))

    def close(self, stamp: int) -> None:
        self.calls.append(("close", stamp))


class _Screen:
    def __init__(self, *windows: _Win) -> None:
        self.windows = list(windows)
        self.active: _Win | None = None
        self.handlers: dict[str, Any] = {}

    def connect(self, name: str, handler: Any) -> None:
        self.handlers[name] = handler

    def force_update(self) -> None:
        return None

    def get_active_window(self) -> _Win | None:
        return self.active

    def get_windows(self) -> list[_Win]:
        return list(self.windows)

    def get_windows_stacked(self) -> list[_Win]:
        return list(self.windows)


@pytest.fixture
def fake_gtk(monkeypatch: pytest.MonkeyPatch) -> Any:
    """gi.repository made of mocks: what the bar does with GTK, not GTK itself."""
    gi = types.ModuleType("gi")
    gi.require_version = lambda *_args: None  # type: ignore[attr-defined]
    repository = types.ModuleType("gi.repository")
    for name in ("Gdk", "GdkX11", "GLib", "Gtk", "Wnck"):
        setattr(repository, name, mock.MagicMock(name=name))
    repository.GLib.Error = type("GLibError", (Exception,), {})  # type: ignore[attr-defined]
    idle: list[tuple[Any, tuple[Any, ...]]] = []
    repository.GLib.idle_add.side_effect = lambda fn, *a: idle.append((fn, a))  # type: ignore[attr-defined]
    timers: list[tuple[int, Any]] = []

    def add_timer(ms: int, fn: Any, *_args: Any) -> int:
        timers.append((ms, fn))
        return len(timers)

    repository.GLib.timeout_add.side_effect = add_timer  # type: ignore[attr-defined]
    repository.GLib.timeout_add_seconds.side_effect = (  # type: ignore[attr-defined]
        lambda seconds, fn, *_a: add_timer(seconds * 1000, fn)
    )
    repository.Gtk.get_current_event_time.return_value = 1234  # type: ignore[attr-defined]
    repository.GdkX11.x11_get_server_time.return_value = 99  # type: ignore[attr-defined]
    window = repository.Gtk.Window.return_value  # type: ignore[attr-defined]
    natural = mock.Mock(width=304, height=BUTTON_TARGET_PX)
    window.get_preferred_size.return_value = (natural, natural)
    window.get_scale_factor.return_value = 1
    monitor = mock.Mock(x=0, y=0, width=1920, height=1080)
    display = repository.Gdk.Display.get_default.return_value  # type: ignore[attr-defined]
    display.get_monitor_at_point.return_value.get_geometry.return_value = monitor
    gi.repository = repository  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.delitem(sys.modules, "biokids_shell.appbar.ui", raising=False)

    from biokids_shell.appbar import ui

    monkeypatch.setattr(ui, "scope_of_pid", lambda pid, _uid=None: scope_of(pid))
    yield types.SimpleNamespace(ui=ui, repo=repository, idle=idle, timers=timers)
    sys.modules.pop("biokids_shell.appbar.ui", None)
    if hasattr(appbar, "ui"):
        delattr(appbar, "ui")


def _deliver(bar: Any, idle: list[tuple[Any, tuple[Any, ...]]]) -> None:
    """Let the workers finish, then run what they handed to the main loop."""
    for _ in range(3):
        for pool in (bar._sound, bar._stops, bar._clock):
            pool.submit(lambda: None).result()
        while idle:
            handler, args = idle.pop(0)
            handler(*args)


def _fire(timers: list[tuple[int, Any]], name: str) -> int:
    """Run the timers waiting to call `name`, as the main loop does when they are due."""
    due = [entry for entry in timers if getattr(entry[1], "__name__", "") == name]
    for entry in due:
        timers.remove(entry)
    for _ms, callback in due:
        callback()
    return len(due)


class _FakeX:
    """The X server, as XReader reads it."""

    def __init__(
        self, client: Rect | None, decorations: Extents | None, shadow: Extents | None = None
    ) -> None:
        self.client, self.decorations, self.shadow = client, decorations, shadow

    def client_rect(self, _xid: int, _scale: int = 1) -> Rect | None:
        return self.client

    def extents(self, _xid: int, name: str) -> Extents | None:
        return self.decorations if name == "_NET_FRAME_EXTENTS" else self.shadow


MAXIMIZED_FRAME = Extents(left=0, right=0, top=64, bottom=0)
"""xfwm4 with `borderless_maximize`: a maximised window keeps only its title bar."""


def _bar_over(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch, window: _Win, **kwargs: Any
) -> Any:
    ui = fake_gtk.ui
    screen = _Screen(_Win(1, 7000, "biokids-kiosk"), window)
    screen.active = window
    monkeypatch.setattr(ui, "read_audio", lambda target, scope_of: AudioState(True, 70))
    bar = ui.AppBarUi(translator("tr"), screen, uid=UID, **kwargs)
    clock = [100.0]
    finger = [False]
    bar.now = lambda: clock[0]
    bar.pointer_pressed = lambda: finger[0]
    bar.test_clock, bar.test_finger = clock, finger
    bar.refresh(raise_bar=True)
    _deliver(bar, fake_gtk.idle)
    return bar


def test_a_dragged_window_takes_the_bar_only_once_it_stops(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-201: "the close button and the sound controls arrive late, it looks very
    bad". The bar leaves at the first step of the drag and comes back on the
    window once it is still; a window that filled the screen fills it again."""
    browser = _Win(4, 6001, "Chromium")
    browser.frame, browser.extents, browser.maximized = (
        Rect(0, 0, 1920, 1080),
        MAXIMIZED_FRAME,
        True,
    )
    bar = _bar_over(fake_gtk, monkeypatch, browser)
    clock, finger = bar.test_clock, bar.test_finger
    assert bar.shown and bar.origin == (1616, 0)

    # A finger on the title bar: xfwm4 takes the window out of maximisation.
    finger[0] = True
    browser.maximized = False
    browser.frame, browser.extents = Rect(300, 200, 1280, 784), XFWM4_FRAME
    browser.handlers["state-changed"](browser, 1, 0)
    assert not bar.shown, "gone the moment the window starts to move"
    clock[0] += 0.1
    browser.frame = Rect(350, 230, 1280, 784)
    browser.handlers["geometry-changed"](browser)
    bar._tick()
    assert not bar.shown, "not brought back by the next tick either"

    clock[0] += 0.3
    assert _fire(fake_gtk.timers, "_settle") == 1
    assert not bar.shown, "the finger is still down"

    finger[0] = False
    clock[0] += 0.3
    _fire(fake_gtk.timers, "_settle")
    assert browser.calls == ["maximize"]
    assert not bar.shown, "not on the floating window while it fills the screen again"

    browser.maximized = True
    browser.frame, browser.extents = Rect(0, 0, 1920, 1080), MAXIMIZED_FRAME
    browser.handlers["geometry-changed"](browser)
    clock[0] += 0.3
    _fire(fake_gtk.timers, "_settle")
    assert bar.shown and bar.origin == (1616, 0)
    bar.shutdown()


def test_a_fixed_size_window_dragged_is_only_followed(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frozen Bubble cannot fill the screen: the bar goes back on its title bar."""
    game = _Win(2, 4242, "Frozen-bubble")
    game.actions = 0
    bar = _bar_over(fake_gtk, monkeypatch, game)
    clock, finger = bar.test_clock, bar.test_finger
    assert bar.origin == (976, 300)

    finger[0] = True
    game.frame = Rect(100, 100, 640, 480)
    game.handlers["geometry-changed"](game)
    assert not bar.shown
    finger[0] = False
    clock[0] += 0.3
    _fire(fake_gtk.timers, "_settle")
    assert bar.shown and bar.origin == (100 + 640 - 304, 100)
    assert "maximize" not in game.calls
    bar.shutdown()


def test_the_bar_goes_by_the_frame_the_x_server_reports(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's screenshot: the bar a title bar's height below a dragged
    Chromium window's top. However libwnck came to its number, the contents as
    the X server has them and the frame xfwm4 put around them say where the
    corner is."""
    browser = _Win(4, 6001, "Chromium")
    browser.frame, browser.extents = DRAGGED_CLIENT, NO_EXTENTS  # libwnck: no frame
    bar = _bar_over(fake_gtk, monkeypatch, browser)
    assert bar.origin == (1272, 264), "what libwnck alone gives"

    bar.xserver = _FakeX(DRAGGED_CLIENT, XFWM4_FRAME)
    bar.refresh(raise_bar=False)
    assert bar.origin == (300 + 1280 - 304, 200)

    bar.xserver = _FakeX(None, None)  # the X server could not be asked
    bar.refresh(raise_bar=False)
    assert bar.origin == (1272, 264), "libwnck, then, as before"
    bar.shutdown()


def test_the_slider_follows_the_finger_and_the_sound_follows_the_slider(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = fake_gtk.ui
    sent: list[int] = []

    def set_level(target: AppTarget, level: int, scope_of: Any) -> AudioState:
        sent.append(level)
        return AudioState(True, level)

    monkeypatch.setattr(ui, "set_volume", set_level)
    bar = _bar_over(fake_gtk, monkeypatch, _Win(2, 4242, "Frozen-bubble"))
    assert bar.audio == AudioState(True, 70)
    bar.slider.get_allocation.return_value = mock.Mock(x=0, width=SLIDER_PX)
    bar.slider_box.get_allocation.return_value = mock.Mock(x=0)
    span = SLIDER_PX - 2 * SLIDER_PAD_PX - TRACK_PX

    def at(level: int) -> mock.Mock:
        return mock.Mock(x=SLIDER_PAD_PX + TRACK_PX / 2 + span * level / 100)

    bar._on_slide_press(None, at(30))
    bar._on_slide_motion(None, at(40))
    bar.slider.set_value.assert_called_with(40)
    assert sent == [], "the sound server is told at most every 80 ms"
    _fire(fake_gtk.timers, "_send_level")
    bar._on_slide_motion(None, at(55))
    bar._on_slide_motion(None, at(60))
    _fire(fake_gtk.timers, "_send_level")  # 40 is still on its way
    _deliver(bar, fake_gtk.idle)
    _fire(fake_gtk.timers, "_send_level")
    bar._on_slide_release(None, at(60))
    _deliver(bar, fake_gtk.idle)
    _fire(fake_gtk.timers, "_send_level")
    _deliver(bar, fake_gtk.idle)
    assert sent == [40, 60], "only the latest, one at a time"

    # A poll taken before the sound server heard the finger does not move the knob back.
    bar.slider.set_value.reset_mock()
    bar.sender.want(80)
    bar._show_audio(AudioState(True, 60))
    bar.slider.set_value.assert_not_called()
    bar.shutdown()


def test_the_buttons_still_step_by_ten_and_move_the_knob(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = fake_gtk.ui
    stepped_to: list[bool] = []

    def change(target: AppTarget, *, louder: bool, scope_of: Any) -> AudioState:
        stepped_to.append(louder)
        return AudioState(True, 70)

    monkeypatch.setattr(ui, "change_volume", change)
    bar = _bar_over(fake_gtk, monkeypatch, _Win(2, 4242, "Frozen-bubble"))
    bar.slider.get_value.return_value = 60
    bar._on_volume(louder=True)
    bar.slider.set_value.assert_called_with(70)
    _deliver(bar, fake_gtk.idle)
    assert stepped_to == [True]
    bar.shutdown()


def test_an_app_with_no_sound_cannot_be_slid(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bar = _bar_over(fake_gtk, monkeypatch, _Win(2, 4242, "Frozen-bubble"))
    bar._show_audio(NO_SOUND)
    bar.slider.set_sensitive.assert_called_with(False)
    bar._on_slide_press(None, mock.Mock(x=80))
    assert not bar._sliding and bar.sender.wanted is None
    bar.shutdown()


@pytest.mark.parametrize(("band", "clock_shown"), [("8-10", True), ("4-7", False)])
def test_the_bar_shows_the_minutes_left_and_for_8_to_10_the_clock(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch, band: str, clock_shown: bool
) -> None:
    fake_gtk.repo.Gtk.Label.side_effect = lambda *_a, **_k: mock.MagicMock()
    fake_gtk.repo.Gtk.Box.side_effect = lambda *_a, **_k: mock.MagicMock()
    asked: list[str] = []

    def state(url: str) -> dict[str, Any]:
        asked.append(url)
        return {"remaining_seconds": 4 * 60 + 10, "managed": True, "age_band": band}

    monkeypatch.setattr(fake_gtk.ui, "fetch_state", state)
    url = "http://127.0.0.1:7880/state"
    bar = _bar_over(fake_gtk, monkeypatch, _Win(2, 4242, "Frozen-bubble"), state_url=url)
    assert asked == [url]
    bar.time_label.set_text.assert_called_with("⏳ 5 dk")
    bar.info.get_style_context().add_class.assert_any_call("warn")
    bar.clock_label.set_visible.assert_called_with(clock_shown)
    bar.shutdown()


def test_the_bar_follows_the_app_in_front_and_hides_for_the_launcher(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = fake_gtk.ui
    kiosk, game = _Win(1, 7000, "biokids-kiosk"), _Win(2, 4242, "Frozen-bubble")
    screen = _Screen(kiosk, game)
    monkeypatch.setattr(ui, "read_audio", lambda target, scope_of: AudioState(True, 70))
    bar = ui.AppBarUi(translator("tr"), screen, uid=UID)

    screen.active = kiosk
    screen.handlers["active-window-changed"](screen, None)
    assert not bar.shown

    screen.active = game
    screen.handlers["active-window-changed"](screen, kiosk)
    assert bar.shown
    assert bar.origin == (976, 300), "on the small window's own title bar"
    assert bar.target == AppTarget(2, 4242, GAME)
    _deliver(bar, fake_gtk.idle)
    assert bar.audio == AudioState(True, 70)
    bar.shutdown()


def test_the_bar_closes_an_app_that_will_not_close(
    fake_gtk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = fake_gtk.ui
    game = _Win(2, 4242, "Frozen-bubble")
    screen = _Screen(_Win(1, 7000, "biokids-kiosk"), game)
    screen.active = game
    stopped: list[str] = []
    monkeypatch.setattr(ui, "stop_scope", lambda scope: stopped.append(scope) or True)
    bar = ui.AppBarUi(translator("tr"), screen, uid=UID)

    bar._on_close(None)
    assert game.calls == [("close", 1234)], "asked first"
    bar._tick()
    assert stopped == [], "and given time to close"
    bar.closer.pending.asked_at -= CLOSE_GRACE_S
    bar._tick()
    _deliver(bar, fake_gtk.idle)
    assert stopped == [GAME]
    bar.shutdown()


def test_home_sends_the_app_away_with_its_dialog_and_shows_the_launcher(
    fake_gtk: Any,
) -> None:
    ui = fake_gtk.ui
    kiosk, game = _Win(1, 7000, "biokids-kiosk"), _Win(2, 4242, "Frozen-bubble")
    question = _Win(3, 4242, "Frozen-bubble", kind="dialog")
    question.transient = game
    screen = _Screen(kiosk, game, question)
    screen.active = question
    bar = ui.AppBarUi(translator("tr"), screen, uid=UID)
    bar.refresh(raise_bar=False)

    bar._on_home(None)

    assert game.calls == ["minimize"]
    assert kiosk.calls == [("activate", 1234)]
    assert not bar.shown
    bar.shutdown()


def test_bring_back_shows_the_apps_own_window(fake_gtk: Any) -> None:
    ui = fake_gtk.ui
    game, site = _Win(2, 4242, "Frozen-bubble"), _Win(4, 6001, "Chromium")
    fake_gtk.repo.Wnck.Screen.get_default.return_value = _Screen(
        _Win(1, 7000, "biokids-kiosk"), game, site
    )

    assert ui.bring_back(GAME, uid=UID) == 0
    assert game.calls == [("activate_transient", 99)]
    assert site.calls == []
    assert ui.bring_back("biokids-app-tuxpaint-9.scope", uid=UID) == EXIT_NOT_FOUND


def test_without_gtk_the_bar_says_so_and_does_not_crash_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_gtk() -> Any:
        raise ImportError("No module named 'gi'")

    monkeypatch.setattr(appbar_main, "load_ui", no_gtk)
    assert appbar_main.main([]) == EXIT_UNAVAILABLE
    assert appbar_main.main(["--bring-back", "biokids-app-x-1"]) == EXIT_UNAVAILABLE


def test_only_an_app_can_be_brought_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_load() -> Any:
        raise AssertionError("GTK was loaded for a unit that is not an app")

    monkeypatch.setattr(appbar_main, "load_ui", must_not_load)
    assert appbar_main.main(["--bring-back", "biokids-shell.service"]) == EXIT_BAD_UNIT


def test_bring_back_is_handed_the_apps_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class FakeUi:
        @staticmethod
        def bring_back(scope: str, *, uid: int | None) -> int:
            seen.append(scope)
            return 0

    monkeypatch.setattr(appbar_main, "load_ui", lambda: FakeUi)
    assert appbar_main.main(["--bring-back", "biokids-app-frozen-bubble-3"]) == 0
    assert seen == [GAME]


# ---------------------------------------------------- the tile, tapped again


class _Running:
    """`systemd-run --scope` that has become the app."""

    returncode = None
    stderr = None

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0


class _Starts:
    def __init__(self) -> None:
        self.count = 0

    async def __call__(self, *_argv: Any, **_kwargs: Any) -> Any:
        self.count += 1
        return _Running()


GAME_TILE = LaunchRequest(
    slug="frozen-bubble", name="Balonlar", kind="app", exec_cmd="frozen-bubble --no-fullscreen"
)


@pytest.fixture
def starts(monkeypatch: pytest.MonkeyPatch) -> _Starts:
    started = _Starts()
    monkeypatch.setattr(shutil, "which", lambda name, **_kw: f"/usr/bin/{name}")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", started)
    monkeypatch.setattr(launcher_module, "SCOPE_REFUSAL_TIMEOUT_S", 0.01)
    return started


class _Answers:
    def __init__(
        self, launcher: AppLauncher, *, shown: int | None, active: bool = True
    ) -> None:
        self.shown = shown
        self.active = active
        self.brought: list[str] = []
        self.stopped: list[str] = []

        async def bring_back(unit: str) -> int | None:
            self.brought.append(unit)
            return self.shown

        async def unit_active(_unit: str) -> bool:
            return self.active

        async def stop_unit(unit: str) -> bool:
            self.stopped.append(unit)
            return True

        launcher.bring_back = bring_back  # type: ignore[method-assign]
        launcher.unit_active = unit_active  # type: ignore[method-assign]
        launcher.stop_unit = stop_unit  # type: ignore[method-assign]


async def test_a_second_tap_brings_the_open_app_back(config: Config, starts: _Starts) -> None:
    """🏠 sent the game to the background; its tile is the way back to it, and
    the child gets the game they left, not a second one from the start."""
    launcher = AppLauncher(config)
    first = await launcher.launch(GAME_TILE)
    answers = _Answers(launcher, shown=0)

    again = await launcher.launch(GAME_TILE)

    assert again.ok and again.unit == first.unit
    assert answers.brought == [first.unit]
    assert starts.count == 1, "a second copy was started"


async def test_an_app_still_opening_its_window_is_not_started_twice(
    config: Config, starts: _Starts
) -> None:
    """A slow app tapped again while it opens: a tile tapped eight times."""
    launcher = AppLauncher(config)
    await launcher.launch(GAME_TILE)
    _Answers(launcher, shown=EXIT_NOT_FOUND, active=True)

    assert (await launcher.launch(GAME_TILE)).ok
    assert starts.count == 1


async def test_an_app_that_was_closed_starts_again(config: Config, starts: _Starts) -> None:
    launcher = AppLauncher(config)
    first = await launcher.launch(GAME_TILE)
    _Answers(launcher, shown=EXIT_NOT_FOUND, active=False)

    again = await launcher.launch(GAME_TILE)

    assert again.ok and again.unit != first.unit
    assert starts.count == 2


async def test_a_scope_left_with_no_window_is_stopped_before_starting_again(
    config: Config, starts: _Starts
) -> None:
    launcher = AppLauncher(config)
    first = await launcher.launch(GAME_TILE)
    launcher.started["frozen-bubble"] -= launcher_module.STILL_OPENING_S + 1
    answers = _Answers(launcher, shown=EXIT_NOT_FOUND, active=True)

    again = await launcher.launch(GAME_TILE)

    assert answers.stopped == [first.unit]
    assert again.ok and starts.count == 2


async def test_without_the_helper_the_tile_starts_the_app_as_before(
    config: Config, starts: _Starts
) -> None:
    launcher = AppLauncher(config)
    await launcher.launch(GAME_TILE)
    _Answers(launcher, shown=EXIT_UNAVAILABLE)

    assert (await launcher.launch(GAME_TILE)).ok
    assert starts.count == 2


async def test_a_web_tile_opens_a_window_every_time(
    config: Config, starts: _Starts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every browser window is one Chromium, in whichever tile's scope started
    it: "this tile's window" could be another site."""
    monkeypatch.setattr(launcher_module, "prepare_browser_profile", lambda *_a: True)
    launcher = AppLauncher(config)
    site = LaunchRequest(slug="eba", name="EBA", kind="web", url="https://www.eba.gov.tr")
    await launcher.launch(site)
    answers = _Answers(launcher, shown=0)

    await launcher.launch(site)

    assert answers.brought == []
    assert starts.count == 2


async def test_the_helper_is_the_app_bar_run_once(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = AppLauncher(config)
    ran: list[list[str]] = []

    async def exit_code(argv: list[str], _within: float) -> int:
        ran.append(argv)
        return 0

    monkeypatch.setattr(launcher_module, "_exit_code", exit_code)
    assert await launcher.bring_back("biokids-app-frozen-bubble-3") == 0
    assert await launcher.unit_active("biokids-app-frozen-bubble-3") is True
    assert await launcher.stop_unit("biokids-app-frozen-bubble-3") is True

    assert ran[0] == [
        sys.executable,
        "-m",
        "biokids_shell.appbar",
        "--bring-back",
        "biokids-app-frozen-bubble-3",
    ]
    assert ran[1] == ["systemctl", "--user", "is-active", "--quiet", GAME]
    assert ran[2] == ["systemctl", "--user", "stop", GAME]

    ran.clear()
    assert await launcher.stop_unit("biokids-shell") is False
    assert ran == [], "the launcher's own unit was named to systemctl"


# ------------------------------------------- the apps open in the background

PAINT_TILE = LaunchRequest(slug="tuxpaint", name="Boya", kind="app", exec_cmd="tuxpaint")


async def test_the_launcher_lists_the_apps_still_open(
    config: Config, starts: _Starts, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-201: what the footer shows as "Açık" - each checked with systemd in one
    call, and an app that has closed forgotten."""
    monkeypatch.setattr(launcher_module, "prepare_browser_profile", lambda *_a: True)
    launcher = AppLauncher(config)
    await launcher.launch(GAME_TILE)
    await launcher.launch(PAINT_TILE)
    await launcher.launch(
        LaunchRequest(slug="eba", name="EBA", kind="web", url="https://www.eba.gov.tr")
    )
    asked: list[list[str]] = []

    async def stdout(argv: list[str], _within: float) -> str | None:
        asked.append(argv)
        return "active\ninactive\n"

    monkeypatch.setattr(launcher_module, "_stdout", stdout)

    assert await launcher.open_apps() == ["frozen-bubble"]
    assert asked == [
        [
            "systemctl",
            "--user",
            "is-active",
            "biokids-app-frozen-bubble-1.scope",
            "biokids-app-tuxpaint-2.scope",
        ]
    ], "web tiles open a window each time and are not listed"
    assert "tuxpaint" not in launcher.running


async def test_without_systemd_the_open_apps_are_what_was_started(
    config: Config, starts: _Starts, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = AppLauncher(config)
    await launcher.launch(GAME_TILE)

    async def nothing(_argv: list[str], _within: float) -> str | None:
        return None

    monkeypatch.setattr(launcher_module, "_stdout", nothing)
    assert await launcher.open_apps() == ["frozen-bubble"]
    assert await AppLauncher(config).open_apps() == [], "nothing started, nothing asked"


def test_systemctls_answer_is_read_one_line_per_scope() -> None:
    scopes = ["biokids-app-a-1.scope", "biokids-app-b-2.scope", "biokids-app-c-3.scope"]
    assert launcher_module.active_scopes(scopes, "active\ndeactivating\nfailed\n") == {
        "biokids-app-a-1.scope"
    }
    assert launcher_module.active_scopes(scopes, "active\n") is None, "does not line up"
