"""D-197: the app bar - home, the app's own volume, and a close that works.

The owner opened Frozen Bubble and could not close it: its window cannot be
maximised, so the title bar's close button was not where a child looks, and a
title bar's close button only asks. These pin what can be pinned without an X
server: which scope a window's process is in, which sound streams are the app's
and how they step, when a close becomes a stop and what may be stopped, how the
launcher's tile brings a sent-away app back, and that none of it needs GTK.
The window itself (`biokids_shell/appbar/ui.py`) is checked on the VM
(`docs/VM-RUNBOOK.md` §8.9).
"""

from __future__ import annotations

import asyncio
import re
import shutil
import struct
import subprocess
import sys
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
    NO_SOUND,
    QUESTION_WAIT_S,
    STOP_GIVE_UP_S,
    AppTarget,
    AudioState,
    CloseAction,
    Closer,
    CloseStep,
    Rect,
    SinkInput,
    WindowInfo,
    app_inputs,
    bar_origin,
    change_volume,
    is_app_scope,
    is_app_window,
    level_marks,
    level_markup,
    list_sink_inputs,
    parse_sink_inputs_json,
    parse_sink_inputs_text,
    read_audio,
    scope_from_cgroup,
    scope_of_pid,
    scope_unit,
    stepped,
    stop_command,
    stop_scope,
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


def test_every_button_is_big_enough_for_a_four_year_old() -> None:
    """UX.md section 2.4: 64 x 64. The border is the bar's colour, still the button."""
    border = re.search(r"border: (\d+)px solid", BAR_CSS.decode())
    assert border is not None
    assert BUTTON_FACE_PX + 2 * int(border.group(1)) == BUTTON_TARGET_PX >= 64
    assert f"min-width: {BUTTON_FACE_PX}px" in BAR_CSS.decode()
    assert f"min-height: {BUTTON_FACE_PX}px" in BAR_CSS.decode()


def test_the_bar_is_as_tall_as_the_title_bar_it_sits_in() -> None:
    """Over a maximised app the bar covers the title bar's strip, not the app."""
    data = (THEME_DIR / "close-active.png").read_bytes()
    _width, height = struct.unpack(">II", data[16:24])
    assert height == BUTTON_TARGET_PX


GTK3_PROPERTIES = {
    "background-color",
    "background-clip",
    "background-image",
    "border",
    "border-radius",
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


def test_the_level_shows_as_five_rising_bars() -> None:
    levels = (0, 1, 10, 20, 50, 70, 100, 150)
    assert [level_marks(v) for v in levels] == [0, 1, 1, 1, 2, 4, 5, 5]

    def spans(state: AudioState) -> list[str]:
        return re.findall(r'">([^<]*)</span>', level_markup(state))

    assert [len(text) for text in spans(AudioState(True, 60))] == [3, 2]
    assert [len(text) for text in spans(NO_SOUND)] == [0, 5]
    assert "".join(spans(AudioState(True, 60))) == appbar.LEVEL_GLYPHS


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
        return (640, 300, 640, 480)

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
    yield types.SimpleNamespace(ui=ui, repo=repository, idle=idle)
    sys.modules.pop("biokids_shell.appbar.ui", None)
    if hasattr(appbar, "ui"):
        delattr(appbar, "ui")


def _deliver(bar: Any, idle: list[tuple[Any, tuple[Any, ...]]]) -> None:
    """Let the workers finish, then run what they handed to the main loop."""
    bar._sound.submit(lambda: None).result()
    bar._stops.submit(lambda: None).result()
    while idle:
        handler, args = idle.pop(0)
        handler(*args)


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
