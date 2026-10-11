"""The app bar's window, in GTK 3 with libwnck 3 (D-197, D-201).

Imported only by `python3 -m biokids_shell.appbar`, never by the launcher or the
tests: GTK is a desktop library and the launcher is a web server. Both are LGPL
and loaded as libraries through PyGObject, which an MIT program may do.

**Not a window the window manager knows.** The bar is a popup (override-
redirect), like a menu. A window the manager knows can be clicked into focus,
and then the "active app" the bar acts on would be the bar itself; it would be
maximised by devilspie2's rule; and an app gone full screen - a video in the
child's browser - would cover it. A popup never takes the focus, nothing
resizes it, and it is raised again whenever the stacking changes. It is hidden
whenever the launcher is the active window, because the launcher raises itself
for the time warnings and the screens that end the day.

**Off a window while it moves** (D-201). Moved after every step of a drag, the
bar trailed the window across the screen. It is hidden at the window's first
`geometry-changed` and comes back on its title bar once the window has been
still for a quarter of a second (`Follower`); a maximised window dragged out of
maximisation is maximised again when the finger lets go.

**Nothing slow in the window's thread.** `pactl`, `systemctl` and the launcher's
`/state` are asked on worker threads, and only plain values cross back;
libwnck and GTK are touched from the main loop alone.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import signal
import time
from collections.abc import Callable
from typing import Any, TypeVar

import gi

from biokids_core.i18n import Translator
from biokids_shell.appbar import (
    BAR_CSS,
    BAR_NAME,
    BUTTON_TARGET_PX,
    CLOCK_TICK_S,
    CLOSE_GLYPH,
    EXIT_NOT_FOUND,
    EXIT_UNAVAILABLE,
    GTK_FRAME_EXTENTS,
    HOME_GLYPH,
    INFO_PX,
    LOUDER_GLYPH,
    NET_FRAME_EXTENTS,
    NO_SOUND,
    QUIETER_GLYPH,
    SEND_EVERY_MS,
    SETTLE_S,
    SLIDER_PAD_PX,
    SLIDER_PX,
    TIME_GLYPH,
    TIME_POLL_S,
    TRACK_PX,
    VOLUME_MAX,
    VOLUME_STEP,
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
    TimeTone,
    WindowInfo,
    bar_origin,
    change_volume,
    clock_text,
    decode_cardinals,
    extents_from,
    fetch_state,
    frame_rect,
    implied_extents,
    is_app_window,
    is_kiosk,
    level_at,
    lost_maximize,
    read_audio,
    scope_of_pid,
    set_volume,
    shows_clock,
    stepped,
    stop_scope,
    time_left,
    window_extents,
)

gi.require_version("Gdk", "3.0")
gi.require_version("GdkX11", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("Wnck", "3.0")

from gi.repository import Gdk, GdkX11, GLib, Gtk, Wnck  # noqa: E402

log = logging.getLogger(__name__)

T = TypeVar("T")

TICK_MS = 500
"""How often a pending close and the bar's place are looked at again."""
AUDIO_POLL_MS = 2000
"""How often the volume is read again while an app is in front."""
RAISE_AGAIN_MS = 200
"""A second raise after the window manager has finished restacking."""
SETTLE_MS = int(SETTLE_S * 1000)
SCOPE_CACHE_MAX = 256

MAX_TRANSIENT_DEPTH = 8


def window_info(window: Any) -> WindowInfo:
    kind = window.get_window_type()
    return WindowInfo(
        xid=int(window.get_xid()),
        pid=int(window.get_pid() or 0),
        res_class=str(window.get_class_group_name() or ""),
        res_name=str(window.get_class_instance_name() or ""),
        kind=str(getattr(kind, "value_nick", "") or "").lower(),
    )


def _rect(values: Any) -> Rect:
    x, y, width, height = (int(value) for value in values)
    return Rect(x, y, width, height)


def _screen() -> Any:
    """The X screen through libwnck, as a pager: the window manager then honours
    the bar's requests to show and hide windows (EWMH source indication)."""
    Wnck.set_client_type(Wnck.ClientType.PAGER)
    screen = Wnck.Screen.get_default()
    if screen is not None:
        screen.force_update()
    return screen


def _server_time() -> int:
    return int(GdkX11.x11_get_server_time(Gdk.get_default_root_window()))


class XReader:
    """What the X server says about a window now, not what libwnck last heard.

    Over a Chromium window the owner dragged, the bar sat a title bar's height
    too low and well left of the corner: the frame the bar was placed by was
    not the frame on screen. So where the bar goes is worked out from the
    window's contents as the X server has them and the frame the window manager
    has put around them (`_NET_FRAME_EXTENTS`), both read at that moment. Any
    of it that cannot be read falls back to libwnck.
    """

    def __init__(self, display: Any) -> None:
        self.display = display

    def _window(self, xid: int) -> Any:
        try:
            return GdkX11.X11Window.foreign_new_for_display(self.display, xid)
        except Exception:  # a window that is gone, or no X11 display
            return None

    def client_rect(self, xid: int, scale: int = 1) -> Rect | None:
        """The window's contents, in the root window's pixels."""
        window = self._window(xid)
        if window is None:
            return None
        try:
            origin = window.get_origin()
            _x, _y, width, height = window.get_geometry()
            rect = Rect(int(origin[-2]), int(origin[-1]), int(width), int(height))
        except Exception:
            return None
        return Rect(rect.x * scale, rect.y * scale, rect.width * scale, rect.height * scale)

    def extents(self, xid: int, name: str) -> Extents | None:
        """A frame-extents property of the window, or None when it has none."""
        window = self._window(xid)
        if window is None:
            return None
        try:
            found = Gdk.property_get(
                window,
                Gdk.Atom.intern(name, False),
                Gdk.Atom.intern("CARDINAL", False),
                0,
                16,
                0,
            )
            ok, _kind, fmt, data = found
            if not ok or int(fmt) != 32:
                return None
            return extents_from(decode_cardinals(bytes(data), 4))
        except Exception:
            return None


class AppBarUi:
    """The bar: home, the minutes left, the app's sound, close - over the app in front."""

    def __init__(
        self, tr: Translator, screen: Any, *, uid: int | None, state_url: str | None = None
    ) -> None:
        self.tr = tr
        self.screen = screen
        self.uid = uid
        self.state_url = state_url
        self.closer = Closer()
        self.follower = Follower()
        self.sender = LevelSender()
        self.target: AppTarget | None = None
        self.audio = NO_SOUND
        self.shown = False
        self.origin: tuple[int, int] | None = None
        self._scopes: dict[tuple[int, int], str | None] = {}
        self._polling = False
        self._time_polling = False
        self._sliding = False
        self._watched: Any = None
        self._watched_xid: int | None = None
        self._watch_ids: list[int] = []
        self._settle_source: int | None = None
        self._send_source: int | None = None
        self._logged_frame: tuple[int, Rect, Rect] | None = None
        self._maximized_bits = int(
            Wnck.WindowState.MAXIMIZED_HORIZONTALLY | Wnck.WindowState.MAXIMIZED_VERTICALLY
        )
        self._maximize_action = int(Wnck.WindowActions.MAXIMIZE)
        self._buttons = (
            int(Gdk.ModifierType.BUTTON1_MASK)
            | int(Gdk.ModifierType.BUTTON2_MASK)
            | int(Gdk.ModifierType.BUTTON3_MASK)
        )
        self.xserver = XReader(Gdk.Display.get_default())
        self.pointer_pressed: Callable[[], bool] = self._read_pointer
        self.now: Callable[[], float] = time.monotonic
        # One thread for the sound server, one for stopping apps, one for the
        # launcher: a stop waits out the app's grace period, and neither the
        # volume nor the minutes should wait with it.
        self._sound = concurrent.futures.ThreadPoolExecutor(
            1, thread_name_prefix="appbar-sound"
        )
        self._stops = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="appbar-stop")
        self._clock = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="appbar-time")
        self.window = self._build()

        screen.connect("active-window-changed", self._changed)
        screen.connect("window-stacking-changed", self._changed)
        screen.connect("window-closed", self._changed)
        gdk_screen = Gdk.Screen.get_default()
        gdk_screen.connect("monitors-changed", self._changed)
        gdk_screen.connect("size-changed", self._changed)
        GLib.timeout_add(TICK_MS, self._tick)
        GLib.timeout_add(AUDIO_POLL_MS, self._poll_audio)
        GLib.timeout_add_seconds(TIME_POLL_S, self._poll_time)
        GLib.timeout_add_seconds(CLOCK_TICK_S, self._tick_clock)

    # ------------------------------------------------------------- building

    def _build(self) -> Any:
        window = Gtk.Window(type=Gtk.WindowType.POPUP)
        window.set_name(BAR_NAME)
        window.set_accept_focus(False)
        window.set_title("BioKids")

        self.home = self._button(HOME_GLYPH, "home", "appbar.home", self._on_home)
        self.info = self._build_info()
        self.quieter = self._button(
            QUIETER_GLYPH, "volume", "appbar.quieter", lambda _b: self._on_volume(louder=False)
        )
        self.slider_box = self._build_slider()
        self.louder = self._button(
            LOUDER_GLYPH, "volume", "appbar.louder", lambda _b: self._on_volume(louder=True)
        )
        self.close = self._button(CLOSE_GLYPH, "close", "appbar.close", self._on_close)

        # Close last: the screen's top-right corner, where a maximised window's
        # own close button was, so the way out has not moved. The minutes sit
        # between home and the sound, a gap with nothing to mistap.
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        for widget in (
            self.home,
            self.info,
            self.quieter,
            self.slider_box,
            self.louder,
            self.close,
        ):
            box.pack_start(widget, False, False, 0)
        window.add(box)

        provider = Gtk.CssProvider()
        # Raises GLib.Error on a sheet GTK cannot read: better no bar, and the
        # title bar's close button, than a bar of buttons too small to hit.
        provider.load_from_data(BAR_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        box.show_all()
        self.time_label.hide()
        self.clock_label.hide()
        self._show_audio(NO_SOUND)
        return window

    def _button(self, glyph: str, kind: str, name_key: str, handler: Callable[..., Any]) -> Any:
        button = Gtk.Button(label=glyph)
        button.set_can_focus(False)
        button.get_style_context().add_class(kind)
        button.get_accessible().set_name(self.tr(name_key))
        button.connect("clicked", handler)
        return button

    def _build_info(self) -> Any:
        """The minutes left today and the clock, one above the other."""
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        info.get_style_context().add_class("info")
        info.set_size_request(INFO_PX, -1)
        self.time_label = Gtk.Label()
        self.time_label.get_style_context().add_class("time")
        self.clock_label = Gtk.Label()
        self.clock_label.get_style_context().add_class("clock")
        for label in (self.time_label, self.clock_label):
            info.pack_start(label, True, True, 0)
        return info

    def _build_slider(self) -> Any:
        """The app's volume, as a slider a finger can drag anywhere along.

        A Gtk.Scale draws it; an event box over it takes the finger, along the
        bar's whole height and not only on the thin track, and moves the knob
        to wherever the finger is.
        """
        self.slider = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, VOLUME_MAX, VOLUME_STEP
        )
        self.slider.set_draw_value(False)
        self.slider.set_can_focus(False)
        self.slider.set_size_request(SLIDER_PX, -1)
        self.slider.set_valign(Gtk.Align.CENTER)
        holder = Gtk.EventBox()
        holder.set_visible_window(False)
        holder.set_above_child(True)
        holder.set_size_request(SLIDER_PX, BUTTON_TARGET_PX)
        holder.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.BUTTON_MOTION_MASK
        )
        holder.connect("button-press-event", self._on_slide_press)
        holder.connect("motion-notify-event", self._on_slide_motion)
        holder.connect("button-release-event", self._on_slide_release)
        holder.add(self.slider)
        return holder

    # ------------------------------------------------------- what is shown

    def _changed(self, *_args: Any) -> None:
        self.refresh(raise_bar=True)

    def refresh(self, *, raise_bar: bool) -> None:
        active = self.screen.get_active_window()
        info = window_info(active) if active is not None else None
        if info is None or not is_app_window(info):
            self.target = None
            self._watch(None)
            self.follower.forget()
            self._hide()
            return
        target = AppTarget(info.xid, info.pid, self._scope(info))
        moved_to_another_app = target != self.target
        self.target = target
        self._watch(active)
        if moved_to_another_app:
            self.sender = LevelSender()
            self._sliding = False
            self._show_audio(NO_SOUND)
        if self.follower.hiding(info.xid, self.now()):
            # Being moved or resized: the bar comes back where it stops.
            self._hide()
            return
        self._place(active)
        if not self.shown:
            self.window.show()
            self.shown = True
            raise_bar = True
            moved_to_another_app = True
        if raise_bar:
            self._raise()
            GLib.timeout_add(RAISE_AGAIN_MS, self._raise_once)
        if moved_to_another_app:
            self._poll_audio()
            self._poll_time()
            self._tick_clock()

    def _hide(self) -> None:
        if self.shown:
            self.window.hide()
            self.shown = False

    def _raise(self) -> None:
        gdk_window = self.window.get_window()
        if gdk_window is not None and self.shown:
            gdk_window.raise_()

    def _raise_once(self) -> bool:
        self._raise()
        return False

    def _frame(self, active: Any, scale: int) -> Rect:
        """Where the active window is on the screen, title bar and borders included."""
        reported = _rect(active.get_geometry())
        try:
            wnck_client = _rect(active.get_client_window_geometry())
        except (AttributeError, TypeError, ValueError):
            wnck_client = reported
        xid = int(active.get_xid())
        client = self.xserver.client_rect(xid, scale) or wnck_client
        extents = window_extents(
            self.xserver.extents(xid, NET_FRAME_EXTENTS),
            self.xserver.extents(xid, GTK_FRAME_EXTENTS),
            fallback=implied_extents(wnck_client, reported),
        )
        frame = frame_rect(client, extents) or reported
        if frame != reported and self._logged_frame != (xid, frame, reported):
            # What the VM run is asked to look at (VM-RUNBOOK section 8.9).
            log.debug(
                "window %#x: frame %s from the X server, libwnck said %s (extents %s)",
                xid,
                frame,
                reported,
                extents,
            )
            self._logged_frame = (xid, frame, reported)
        return frame

    def _place(self, active: Any) -> None:
        scale = max(1, int(self.window.get_scale_factor() or 1))
        frame = self._frame(active, scale)
        display = Gdk.Display.get_default()
        monitor = display.get_monitor_at_point(
            (frame.x + frame.width // 2) // scale, (frame.y + frame.height // 2) // scale
        )
        if monitor is not None:
            geometry = monitor.get_geometry()
            area = Rect(
                int(geometry.x) * scale,
                int(geometry.y) * scale,
                int(geometry.width) * scale,
                int(geometry.height) * scale,
            )
        else:
            root = Gdk.get_default_root_window()
            area = Rect(0, 0, int(root.get_width()) * scale, int(root.get_height()) * scale)
        _minimum, natural = self.window.get_preferred_size()
        origin = bar_origin(
            area, int(natural.width) * scale, frame, bar_height=BUTTON_TARGET_PX * scale
        )
        if origin != self.origin:
            self.window.move(origin[0] // scale, origin[1] // scale)
            self.origin = origin

    def _scope(self, info: WindowInfo) -> str | None:
        key = (info.xid, info.pid)
        if key not in self._scopes:
            if len(self._scopes) >= SCOPE_CACHE_MAX:
                self._scopes.clear()
            self._scopes[key] = scope_of_pid(info.pid, self.uid)
        return self._scopes[key]

    def _open_windows(self) -> dict[int, str | None]:
        found: dict[int, str | None] = {}
        for window in self.screen.get_windows() or []:
            info = window_info(window)
            if is_app_window(info):
                found[info.xid] = self._scope(info)
        return found

    def _kiosk(self) -> Any:
        for window in reversed(self.screen.get_windows_stacked() or []):
            if is_kiosk(window_info(window)):
                return window
        return None

    # ------------------------------------------------- a window that moves

    def _watch(self, window: Any) -> None:
        """Follow the geometry of the window in front, and of no other."""
        xid = None if window is None else int(window.get_xid())
        if xid == self._watched_xid:
            return
        if self._watched is not None:
            for handler in self._watch_ids:
                with contextlib.suppress(Exception):
                    self._watched.disconnect(handler)
        self._watched, self._watched_xid, self._watch_ids = window, xid, []
        if window is not None:
            self._watch_ids = [
                window.connect("geometry-changed", self._geometry_changed),
                window.connect("state-changed", self._state_changed),
            ]

    def _geometry_changed(self, window: Any) -> None:
        self._moved(window, unmaximized=False)

    def _state_changed(self, window: Any, changed: Any, new_state: Any) -> None:
        if lost_maximize(int(changed), int(new_state), self._maximized_bits):
            self._moved(window, unmaximized=True)

    def _moved(self, window: Any, *, unmaximized: bool) -> None:
        target = self.target
        if target is None or int(window.get_xid()) != target.xid:
            return
        hide = self.follower.changed(
            target.xid,
            self.now(),
            maximized=bool(window.is_maximized()),
            pressed=self.pointer_pressed(),
            unmaximized=unmaximized,
        )
        if hide:
            self._hide()
        elif self.shown:
            # A window that keeps moving by itself: the bar follows it.
            self._place(window)
        self._settle_later(SETTLE_MS)

    def _settle_later(self, delay_ms: int) -> None:
        if self._settle_source is None:
            self._settle_source = GLib.timeout_add(max(1, delay_ms), self._settle)

    def _settle(self) -> bool:
        self._settle_source = None
        window, target = self._watched, self.target
        if (
            window is None
            or target is None
            or self._watched_xid != target.xid
            or self.follower.xid not in (None, target.xid)
        ):
            self.follower.forget()
            self.refresh(raise_bar=True)
            return False
        step, wait_s = self.follower.settle(
            self.now(),
            maximized=bool(window.is_maximized()),
            can_maximize=self._can_maximize(window),
            pressed=self.pointer_pressed(),
        )
        if step is FollowStep.WAIT:
            self._settle_later(int(wait_s * 1000))
        elif step is FollowStep.MAXIMIZE:
            log.info("a window was dragged out of filling the screen; it fills it again")
            window.maximize()
            self._settle_later(SETTLE_MS)
        else:
            self.refresh(raise_bar=True)
        return False

    def _can_maximize(self, window: Any) -> bool:
        """Not Frozen Bubble's fixed-size window: that one is only re-placed. Nor
        a video gone full screen, which maximising would bring back out of it."""
        try:
            if window.is_fullscreen():
                return False
            return bool(int(window.get_actions()) & self._maximize_action)
        except (AttributeError, TypeError, ValueError):
            return False

    def _read_pointer(self) -> bool:
        """Whether a mouse button or a finger is down anywhere on the screen."""
        try:
            pointer = Gdk.Display.get_default().get_default_seat().get_pointer()
            found = Gdk.get_default_root_window().get_device_position(pointer)
            mask = int(found[-1])
        except Exception:
            return False
        return bool(mask & self._buttons)

    # ---------------------------------------------------------------- home

    def _on_home(self, _button: Any) -> None:
        """Send the app to the background and show the launcher."""
        stamp = Gtk.get_current_event_time()
        active = self.screen.get_active_window()
        if active is not None and is_app_window(window_info(active)):
            # A dialog goes down with the window it belongs to.
            for _ in range(MAX_TRANSIENT_DEPTH):
                parent = active.get_transient()
                if parent is None:
                    break
                active = parent
            active.minimize()
        kiosk = self._kiosk()
        if kiosk is not None:
            kiosk.activate(stamp)
        self._hide()

    # --------------------------------------------------------------- sound

    def _on_volume(self, *, louder: bool) -> None:
        target = self.target
        if target is None:
            return
        # The knob moves at the tap; the sound server's answer settles it.
        self.slider.set_value(stepped(int(self.slider.get_value()), louder=louder))
        uid = self.uid
        self._submit(
            self._sound,
            lambda: change_volume(
                target, louder=louder, scope_of=lambda pid: scope_of_pid(pid, uid)
            ),
            lambda state: self._audio_for(target, state),
        )

    def _on_slide_press(self, _holder: Any, event: Any) -> bool:
        if self.target is None or not self.audio.has_sound:
            return True
        self._sliding = True
        self._slide_to(float(event.x))
        return True

    def _on_slide_motion(self, _holder: Any, event: Any) -> bool:
        if self._sliding:
            self._slide_to(float(event.x))
        return True

    def _on_slide_release(self, _holder: Any, event: Any) -> bool:
        if self._sliding:
            self._slide_to(float(event.x))
            self._sliding = False
            self._send_later(0)
        return True

    def _slide_to(self, x: float) -> None:
        """The knob under the finger, and the app's sound after it."""
        width = int(self.slider.get_allocation().width)
        offset = int(self.slider.get_allocation().x) - int(self.slider_box.get_allocation().x)
        level = level_at(x - offset, SLIDER_PAD_PX, width - 2 * SLIDER_PAD_PX, TRACK_PX)
        self.slider.set_value(level)
        self._name_level(AudioState(has_sound=True, level=level))
        self.sender.want(level)
        self._send_later(SEND_EVERY_MS)

    def _send_later(self, delay_ms: int) -> None:
        if self._send_source is None:
            self._send_source = GLib.timeout_add(max(1, delay_ms), self._send_level)

    def _send_level(self) -> bool:
        self._send_source = None
        target, sender = self.target, self.sender
        level = sender.next() if target is not None else None
        if target is None or level is None:
            return False
        uid = self.uid

        def sent(state: AudioState | None) -> None:
            sender.done(level)
            if sender is not self.sender:
                return  # another app is in front now
            if sender.waiting():
                self._send_later(SEND_EVERY_MS)  # the finger moved on meanwhile
            elif state is not None:
                self._audio_for(target, state)

        self._submit(
            self._sound,
            lambda: set_volume(target, level, scope_of=lambda pid: scope_of_pid(pid, uid)),
            sent,
            failed=lambda: sent(None),
        )
        return False

    def _poll_audio(self) -> bool:
        target = self.target
        if target is not None and self.shown and not self._polling:
            self._polling = True
            uid = self.uid

            def done(state: AudioState) -> None:
                self._polling = False
                self._audio_for(target, state)

            self._submit(
                self._sound,
                lambda: read_audio(target, lambda pid: scope_of_pid(pid, uid)),
                done,
                failed=lambda: setattr(self, "_polling", False),
            )
        return True

    def _audio_for(self, target: AppTarget, state: AudioState) -> None:
        if target == self.target:
            self._show_audio(state)

    def _show_audio(self, state: AudioState) -> None:
        self.audio = state
        for widget in (self.quieter, self.louder, self.slider):
            widget.set_sensitive(state.has_sound)
        if self._sliding or self.sender.settling():
            # The finger, or what it last asked for, wins over a reading
            # taken before the sound server heard it.
            return
        self.slider.set_value(state.level if state.has_sound else 0)
        self._name_level(state)

    def _name_level(self, state: AudioState) -> None:
        self.slider.get_accessible().set_name(
            self.tr("appbar.level", percent=state.level)
            if state.has_sound
            else self.tr("appbar.no_sound")
        )

    # ------------------------------------------------------ time and clock

    def _poll_time(self) -> bool:
        url = self.state_url
        if url is not None and self.shown and not self._time_polling:
            self._time_polling = True

            def done(state: dict[str, Any] | None) -> None:
                self._time_polling = False
                self._show_time(state)

            self._submit(
                self._clock,
                lambda: fetch_state(url),
                done,
                failed=lambda: setattr(self, "_time_polling", False),
            )
        return True

    def _show_time(self, state: dict[str, Any] | None) -> None:
        """The minutes as the daemon counts them; nothing at all when it cannot say."""
        left = time_left(state)
        context = self.info.get_style_context()
        for tone in TimeTone:
            context.remove_class(tone.value)
        if left is None:
            self.time_label.hide()
        else:
            minutes = self.tr("shell.remaining_minutes", minutes=left.minutes)
            self.time_label.set_text(f"{TIME_GLYPH} {minutes}")
            self.time_label.get_accessible().set_name(
                f"{self.tr('shell.remaining_label')}: {minutes}"
            )
            if left.tone is not TimeTone.OK:
                context.add_class(left.tone.value)
            self.time_label.show()
        self.clock_label.set_visible(shows_clock(state))

    def _tick_clock(self) -> bool:
        self.clock_label.set_text(clock_text(time.localtime()))
        return True

    # --------------------------------------------------------------- close

    def _on_close(self, _button: Any) -> None:
        active = self.screen.get_active_window()
        if active is None:
            return
        info = window_info(active)
        if not is_app_window(info):
            return
        target = AppTarget(info.xid, info.pid, self._scope(info))
        action = self.closer.tap(target, self.now(), self._open_windows())
        if action is CloseAction.ASK:
            active.close(Gtk.get_current_event_time())
        elif action is CloseAction.STOP and target.scope is not None:
            log.info("a second tap on close: stopping %s", target.scope)
            self._stop(target.scope)

    def _stop(self, scope: str) -> None:
        def report(stopped: bool) -> None:
            if not stopped:
                log.warning("%s could not be stopped", scope)

        self._submit(self._stops, lambda: stop_scope(scope), report)

    def _tick(self) -> bool:
        step, scope = self.closer.tick(self.now(), self._open_windows())
        if step is CloseStep.STOP and scope is not None:
            log.info("%s did not close when asked; stopping its scope", scope)
            self._stop(scope)
        elif step is CloseStep.GIVE_UP:
            log.warning("an app window did not close (%s)", scope or "no scope")
        self.refresh(raise_bar=False)
        return True

    # ------------------------------------------------------------- plumbing

    def _submit(
        self,
        executor: concurrent.futures.ThreadPoolExecutor,
        work: Callable[[], T],
        done: Callable[[T], None],
        failed: Callable[[], None] | None = None,
    ) -> None:
        """Run `work` on a worker thread and hand its result to `done` in the main loop."""

        def deliver(future: concurrent.futures.Future[T]) -> bool:
            try:
                result = future.result()
            except Exception:  # a worker's failure must not take the bar down
                log.exception("the app bar's background work failed")
                if failed is not None:
                    failed()
                return False
            done(result)
            return False

        future = executor.submit(work)
        future.add_done_callback(lambda f: GLib.idle_add(deliver, f))

    def shutdown(self) -> None:
        for executor in (self._sound, self._stops, self._clock):
            executor.shutdown(wait=False, cancel_futures=True)


def run_bar(
    tr: Translator,
    *,
    uid: int | None,
    ready: Callable[[], None],
    state_url: str | None = None,
) -> int:
    """Show the bar until the launcher stops it. The exit code is the process's."""
    if Gdk.Display.get_default() is None:
        log.error("no X display; the app bar cannot be shown")
        return 1
    screen = _screen()
    if screen is None:
        log.error("libwnck found no screen; the app bar cannot be shown")
        return 1
    try:
        ui = AppBarUi(tr, screen, uid=uid, state_url=state_url)
    except GLib.Error as exc:
        log.error("the app bar's style sheet could not be read: %s", exc)
        return EXIT_UNAVAILABLE
    for signum in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, _quit)
    ui.refresh(raise_bar=True)
    ready()
    Gtk.main()
    ui.shutdown()
    return 0


def _quit(*_args: Any) -> bool:
    Gtk.main_quit()
    return False


def bring_back(scope: str, *, uid: int | None) -> int:
    """Show an app's window again: the second tap on its tile (D-197).

    The topmost ordinary window of that scope, un-minimised and in front, with
    a dialog it has open in front of it. 0 when shown, `EXIT_NOT_FOUND` when
    the app has no window.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return EXIT_UNAVAILABLE
    screen = _screen()
    if screen is None:
        return EXIT_UNAVAILABLE
    found: list[tuple[Any, WindowInfo]] = []
    for window in reversed(screen.get_windows_stacked() or []):
        info = window_info(window)
        if is_app_window(info) and scope_of_pid(info.pid, uid) == scope:
            found.append((window, info))
    if not found:
        return EXIT_NOT_FOUND
    window = next((w for w, info in found if info.kind == "normal"), found[0][0])
    window.activate_transient(_server_time())
    # The request is a client message to the root window; make sure it has
    # left this process before the process does.
    display.sync()
    return 0


def serve_control(*, uid: int | None) -> Any:
    """The running bar answers the launcher's "bring this app back" (D-208):
    `bring_back` above, on this main loop, asked over `control`'s socket."""
    from biokids_shell.appbar import control

    return control.serve(lambda scope: bring_back(scope, uid=uid), GLib.idle_add, uid=uid)
