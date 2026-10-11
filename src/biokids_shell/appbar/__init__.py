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
* 🔉, a slider and 🔊 set the app's own volume - the app's sound streams, not
  the machine's, so Zio and Sio's warnings stay as loud as they were. The
  buttons step by ten percent; the slider follows a finger (D-201).
* The minutes left today and, in the whole bar, the clock, read from the
  launcher's own `/state` (D-201): orange in the last five minutes, red in the
  last one. Neither, and no room kept for them, while the launcher cannot say.
* ✕ asks the window to close, as the title bar's button did. If it is still
  there three seconds later - and has not answered with a question of its own,
  "save your drawing?" - or the child taps ✕ again, its scope is stopped:
  `systemctl --user stop` on the `biokids-app-*.scope` the launcher started it
  in, which is TERM, then KILL after the grace period a drawing needs to be
  saved. Only ever such a scope, in the child's own user manager.

A window being dragged takes the bar with it only once it stops: the bar is
hidden the moment the window starts to move and comes back on its title bar a
quarter of a second after it stops, and a window that was dragged out of
filling the screen fills it again (D-201).

The bar fits the window it sits on (D-205): over a window too narrow for all of
it, smaller buttons and the sound by its two buttons alone, and over a very
narrow one only home and close. And a new app window fills the screen even when
the app shrinks it back to the size it saved last time, as KDE's apps do, before
the bar is ever placed on it small.

This module is the part that can be tested anywhere: which scope a process is
in, which sound streams belong to an app, how a volume steps, when a close
becomes a stop, where a window's frame is and when a moving window has stopped,
how much of the bar fits over a window and when a new window is maximised.
The window itself is `ui.py`, in GTK 3 and libwnck 3, imported only when the
bar runs (`python3 -m biokids_shell.appbar`), so the test suite needs neither.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

from biokids_shell.kiosk import KIOSK_WINDOW_CLASS, launcher_port

if TYPE_CHECKING:
    from biokids_core.config import Config

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
in a 64-pixel bar (`dev/make_xfwm4_theme.py`). The rest is a transparent border,
which is still the button."""
TITLE_LINE_PX: Final = 2
"""The title bar's own line along its bottom edge (`dev/make_xfwm4_theme.py`).
The bar draws it too, so over a maximised app it sits flush in the title bar
instead of breaking the line with a pale box (D-201)."""
BAR_NAME: Final = "biokids-appbar"

HOME_GLYPH: Final = "🏠"
QUIETER_GLYPH: Final = "🔉"
LOUDER_GLYPH: Final = "🔊"
CLOSE_GLYPH: Final = "✕"
"""Text, not an emoji: white on the close button's red, as the title bar's was."""
TIME_GLYPH: Final = "⏳"

BAR_BACKGROUND: Final = "#FBF7F2"  # --bg, the title bar's own colour
BAR_LINE: Final = "#E7DED5"  # --line, the title bar's bottom line

# Each face sits where the title bar's close button sat: three pixels from the
# top, three above the line.
_FACE_TOP = (BUTTON_TARGET_PX - TITLE_LINE_PX - BUTTON_FACE_PX) // 2
_FACE_BOTTOM = BUTTON_TARGET_PX - BUTTON_FACE_PX - _FACE_TOP
_FACE_SIDE = (BUTTON_TARGET_PX - BUTTON_FACE_PX) // 2

SLIDER_PX: Final = 168
"""The volume slider's width, to the finger: its whole height is the bar's."""
SLIDER_PAD_PX: Final = 22
"""Room at each end of the track for the knob at 0 and at 100."""
TRACK_PX: Final = 14
KNOB_PX: Final = 40
"""The knob a child drags: as big as a fingertip, a white disc in a teal ring."""
KNOB_RING_PX: Final = 3
_KNOB_OUTER = KNOB_PX + 2 * KNOB_RING_PX
_KNOB_OVERHANG = (_KNOB_OUTER - TRACK_PX) // 2
INFO_PX: Final = 120
"""The minutes and the clock: a fixed width, wide enough for "⏳ 120 dk", so the
bar does not move as they change."""

COMPACT_TARGET_PX: Final = 48
"""A button of the compact bar across, to the finger (D-205); its height is still
the title bar's 64. More than the 44 x 44 UX.md section 2.4 asks for 8-10 and
never under 40: a child's finger target still, over a window with no room for 64."""
COMPACT_FACE_PX: Final = 44
"""A compact button's rounded square, to the eye: 44 pixels, where 56 were."""
COMPACT_INFO_PX: Final = 92
"""The minutes in the compact bar, in a smaller type and without the clock. Wide
enough for "⏳ 120 dk"; a wider text - English's "min" - widens it a little, and
the bar is placed by its new width."""

_COMPACT_FACE_TOP = (BUTTON_TARGET_PX - TITLE_LINE_PX - COMPACT_FACE_PX) // 2
_COMPACT_FACE_BOTTOM = BUTTON_TARGET_PX - COMPACT_FACE_PX - _COMPACT_FACE_TOP
_COMPACT_FACE_SIDE = (COMPACT_TARGET_PX - COMPACT_FACE_PX) // 2
_COMPACT_BORDER = " ".join(
    f"{px}px"
    for px in (_COMPACT_FACE_TOP, _COMPACT_FACE_SIDE, _COMPACT_FACE_BOTTOM, _COMPACT_FACE_SIDE)
)

BAR_CSS: Final = f"""
#{BAR_NAME} {{
  background-color: {BAR_BACKGROUND};
  background-image: linear-gradient(to top, {BAR_LINE} {TITLE_LINE_PX}px, \
{BAR_BACKGROUND} {TITLE_LINE_PX}px);
}}
#{BAR_NAME} button {{
  min-width: {BUTTON_FACE_PX}px;
  min-height: {BUTTON_FACE_PX}px;
  padding: 0;
  margin: 0;
  border-style: solid;
  border-color: transparent;
  border-width: {_FACE_TOP}px {_FACE_SIDE}px {_FACE_BOTTOM}px {_FACE_SIDE}px;
  border-image: none;
  border-radius: 18px;
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
#{BAR_NAME} scale {{
  padding: {_KNOB_OVERHANG}px {SLIDER_PAD_PX}px;
  margin: 0;
}}
#{BAR_NAME} scale trough {{
  min-height: {TRACK_PX}px;
  padding: 0;
  border-width: 0;
  border-image: none;
  border-radius: {TRACK_PX // 2}px;
  background-color: {BAR_LINE};
  background-image: none;
  box-shadow: none;
}}
#{BAR_NAME} scale highlight {{
  border-width: 0;
  border-image: none;
  border-radius: {TRACK_PX // 2}px;
  background-color: #1FB6A6;
  background-image: none;
  box-shadow: none;
}}
#{BAR_NAME} scale slider {{
  min-width: {KNOB_PX}px;
  min-height: {KNOB_PX}px;
  margin: -{_KNOB_OVERHANG}px;
  border-style: solid;
  border-width: {KNOB_RING_PX}px;
  border-color: #1FB6A6;
  border-image: none;
  border-radius: {_KNOB_OUTER // 2}px;
  background-color: #FFFFFF;
  background-image: none;
  box-shadow: none;
}}
#{BAR_NAME} scale:disabled {{ opacity: 0.35; }}
#{BAR_NAME} box.info {{
  margin: 7px 4px 9px 4px;
  padding: 0 8px;
  border-radius: 14px;
  background-color: {BAR_BACKGROUND};
}}
#{BAR_NAME} box.info label {{ color: #221E1C; }}
#{BAR_NAME} box.info label.time {{
  font-size: 19px;
  font-weight: bold;
}}
#{BAR_NAME} box.info label.clock {{
  font-size: 16px;
  color: #6B625C;
}}
#{BAR_NAME} box.info.warn {{ background-color: #F2A32B; }}
#{BAR_NAME} box.info.warn label {{ color: #3A2A00; }}
#{BAR_NAME} box.info.danger {{ background-color: #E04A3F; }}
#{BAR_NAME} box.info.danger label {{ color: #FFFFFF; }}
#{BAR_NAME}.compact button,
#{BAR_NAME}.minimal button {{
  min-width: {COMPACT_FACE_PX}px;
  min-height: {COMPACT_FACE_PX}px;
  border-width: {_COMPACT_BORDER};
  border-radius: 14px;
  font-size: 24px;
}}
#{BAR_NAME}.compact button.close,
#{BAR_NAME}.minimal button.close {{ font-size: 26px; }}
#{BAR_NAME}.compact box.info {{
  margin: 7px 2px 9px 2px;
  padding: 0 4px;
}}
#{BAR_NAME}.compact box.info label.time {{ font-size: 15px; }}
""".encode()
"""GTK 3 CSS. The launcher's colours (UX.md section 1): an indigo tint for home,
a teal tint for the sound, and for close the title bar's own red (`--danger`,
white on it 4:1), so the way out looks as it did. The minutes turn `--warn` with
dark text, then `--danger` with white, as the launcher's banners do. Every
property here is one GTK 3 knows: an unknown one fails the whole sheet.

The bar's tier (D-205) is a class on the bar itself, `compact` or `minimal`; the
rules for it come last, so they win over the full bar's of the same weight. A
compact face sits in the title bar as a full one does: as far from the top as
from the line."""


@dataclass(frozen=True, slots=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


def bar_origin(
    monitor: Rect,
    bar_width: int,
    window: Rect | None = None,
    bar_height: int = BUTTON_TARGET_PX,
) -> tuple[int, int]:
    """Where the bar goes: the top-right corner of the app's own title bar.

    For a maximised window that is the top-right corner of the monitor, where
    the title bar's close button was, so the way out does not move. Any other
    window - Frozen Bubble's fixed 640x480, a window the child dragged - gets
    the bar on its own title bar: at the monitor's corner it sat over the
    launcher's remaining-time ring, beside an app in the middle of the screen.
    Never off the monitor, not even partly.
    """
    right, top = monitor.x + monitor.width, monitor.y
    if window is not None:
        right = min(right, window.x + window.width)
        top = max(top, window.y)
    x = max(monitor.x, right - bar_width)
    return x, min(top, monitor.y + max(0, monitor.height - bar_height))


# --------------------------------------------------------- the bar's size


class BarTier(StrEnum):
    """How much of the bar is shown: what fits over the window in front (D-205).

    The owner opened Palapeli in a window a little wider than the bar, and the
    bar covered nearly all of its title bar; Frozen Bubble's fixed 640 x 480
    window lost most of its own. The bar now fits the window it sits on. The
    value is the CSS class the bar carries.
    """

    FULL = "full"
    """Home, the minutes and the clock, quieter, the slider, louder, close."""
    COMPACT = "compact"
    """Smaller buttons, the minutes without the clock, the sound by its two
    buttons alone: about half as wide."""
    MINIMAL = "minimal"
    """Home and close: the way back to the launcher and the way out."""


@dataclass(frozen=True, slots=True)
class TierLayout:
    """What a tier of the bar shows, in CSS pixels. Always as tall as the title bar."""

    button_px: int
    """Each button across, to the finger."""
    face_px: int
    """Each button's rounded square, to the eye."""
    info_px: int
    """The minutes; 0 where they are not shown."""
    clock: bool
    volume: bool
    """🔉 and 🔊."""
    slider: bool

    @property
    def width(self) -> int:
        """The bar's natural width: home and close, and what else this tier shows.
        With the box of the minutes; without it, `info_px` narrower, while there
        is nothing to put in it (`shows_info`)."""
        buttons = 4 if self.volume else 2
        slider = SLIDER_PX if self.slider else 0
        return buttons * self.button_px + self.info_px + slider

    def shows_info(self, *, minutes: bool, clock: bool) -> bool:
        """Whether the box of the minutes and the clock is shown: only with
        something in it.

        `minutes` is whether the launcher said how many are left, `clock` whether
        the clock is wanted, which only a tier with room for it shows. While the
        daemon restarts - an upgrade - the launcher cannot say the minutes, and
        the compact bar has no clock: an empty box there was a 92-pixel gap
        between 🏠 and 🔉, as it was in every bar before the launcher's first
        answer.
        """
        return self.info_px > 0 and (minutes or (clock and self.clock))


BAR_TIERS: Final[Mapping[BarTier, TierLayout]] = MappingProxyType(
    {
        BarTier.FULL: TierLayout(
            BUTTON_TARGET_PX, BUTTON_FACE_PX, INFO_PX, clock=True, volume=True, slider=True
        ),
        BarTier.COMPACT: TierLayout(
            COMPACT_TARGET_PX,
            COMPACT_FACE_PX,
            COMPACT_INFO_PX,
            clock=False,
            volume=True,
            slider=False,
        ),
        BarTier.MINIMAL: TierLayout(
            COMPACT_TARGET_PX, COMPACT_FACE_PX, 0, clock=False, volume=False, slider=False
        ),
    }
)
"""The whole bar, 544 pixels wide; the compact one, 284; home and close, 96."""

FULL_FROM_PX: Final = 1000
"""The narrowest window, in CSS pixels, with the whole bar over it. The bar's 544
pixels are 54% of that: about half, so a window that fills a screen 1024 pixels
wide keeps every control - and over a maximised app the bar covers only its
title, never the app. Narrower, and the whole bar would take more than about
half of the window's title bar."""
COMPACT_FROM_PX: Final = 520
"""The narrowest window with the compact bar over it: its 284 pixels are 55% of
that. Frozen Bubble's 648-pixel window gets it, at 44%. Under this, home and
close alone: 96 pixels, half of a window 192 wide, narrower than any app's."""


def bar_tier(width: int, scale: int = 1) -> BarTier:
    """The tier for a window `width` device pixels wide, at the screen's scale factor.

    Only the window's width decides, never what is in front of it, so a bar that
    changes its own size cannot change its tier back: nothing feeds back.
    """
    css_width = width // max(1, scale)
    if css_width >= FULL_FROM_PX:
        return BarTier.FULL
    if css_width >= COMPACT_FROM_PX:
        return BarTier.COMPACT
    return BarTier.MINIMAL


def visible_width(window: Rect, monitor: Rect) -> int:
    """How much of a window's width is on the monitor: what the bar must fit on.

    A game dragged half off the screen has half a title bar left to hold it.
    """
    left = max(window.x, monitor.x)
    right = min(window.x + window.width, monitor.x + monitor.width)
    return max(0, right - left)


# ------------------------------------------------------------ the frame

NET_FRAME_EXTENTS: Final = "_NET_FRAME_EXTENTS"
"""What the window manager draws around a window: its title bar and borders."""
GTK_FRAME_EXTENTS: Final = "_GTK_FRAME_EXTENTS"
"""The invisible shadow around a window that draws its own title bar."""
MAX_EXTENT_PX: Final = 512
"""Larger than any frame; a value past it is not one."""


@dataclass(frozen=True, slots=True)
class Extents:
    left: int
    right: int
    top: int
    bottom: int


NO_EXTENTS: Final = Extents(0, 0, 0, 0)


def decode_cardinals(data: bytes, count: int = 4) -> tuple[int, ...] | None:
    """A format-32 X property as GDK hands it over: `count` C longs, native order.

    `gdk_property_get` copies Xlib's buffer, in which each 32-bit item is a
    `long` - eight bytes on amd64. Four-byte items are read too.
    """
    if count <= 0 or not data or len(data) % count:
        return None
    width = len(data) // count
    if width not in (4, 8):
        return None
    return tuple(
        int.from_bytes(data[i : i + width], sys.byteorder) for i in range(0, len(data), width)
    )


def extents_from(values: Sequence[int] | None) -> Extents | None:
    """left, right, top, bottom, as `_NET_FRAME_EXTENTS` lists them; None for nonsense."""
    if values is None or len(values) != 4:
        return None
    if any(value < 0 or value > MAX_EXTENT_PX for value in values):
        return None
    left, right, top, bottom = (int(value) for value in values)
    return Extents(left, right, top, bottom)


def implied_extents(client: Rect, frame: Rect) -> Extents:
    """The frame libwnck assumed: its frame geometry against the client's."""
    return Extents(
        left=client.x - frame.x,
        right=(frame.x + frame.width) - (client.x + client.width),
        top=client.y - frame.y,
        bottom=(frame.y + frame.height) - (client.y + client.height),
    )


def window_extents(
    decorations: Extents | None, shadow: Extents | None, fallback: Extents
) -> Extents:
    """How far the visible window reaches beyond its contents.

    The window manager's own frame, read from the X server at the moment it is
    needed, when there is one. A window that draws its own title bar has none,
    and an invisible shadow inside its edges instead: that is taken off. When
    neither can be read, what libwnck last worked out.
    """
    if decorations is not None and decorations != NO_EXTENTS:
        return decorations
    if shadow is not None and shadow != NO_EXTENTS:
        return Extents(-shadow.left, -shadow.right, -shadow.top, -shadow.bottom)
    if decorations is not None:
        return decorations
    return fallback


def frame_rect(client: Rect, extents: Extents) -> Rect | None:
    """The window as the child sees it: its contents and the frame around them."""
    frame = Rect(
        client.x - extents.left,
        client.y - extents.top,
        client.width + extents.left + extents.right,
        client.height + extents.top + extents.bottom,
    )
    return frame if frame.width > 0 and frame.height > 0 else None


# --------------------------------------------------- following a window

SETTLE_S: Final = 0.25
"""How long a window must stay still before the bar comes back on it."""
RESTLESS_S: Final = 1.5
"""A window that keeps moving with no finger on it - an app resizing itself -
gets the bar back after this, following it, rather than never."""
HELD_AT_MOST_S: Final = 10.0
"""A drag longer than this gets the bar back before it ends: the way out is
never gone for long."""


class FollowStep(StrEnum):
    """What to do once a window may have stopped moving."""

    WAIT = "wait"
    """It moved a moment ago, or the finger is still on it."""
    MAXIMIZE = "maximize"
    """It was dragged out of filling the screen: it fills it again first."""
    SHOW = "show"
    """It has stopped: the bar goes back on its title bar."""


def lost_maximize(changed: int, new_state: int, maximized_bits: int) -> bool:
    """libwnck's `state-changed`: whether the window has just stopped being maximised."""
    return bool(changed & maximized_bits) and (new_state & maximized_bits) != maximized_bits


@dataclass
class Follower:
    """The bar over a window that is being moved or resized.

    The bar is a separate window: moving it after every step of a drag kept it
    a step behind, the close button trailing the window across the screen. So
    it is hidden the moment the window starts to move, and shown again on the
    window's title bar a quarter of a second after the last step. xfwm4 takes a
    maximised window out of maximisation when it is dragged; a child never
    needs a floating window, so one that was maximised before a drag with a
    finger is maximised again when the finger lets go. An app that changes its
    own size is not fought with: without a finger, nothing is maximised - except
    in a new window's first moments, which are the `Filler`'s (D-205).
    """

    xid: int | None = None
    since: float = 0.0
    last_change: float = 0.0
    was_maximized: bool = False
    pressed: bool = False
    remaximized: bool = False

    def changed(
        self,
        xid: int,
        now: float,
        *,
        maximized: bool,
        pressed: bool,
        unmaximized: bool = False,
    ) -> bool:
        """The window's geometry or state changed. True: keep the bar hidden.

        `maximized` is what libwnck says at that moment, which at the first
        step of a drag is still what the window was before it.
        """
        if self.xid != xid:
            self.xid, self.since = xid, now
            self.was_maximized, self.pressed, self.remaximized = maximized, False, False
        self.last_change = now
        self.was_maximized = self.was_maximized or unmaximized
        self.pressed = self.pressed or pressed
        return self.hiding(xid, now)

    def hiding(self, xid: int, now: float) -> bool:
        """Whether the bar stays off this window for now."""
        if self.xid is None or xid != self.xid:
            return False
        limit = HELD_AT_MOST_S if self.pressed else RESTLESS_S
        return now - self.since < limit

    def dragging(self, xid: int) -> bool:
        """Whether a finger is moving or resizing this window: it is the child's then."""
        return self.xid == xid and self.pressed

    def settle(
        self, now: float, *, maximized: bool, can_maximize: bool, pressed: bool
    ) -> tuple[FollowStep, float]:
        """The next step, and how long to wait before asking again when it is WAIT."""
        if self.xid is None:
            return FollowStep.SHOW, 0.0
        quiet = now - self.last_change
        if quiet < SETTLE_S:
            return FollowStep.WAIT, SETTLE_S - quiet
        if pressed and now - self.since < HELD_AT_MOST_S:
            self.pressed = True
            return FollowStep.WAIT, SETTLE_S
        if (
            self.was_maximized
            and self.pressed
            and not maximized
            and can_maximize
            and not self.remaximized
        ):
            self.remaximized = True
            self.last_change = now
            return FollowStep.MAXIMIZE, SETTLE_S
        self.forget()
        return FollowStep.SHOW, 0.0

    def forget(self) -> None:
        self.xid = None
        self.pressed = self.was_maximized = self.remaximized = False


FILL_AGAIN_S: Final = 1.5
"""From when a new window is looked at again. KDE's apps put back the size they
saved last time a moment after they appear: sometimes after a first look has
already found them filling the screen. An app seen doing it is looked at as soon
as the window is still (`Filler.shrunk`); this is for one that was not seen."""
FILL_UNTIL_S: Final = 6.0
"""Until when. A window that does not fill the screen this long after it first
came to the front is left as it is: its size is its app's, or the child's."""
FILL_MEMORY: Final = 512
"""Windows remembered at most. A closed one is forgotten at once."""


@dataclass
class _Sighting:
    first: float
    looked: bool = False
    done: bool = False
    shrunk: bool = False
    """Its app was seen taking it out of filling the screen: the second look is
    due now, not at `FILL_AGAIN_S`."""


class Filler:
    """A new app window fills the screen, even when its app shrinks it back (D-205).

    devilspie2 maximises every ordinary window as it appears (D-163), and some
    apps then undo it: KDE's restore the size and place they saved last time
    just after their window is shown. The owner's Palapeli opened in a window
    not much wider than the bar, with the launcher all around it.

    So the bar looks at each new window the first time it is in front: an
    ordinary window that could fill the screen and does not is maximised. Then
    it looks again, and maximises the window once more the first time it finds
    it not filling the screen: at once when the app is seen shrinking it
    (`shrunk`), so the window fills the screen again as soon as it is still and
    the bar is never placed on it small in between; and from `FILL_AGAIN_S` on
    whatever was seen, for an app that restores its size late - on a slow
    machine, later than a fixed second look would - or without the window
    manager saying so. At `FILL_UNTIL_S`, or after that second time, it leaves
    the window alone. At most twice, so an app that insists is never fought.
    Never a dialog, which stays the size of a question; never a window that
    cannot be maximised (Frozen Bubble's fixed size), which is only followed;
    never while a button is down or the window is being dragged, which is the
    child's doing. And never a window that was open before the bar started: one
    the bar finds when it is restarted is not new, and is as the child has been
    using it.
    """

    def __init__(self) -> None:
        self._seen: dict[int, _Sighting] = {}

    def known(self, xid: int) -> None:
        """A window that was there before the bar: left as it is."""
        self._remember(xid, _Sighting(0.0, looked=True, done=True))

    def forget(self, xid: int) -> None:
        """The window has closed."""
        self._seen.pop(xid, None)

    def due(self, xid: int, now: float) -> bool:
        """Whether there is anything to look at in this window now."""
        sighting = self._seen.get(xid)
        if sighting is None:
            return True
        if sighting.done:
            return False
        return not sighting.looked or sighting.shrunk or now - sighting.first >= FILL_AGAIN_S

    def shrunk(self, xid: int, now: float) -> bool:
        """The window's app took it out of filling the screen, no finger on it.

        devilspie2 maximised a KDE app's window and the app put back the size it
        saved: the window is looked at again at once, not at `FILL_AGAIN_S`. The
        `Follower` waits until it is still and the look maximises it before the
        bar is placed - where a look a second later showed the child the screen
        filled, a small window with the bar on it, and the screen filled again.
        True when that look is now due: a window already looked at, not yet
        left alone, younger than `FILL_UNTIL_S`. A window the bar has not looked
        at yet gets its first look anyway.
        """
        sighting = self._seen.get(xid)
        if sighting is None or not sighting.looked or sighting.done:
            return False
        if now - sighting.first >= FILL_UNTIL_S:
            return False
        sighting.shrunk = True
        return True

    def look(
        self,
        xid: int,
        now: float,
        *,
        normal: bool,
        can_maximize: bool,
        maximized: bool,
        busy: bool,
    ) -> bool:
        """The window is in front: True when it is to be maximised now.

        `normal` is libwnck's window type, `busy` a button down or the window
        under a finger (`Follower.dragging`).
        """
        if not self.due(xid, now):
            return False
        sighting = self._seen.get(xid) or self._remember(xid, _Sighting(now))
        if not normal:
            sighting.done = True
            return False
        fill = can_maximize and not maximized and not busy
        if not sighting.looked:
            sighting.looked = True
            return fill
        if now - sighting.first >= FILL_UNTIL_S:
            sighting.done = True
            return False
        sighting.done = fill
        return fill

    def _remember(self, xid: int, sighting: _Sighting) -> _Sighting:
        if xid not in self._seen and len(self._seen) >= FILL_MEMORY:
            # The oldest: long since looked at, and likely closed unseen.
            del self._seen[next(iter(self._seen))]
        self._seen[xid] = sighting
        return sighting


# ------------------------------------------------------------------- sound

VOLUME_STEP: Final = 10
VOLUME_MAX: Final = 100
"""Never louder than the app's own full volume: the machine's volume is the
launcher's slider, and an app pushed past 100% distorts."""
SEND_EVERY_MS: Final = 80
"""While a finger drags the slider, the sound server is told at most this often."""


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


def level_at(x: float, track_x: float, track_width: float, knob: float = 0.0) -> int:
    """The volume under a finger at `x` on the slider: 0 at the track's left end,
    100 at its right, whole percents in between, and never past either end.

    `knob` is how much of the track the knob's own length takes: its centre
    travels from half a knob in at one end to half a knob in at the other.
    """
    span = track_width - knob
    if span <= 0:
        return 0
    fraction = (x - track_x - knob / 2) / span
    return max(0, min(VOLUME_MAX, round(fraction * VOLUME_MAX)))


class LevelSender:
    """The level a finger is dragging the slider to, sent one at a time.

    A drag is dozens of positions a second and each one a `pactl` run. Only the
    latest waits: while one is being set, the finger's newest place replaces
    whatever was waiting, so the sound follows the finger without a queue
    building up behind it.
    """

    def __init__(self) -> None:
        self.wanted: int | None = None
        self.sent: int | None = None
        self.busy = False

    def want(self, level: int) -> None:
        self.wanted = level

    def next(self) -> int | None:
        """The level to send now, or None: nothing new, or one is on its way."""
        if self.busy or self.wanted is None or self.wanted == self.sent:
            return None
        self.busy = True
        return self.wanted

    def done(self, level: int) -> None:
        self.busy = False
        self.sent = level

    def waiting(self) -> bool:
        """A newer level than the one just sent is waiting to go."""
        return not self.busy and self.wanted is not None and self.wanted != self.sent

    def settling(self) -> bool:
        """The slider shows the child's level, not yet the sound server's."""
        return self.busy or self.waiting()


def read_audio(
    target: AppTarget, scope_of: Callable[[int], str | None], run: Runner = run_command
) -> AudioState:
    mine = app_inputs(list_sink_inputs(run) or [], target, scope_of)
    return AudioState(has_sound=True, level=audio_level(mine)) if mine else NO_SOUND


def _apply_level(streams: Iterable[SinkInput], level: int, run: Runner) -> AudioState:
    """Every stream of the app at `level`; a muted one is unmuted to be heard."""
    for stream in streams:
        run(["pactl", "set-sink-input-volume", str(stream.index), f"{level}%"])
        if stream.muted and level > 0:
            run(["pactl", "set-sink-input-mute", str(stream.index), "0"])
    return AudioState(has_sound=True, level=level)


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
    return _apply_level(mine, stepped(audio_level(mine), louder=louder), run)


def set_volume(
    target: AppTarget,
    level: int,
    *,
    scope_of: Callable[[int], str | None],
    run: Runner = run_command,
) -> AudioState:
    """Set the app's volume to where the slider was dragged, 0 to 100."""
    mine = app_inputs(list_sink_inputs(run) or [], target, scope_of)
    if not mine:
        return NO_SOUND
    return _apply_level(mine, max(0, min(VOLUME_MAX, int(level))), run)


# -------------------------------------------------------- time and clock

TIME_POLL_S: Final = 15
"""How often the bar asks the launcher for the minutes left."""
CLOCK_TICK_S: Final = 5
WARN_BELOW_S: Final = 5 * 60
"""Orange, as the launcher's ring and banners are, in the last five minutes."""
DANGER_BELOW_S: Final = 60
"""Red in the last minute."""
STATE_TIMEOUT_S: Final = 3.0
MAX_STATE_BYTES: Final = 256 * 1024
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})


class TimeTone(StrEnum):
    OK = "ok"
    WARN = "warn"
    DANGER = "danger"


@dataclass(frozen=True, slots=True)
class TimeLeft:
    minutes: int
    """Rounded up, as the launcher's ring shows them."""
    tone: TimeTone


def time_tone(seconds: int) -> TimeTone:
    if seconds < DANGER_BELOW_S:
        return TimeTone.DANGER
    if seconds < WARN_BELOW_S:
        return TimeTone.WARN
    return TimeTone.OK


def time_left(state: Mapping[str, Any] | None) -> TimeLeft | None:
    """The minutes left today, from the launcher's `/state`; None when it cannot say.

    Never worked out here: the daemon is the only thing that knows how much
    time a child has, and the launcher only passes its answer on.
    """
    if not state or state.get("offline") or state.get("managed") is False:
        return None
    seconds = state.get("remaining_seconds")
    if isinstance(seconds, bool) or not isinstance(seconds, int | float):
        return None
    seconds = max(0, int(seconds))
    return TimeLeft(minutes=math.ceil(seconds / 60), tone=time_tone(seconds))


def shows_clock(state: Mapping[str, Any] | None) -> bool:
    """Every child gets the clock in the app bar: the owner asked for it there
    (D-201), next to the minutes left. The launcher still shows it only to 8-10
    (UX.md section 2.2), where the ring is the main thing to read."""
    return bool(state)


def clock_text(moment: time.struct_time) -> str:
    return time.strftime("%H:%M", moment)


def state_url(config: Config) -> str:
    """The launcher's own `/state`, on this child's own launcher port (D-188)."""
    return f"http://{config.shell.host}:{launcher_port(config)}/state"


def fetch_state(
    url: str,
    timeout: float = STATE_TIMEOUT_S,
    open_url: Callable[..., Any] | None = None,
) -> dict[str, Any] | None:
    """The launcher's `/state`, or None. Only ever on this machine's loopback."""
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK_HOSTS:
        log.warning("not asking %s for the time: not the launcher on this machine", url)
        return None
    if open_url is None:
        # No proxy, whatever the session's environment says: the launcher is here.
        open_url = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    try:
        with open_url(url, timeout=timeout) as response:
            data = json.loads(response.read(MAX_STATE_BYTES))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.debug("the launcher's state could not be read: %s", exc)
        return None
    return data if isinstance(data, dict) else None


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
