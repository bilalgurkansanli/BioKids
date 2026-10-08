"""The BioKids app bar: home, the app's own volume, and a close that works (D-197).

The owner asked for it in two sentences. "Every app has a close button at the
top right; put a 'send to background' button and the app's own volume control
there too." And: "I opened Frozen Bubble 2 and it does not close."

The second one is what D-163 could not do. Its close button lives in the
window manager's title bar and devilspie2 maximises every window, so the button
is at the screen's top right - for a window that can be maximised. Frozen
Bubble's window has a fixed size: it floats in the middle of the screen with its
close button at its own corner, the launcher visible and clickable all around
it. And a title bar's close button only *asks*: an app that ignores the request
cannot be closed by a child at all. A title bar cannot hold a volume control
either.

So a small bar of BioKids' own sits at the screen's top-right corner, over any
app window the child is in, and never over the launcher:

* 🏠 minimises the app and shows the launcher. The app keeps running, and its
  tile brings the same window back (`AppLauncher.launch`).
* 🔉 and 🔊 step the app's own volume by ten percent - the app's sound streams,
  not the machine's, so Zio and Sio's warnings stay as loud as they were.
* ✕ asks the window to close, as the title bar's button did. If it is still
  there three seconds later - and has not answered with a question of its own,
  "save your drawing?" - or the child taps ✕ again, its scope is stopped:
  `systemctl --user stop` on the `biokids-app-*.scope` the launcher started it
  in, which is TERM, then KILL after the grace period a drawing needs to be
  saved. Only ever such a scope, in the child's own user manager.

This module is the part that can be tested anywhere: which scope a process is
in, which sound streams belong to an app, how a volume steps, and when a close
becomes a stop. The window itself is `ui.py`, in GTK 3 and libwnck 3, imported
only when the bar runs (`python3 -m biokids_shell.appbar`), so the test suite
needs neither.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from biokids_shell.kiosk import KIOSK_WINDOW_CLASS

log = logging.getLogger(__name__)

MODULE: Final = "biokids_shell.appbar"
"""What the launcher runs with `python3 -m`."""

READY: Final = "ready"
"""The one line the bar prints, once its window exists. Until the launcher reads
it, every app window keeps the title bar's close button (`windows.AppBar`)."""

EXIT_NOT_FOUND: Final = 1
"""`--bring-back`: the app has no window to show."""
EXIT_BAD_UNIT: Final = 2
EXIT_UNAVAILABLE: Final = 3
"""GTK 3 or libwnck 3 is missing. Not worth restarting for."""

# ------------------------------------------------------------------ scopes

APP_SCOPE: Final = re.compile(r"biokids-app-[A-Za-z0-9_-]+\.scope")
"""The scopes `AppLauncher` starts apps in, and the only units the bar stops."""


def is_app_scope(name: str) -> bool:
    return APP_SCOPE.fullmatch(name) is not None


def scope_unit(unit: str) -> str:
    """`biokids-app-tuxpaint-3`, the name systemd-run was given, as its scope."""
    return unit if unit.endswith(".scope") else f"{unit}.scope"


def scope_from_cgroup(text: str, uid: int | None = None) -> str | None:
    """The app scope a process is in, from its `/proc/<pid>/cgroup`.

    `0::/user.slice/user-1001.slice/user@1001.service/app.slice/biokids-app-frozen-bubble-3.scope`
    is Frozen Bubble; the kiosk is in `.../biokids-shell.service` and has none.
    With `uid`, the scope must also sit under that user's own manager,
    `user@<uid>.service`: the bar only ever acts for the child it runs as.
    """
    for line in text.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        path = [part for part in fields[2].split("/") if part]
        found = [index for index, part in enumerate(path) if is_app_scope(part)]
        if not found:
            continue
        index = found[-1]
        if uid is not None and f"user@{uid}.service" not in path[:index]:
            continue
        return path[index]
    return None


def read_cgroup(pid: int, proc: Path = Path("/proc")) -> str | None:
    if pid <= 0:
        return None
    try:
        return (proc / str(pid) / "cgroup").read_text(encoding="utf-8")
    except OSError:
        return None


def scope_of_pid(pid: int, uid: int | None = None, proc: Path = Path("/proc")) -> str | None:
    """The app scope process `pid` is in, or None (the kiosk, a gone process)."""
    text = read_cgroup(pid, proc)
    return scope_from_cgroup(text, uid) if text else None


def own_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def stop_command(scope: str) -> list[str]:
    """`systemctl --user stop` for an app scope, and for nothing else.

    The bar runs as the child, so `--user` can only reach the child's own
    manager; this keeps it from the units in there that are not apps - the
    launcher itself and Zio and Sio's voice.
    """
    if not is_app_scope(scope):
        raise ValueError(f"refusing to stop {scope!r}: not a BioKids app scope")
    return ["systemctl", "--user", "stop", scope]


# ----------------------------------------------------------------- windows

APP_WINDOW_KINDS: Final = frozenset({"normal", "dialog", "utility"})
"""What the bar is shown for. Not the desktop, menus, tooltips or splashes."""


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """What the bar needs to know about a window, read from libwnck."""

    xid: int
    pid: int
    res_class: str
    res_name: str
    kind: str
    """libwnck's window type nick: `normal`, `dialog`, `desktop`, ..."""


@dataclass(frozen=True, slots=True)
class AppTarget:
    """The app window the bar is acting on, and the scope it runs in."""

    xid: int
    pid: int
    scope: str | None


def is_kiosk(window: WindowInfo) -> bool:
    """The launcher, found by the WM_CLASS the kiosk sets with `--class`."""
    wanted = KIOSK_WINDOW_CLASS.lower()
    return wanted in {window.res_class.lower(), window.res_name.lower()}


def is_app_window(window: WindowInfo | None) -> bool:
    """Whether the bar belongs on screen while this window is the active one.

    Never for the launcher: it raises itself for the time warnings and the
    screens that end the day, and nothing may sit on top of those.
    """
    return window is not None and not is_kiosk(window) and window.kind in APP_WINDOW_KINDS


def windows_of(target: AppTarget, open_windows: Mapping[int, str | None]) -> frozenset[int]:
    """The open windows that belong to the target's app: same scope, or itself."""
    return frozenset(
        xid
        for xid, scope in open_windows.items()
        if xid == target.xid or (target.scope is not None and scope == target.scope)
    )


# --------------------------------------------------------------- the look

BUTTON_TARGET_PX: Final = 64
"""Each button, to the finger: UX.md section 2.4's smallest target for 4-7, and
the height of the BioKids title bar, so over a maximised app the bar sits in the
title bar's strip and covers nothing of the app itself."""
BUTTON_FACE_PX: Final = 56
"""Each button, to the eye: the title bar's close button was a 56-pixel square
in a 64-pixel bar (`dev/make_xfwm4_theme.py`). The rest is a border in the bar's
own colour, which is still the button."""
BAR_NAME: Final = "biokids-appbar"

HOME_GLYPH: Final = "🏠"
QUIETER_GLYPH: Final = "🔉"
LOUDER_GLYPH: Final = "🔊"
CLOSE_GLYPH: Final = "✕"
"""Text, not an emoji: white on the close button's red, as the title bar's was."""

_EDGE = (BUTTON_TARGET_PX - BUTTON_FACE_PX) // 2

BAR_CSS: Final = f"""
#{BAR_NAME} {{
  background-color: #FBF7F2;
}}
#{BAR_NAME} button {{
  min-width: {BUTTON_FACE_PX}px;
  min-height: {BUTTON_FACE_PX}px;
  padding: 0;
  margin: 0;
  border: {_EDGE}px solid #FBF7F2;
  border-radius: {_EDGE + 15}px;
  background-clip: padding-box;
  background-image: none;
  box-shadow: none;
  text-shadow: none;
  font-size: 30px;
  color: #221E1C;
}}
#{BAR_NAME} button.home {{ background-color: #E4E6FA; }}
#{BAR_NAME} button.home:hover {{ background-color: #D5D8F6; }}
#{BAR_NAME} button.home:active {{ background-color: #C3C7F1; }}
#{BAR_NAME} button.volume {{ background-color: #D2F2EE; }}
#{BAR_NAME} button.volume:hover {{ background-color: #BCEBE5; }}
#{BAR_NAME} button.volume:active {{ background-color: #A5E3DB; }}
#{BAR_NAME} button.close {{
  background-color: #E04A3F;
  color: #FFFFFF;
  font-weight: bold;
  font-size: 32px;
}}
#{BAR_NAME} button.close:hover {{ background-color: #C93A30; }}
#{BAR_NAME} button.close:active {{ background-color: #B03028; }}
#{BAR_NAME} button:disabled {{ opacity: 0.35; }}
#{BAR_NAME} label.level {{
  font-size: 15px;
  min-width: 44px;
  margin: 0 2px;
}}
""".encode()
"""GTK 3 CSS. The launcher's colours (UX.md section 1): an indigo tint for home,
a teal tint for the sound, and for close the title bar's own red (`--danger`,
white on it 4:1), so the way out looks as it did. Every property here is one GTK
3 knows: an unknown one fails the whole sheet."""


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


def bar_origin(monitor: Rect, bar_width: int) -> tuple[int, int]:
    """The top-right corner of the monitor the app is on: where the title bar's
    close button of a maximised window is, so the way out does not move."""
    return monitor.x + max(0, monitor.width - bar_width), monitor.y


# ------------------------------------------------------------------- sound

VOLUME_STEP: Final = 10
VOLUME_MAX: Final = 100
"""Never louder than the app's own full volume: the machine's volume is the
launcher's slider, and an app pushed past 100% distorts."""
LEVEL_MARKS: Final = 5
LEVEL_GLYPHS: Final = "▁▂▄▆█"
LEVEL_ON: Final = "#1FB6A6"  # --bio-teal
LEVEL_OFF: Final = "#E7DED5"  # --line


@dataclass(frozen=True, slots=True)
class SinkInput:
    """One sound stream, as `pactl` lists it."""

    index: int
    pid: int | None
    """`application.process.id`: the process that plays it."""
    level: int
    """Percent, the loudest channel."""
    muted: bool


@dataclass(frozen=True, slots=True)
class AudioState:
    has_sound: bool
    """Whether the app has a sound stream at all: the buttons dim when not."""
    level: int


NO_SOUND: Final = AudioState(has_sound=False, level=0)

Runner = Callable[[Sequence[str]], str | None]
"""Run a command; its stdout, or None when it failed."""

COMMAND_TIMEOUT_S: Final = 5.0
STOP_TIMEOUT_S: Final = 30.0
"""`systemctl --user stop` waits for the scope: TERM, the grace period, KILL."""


def run_command(argv: Sequence[str], timeout: float = COMMAND_TIMEOUT_S) -> str | None:
    """stdout of a command that succeeded, in the C locale, or None."""
    try:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s could not be run: %s", argv[0], exc)
        return None
    if done.returncode != 0:
        log.debug("%s exited %s: %s", " ".join(argv[:4]), done.returncode, done.stderr[:200])
        return None
    return done.stdout


def _percent(text: str) -> int | None:
    match = re.search(r"(\d+)\s*%", text)
    return int(match.group(1)) if match else None


def _pid(value: object) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if text.isdigit() and int(text) > 0 else None


def parse_sink_inputs_json(text: str) -> list[SinkInput] | None:
    """`pactl -f json list sink-inputs`; None when it is not the JSON expected."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    found: list[SinkInput] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        levels: list[int] = []
        volume = item.get("volume")
        if isinstance(volume, dict):
            for channel in volume.values():
                if isinstance(channel, dict):
                    level = _percent(str(channel.get("value_percent", "")))
                    if level is not None:
                        levels.append(level)
        properties = item.get("properties")
        pid = (
            _pid(properties.get("application.process.id"))
            if isinstance(properties, dict)
            else None
        )
        found.append(
            SinkInput(
                index=index,
                pid=pid,
                level=max(levels, default=0),
                muted=item.get("mute") is True,
            )
        )
    return found


_SINK_INPUT_HEADER = re.compile(r"Sink Input #(\d+)")
_PROCESS_ID = re.compile(r'application\.process\.id\s*=\s*"(\d+)"')


def parse_sink_inputs_text(text: str) -> list[SinkInput]:
    """`LC_ALL=C pactl list sink-inputs`, for a pactl whose JSON cannot be read."""
    found: list[SinkInput] = []
    index: int | None = None
    pid: int | None = None
    levels: list[int] = []
    muted = False

    def finish() -> None:
        if index is not None:
            found.append(
                SinkInput(index=index, pid=pid, level=max(levels, default=0), muted=muted)
            )

    for raw in text.splitlines():
        line = raw.strip()
        header = _SINK_INPUT_HEADER.fullmatch(line)
        if header is not None:
            finish()
            index, pid, levels, muted = int(header.group(1)), None, [], False
        elif index is None:
            continue
        elif line.startswith("Mute:"):
            muted = line.split(":", 1)[1].strip().lower() == "yes"
        elif line.startswith("Volume:"):
            levels = [int(value) for value in re.findall(r"(\d+)%", line)]
        elif (process := _PROCESS_ID.fullmatch(line)) is not None:
            pid = _pid(process.group(1))
    finish()
    return found


def list_sink_inputs(run: Runner = run_command) -> list[SinkInput] | None:
    """Every sound stream on the child's sound server; None when there is no answer.

    `pactl` speaks to PipeWire's pulse server, which is what Debian 13 runs, and
    to PulseAudio alike. JSON first; the text form where the JSON is broken.
    """
    out = run(["pactl", "-f", "json", "list", "sink-inputs"])
    if out is not None:
        parsed = parse_sink_inputs_json(out)
        if parsed is not None:
            return parsed
    out = run(["pactl", "list", "sink-inputs"])
    return None if out is None else parse_sink_inputs_text(out)


def app_inputs(
    inputs: Iterable[SinkInput],
    target: AppTarget,
    scope_of: Callable[[int], str | None],
) -> list[SinkInput]:
    """The streams the target app plays.

    By scope: the sound often comes from another process than the window's -
    Chromium plays from a helper process - but always from the same scope. A
    window with no scope (started without systemd) is matched by its own pid.
    The voice service's stream is in its own unit and is never one of them.
    """
    if target.scope is not None:
        return [i for i in inputs if i.pid is not None and scope_of(i.pid) == target.scope]
    return [i for i in inputs if target.pid > 0 and i.pid == target.pid]


def audio_level(inputs: Iterable[SinkInput]) -> int:
    """What the app sounds like: its loudest stream, a muted one as silence."""
    return max((0 if i.muted else i.level for i in inputs), default=0)


def stepped(level: int, *, louder: bool) -> int:
    """One step of ten, onto the tens: 65 goes to 70 or 60, 60 to 70 or 50."""
    if louder:
        if level >= VOLUME_MAX:
            return level
        return min(VOLUME_MAX, (level // VOLUME_STEP + 1) * VOLUME_STEP)
    return max(0, (math.ceil(level / VOLUME_STEP) - 1) * VOLUME_STEP)


def level_marks(level: int) -> int:
    """How many of the five bars to fill. Any sound at all fills one."""
    if level <= 0:
        return 0
    return max(1, min(LEVEL_MARKS, round(level * LEVEL_MARKS / VOLUME_MAX)))


def level_markup(state: AudioState) -> str:
    """The indicator between the two volume buttons: five rising bars."""
    marks = level_marks(state.level) if state.has_sound else 0
    return (
        f'<span foreground="{LEVEL_ON}">{LEVEL_GLYPHS[:marks]}</span>'
        f'<span foreground="{LEVEL_OFF}">{LEVEL_GLYPHS[marks:]}</span>'
    )


def read_audio(
    target: AppTarget, scope_of: Callable[[int], str | None], run: Runner = run_command
) -> AudioState:
    mine = app_inputs(list_sink_inputs(run) or [], target, scope_of)
    return AudioState(has_sound=True, level=audio_level(mine)) if mine else NO_SOUND


def change_volume(
    target: AppTarget,
    *,
    louder: bool,
    scope_of: Callable[[int], str | None],
    run: Runner = run_command,
) -> AudioState:
    """Step the app's volume once. Every stream of the app ends at the same level."""
    mine = app_inputs(list_sink_inputs(run) or [], target, scope_of)
    if not mine:
        return NO_SOUND
    level = stepped(audio_level(mine), louder=louder)
    for stream in mine:
        run(["pactl", "set-sink-input-volume", str(stream.index), f"{level}%"])
        if stream.muted and louder and level > 0:
            run(["pactl", "set-sink-input-mute", str(stream.index), "0"])
    return AudioState(has_sound=True, level=level)


# ------------------------------------------------------------------- close

CLOSE_GRACE_S: Final = 3.0
"""How long an app has to close itself before its scope is stopped."""
QUESTION_WAIT_S: Final = 60.0
"""An app that answered the close with a window of its own - "save your
drawing?" - is waiting for the child, not stuck. It is left to them this long."""
STOP_GIVE_UP_S: Final = 20.0
"""After a stop: TERM, the ten-second grace period, KILL, and some slack."""
DOUBLE_TAP_S: Final = 1.0
"""Two taps closer together than this are one tap that bounced."""


class CloseAction(StrEnum):
    """What a tap on the close button does."""

    ASK = "ask"
    """Ask the window to close (WM_DELETE_WINDOW), as the title bar's button did."""
    STOP = "stop"
    """Stop the app's scope now: a second tap on an app that is still there."""
    NOTHING = "nothing"


class CloseStep(StrEnum):
    """What to do about a close that was asked for, a moment later."""

    WAIT = "wait"
    STOP = "stop"
    DONE = "done"
    GIVE_UP = "give_up"


@dataclass
class PendingClose:
    target: AppTarget
    asked_at: float
    windows_before: frozenset[int]
    """The app's windows when it was asked; a new one is the app asking back."""
    stopped_at: float | None = None

    def same_app(self, target: AppTarget) -> bool:
        if self.target.scope is not None:
            return target.scope == self.target.scope
        return target.xid == self.target.xid

    def step(self, now: float, app_windows: frozenset[int]) -> CloseStep:
        if self.target.xid not in app_windows:
            return CloseStep.DONE
        if self.stopped_at is not None:
            return (
                CloseStep.GIVE_UP if now - self.stopped_at >= STOP_GIVE_UP_S else CloseStep.WAIT
            )
        if now - self.asked_at < CLOSE_GRACE_S:
            return CloseStep.WAIT
        if app_windows - self.windows_before:
            return (
                CloseStep.WAIT if now - self.asked_at < QUESTION_WAIT_S else CloseStep.GIVE_UP
            )
        return CloseStep.STOP if self.target.scope is not None else CloseStep.GIVE_UP


class Closer:
    """The close button: polite first, then firm.

    Polite, because an app may want to ask "save your drawing?" and must be
    able to. Firm, because an app that does not close itself within three
    seconds - or that a child taps ✖ on a second time - is stopped, and a child
    is never left in front of an app they cannot leave.
    """

    def __init__(self) -> None:
        self.pending: PendingClose | None = None

    def tap(
        self, target: AppTarget, now: float, open_windows: Mapping[int, str | None]
    ) -> CloseAction:
        pending = self.pending
        if pending is not None and pending.same_app(target) and pending.stopped_at is None:
            if now - pending.asked_at < DOUBLE_TAP_S:
                return CloseAction.NOTHING
            if target.scope is not None:
                pending.stopped_at = now
                return CloseAction.STOP
        self.pending = PendingClose(target, now, windows_of(target, open_windows))
        return CloseAction.ASK

    def tick(
        self, now: float, open_windows: Mapping[int, str | None]
    ) -> tuple[CloseStep, str | None]:
        """The next step, and the scope to stop when it is STOP."""
        pending = self.pending
        if pending is None:
            return CloseStep.DONE, None
        step = pending.step(now, windows_of(pending.target, open_windows))
        if step is CloseStep.STOP:
            pending.stopped_at = now
        elif step in (CloseStep.DONE, CloseStep.GIVE_UP):
            self.pending = None
        return step, pending.target.scope


def stop_scope(scope: str, run: Runner | None = None) -> bool:
    """Stop an app's scope; True when systemd says it is done."""
    argv = stop_command(scope)
    if run is None:
        return run_command(argv, timeout=STOP_TIMEOUT_S) is not None
    return run(argv) is not None
