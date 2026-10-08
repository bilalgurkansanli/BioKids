"""The app bar's window, in GTK 3 with libwnck 3 (D-197).

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

**Nothing slow in the window's thread.** `pactl` and `systemctl` run on worker
threads, and only plain values cross back; libwnck and GTK are touched from the
main loop alone.
"""

from __future__ import annotations

import concurrent.futures
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
    CLOSE_GLYPH,
    EXIT_NOT_FOUND,
    EXIT_UNAVAILABLE,
    HOME_GLYPH,
    LOUDER_GLYPH,
    NO_SOUND,
    QUIETER_GLYPH,
    AppTarget,
    AudioState,
    CloseAction,
    Closer,
    CloseStep,
    Rect,
    WindowInfo,
    bar_origin,
    change_volume,
    is_app_window,
    is_kiosk,
    level_markup,
    read_audio,
    scope_of_pid,
    stop_scope,
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
"""How often the volume indicator is refreshed while an app is in front."""
RAISE_AGAIN_MS = 200
"""A second raise after the window manager has finished restacking."""
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


class AppBarUi:
    """The bar: four buttons and a level, over whichever app is in front."""

    def __init__(self, tr: Translator, screen: Any, *, uid: int | None) -> None:
        self.tr = tr
        self.screen = screen
        self.uid = uid
        self.closer = Closer()
        self.target: AppTarget | None = None
        self.audio = NO_SOUND
        self.shown = False
        self.origin: tuple[int, int] | None = None
        self._scopes: dict[tuple[int, int], str | None] = {}
        self._polling = False
        # One thread for the sound server, one for stopping apps: a stop waits
        # out the app's grace period, and the volume should not wait with it.
        self._sound = concurrent.futures.ThreadPoolExecutor(
            1, thread_name_prefix="appbar-sound"
        )
        self._stops = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="appbar-stop")
        self.window = self._build()

        screen.connect("active-window-changed", self._changed)
        screen.connect("window-stacking-changed", self._changed)
        screen.connect("window-closed", self._changed)
        gdk_screen = Gdk.Screen.get_default()
        gdk_screen.connect("monitors-changed", self._changed)
        gdk_screen.connect("size-changed", self._changed)
        GLib.timeout_add(TICK_MS, self._tick)
        GLib.timeout_add(AUDIO_POLL_MS, self._poll_audio)

    # ------------------------------------------------------------- building

    def _build(self) -> Any:
        window = Gtk.Window(type=Gtk.WindowType.POPUP)
        window.set_name(BAR_NAME)
        window.set_accept_focus(False)
        window.set_title("BioKids")

        self.home = self._button(HOME_GLYPH, "home", "appbar.home", self._on_home)
        self.quieter = self._button(
            QUIETER_GLYPH, "volume", "appbar.quieter", lambda _b: self._on_volume(louder=False)
        )
        self.level = Gtk.Label()
        self.level.get_style_context().add_class("level")
        self.louder = self._button(
            LOUDER_GLYPH, "volume", "appbar.louder", lambda _b: self._on_volume(louder=True)
        )
        self.close = self._button(CLOSE_GLYPH, "close", "appbar.close", self._on_close)

        # Close last: the screen's top-right corner, where a maximised window's
        # own close button was, so the way out has not moved.
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        for widget in (self.home, self.quieter, self.level, self.louder, self.close):
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
        self._show_audio(NO_SOUND)
        return window

    def _button(self, glyph: str, kind: str, name_key: str, handler: Callable[..., Any]) -> Any:
        button = Gtk.Button(label=glyph)
        button.set_can_focus(False)
        button.get_style_context().add_class(kind)
        button.get_accessible().set_name(self.tr(name_key))
        button.connect("clicked", handler)
        return button

    # ------------------------------------------------------- what is shown

    def _changed(self, *_args: Any) -> None:
        self.refresh(raise_bar=True)

    def refresh(self, *, raise_bar: bool) -> None:
        active = self.screen.get_active_window()
        info = window_info(active) if active is not None else None
        if info is None or not is_app_window(info):
            self.target = None
            self._hide()
            return
        target = AppTarget(info.xid, info.pid, self._scope(info))
        moved_to_another_app = target != self.target
        self.target = target
        self._place(active)
        if not self.shown:
            self.window.show()
            self.shown = True
            raise_bar = True
        if raise_bar:
            self._raise()
            GLib.timeout_add(RAISE_AGAIN_MS, self._raise_once)
        if moved_to_another_app:
            self._show_audio(NO_SOUND)
            self._poll_audio()

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

    def _place(self, active: Any) -> None:
        x, y, width, height = active.get_geometry()
        scale = max(1, int(self.window.get_scale_factor() or 1))
        display = Gdk.Display.get_default()
        monitor = display.get_monitor_at_point(
            (x + width // 2) // scale, (y + height // 2) // scale
        )
        if monitor is not None:
            geometry = monitor.get_geometry()
            area = Rect(geometry.x, geometry.y, geometry.width, geometry.height)
        else:
            root = Gdk.get_default_root_window()
            area = Rect(0, 0, int(root.get_width()), int(root.get_height()))
        _minimum, natural = self.window.get_preferred_size()
        origin = bar_origin(area, int(natural.width), Rect(x, y, width, height))
        if origin != self.origin:
            self.window.move(*origin)
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
        uid = self.uid
        self._submit(
            self._sound,
            lambda: change_volume(
                target, louder=louder, scope_of=lambda pid: scope_of_pid(pid, uid)
            ),
            lambda state: self._audio_for(target, state),
        )

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
        self.quieter.set_sensitive(state.has_sound)
        self.louder.set_sensitive(state.has_sound)
        self.level.set_markup(level_markup(state))
        self.level.get_accessible().set_name(
            self.tr("appbar.level", percent=state.level)
            if state.has_sound
            else self.tr("appbar.no_sound")
        )

    # --------------------------------------------------------------- close

    def _on_close(self, _button: Any) -> None:
        active = self.screen.get_active_window()
        if active is None:
            return
        info = window_info(active)
        if not is_app_window(info):
            return
        target = AppTarget(info.xid, info.pid, self._scope(info))
        action = self.closer.tap(target, time.monotonic(), self._open_windows())
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
        step, scope = self.closer.tick(time.monotonic(), self._open_windows())
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
        self._sound.shutdown(wait=False, cancel_futures=True)
        self._stops.shutdown(wait=False, cancel_futures=True)


def run_bar(tr: Translator, *, uid: int | None, ready: Callable[[], None]) -> int:
    """Show the bar until the launcher stops it. The exit code is the process's."""
    if Gdk.Display.get_default() is None:
        log.error("no X display; the app bar cannot be shown")
        return 1
    screen = _screen()
    if screen is None:
        log.error("libwnck found no screen; the app bar cannot be shown")
        return 1
    try:
        ui = AppBarUi(tr, screen, uid=uid)
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
