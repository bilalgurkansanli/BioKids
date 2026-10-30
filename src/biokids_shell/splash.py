"""The kite while a child's screen opens (D-254).

`python3 -m biokids_shell.splash`, started by /etc/X11/Xsession.d/45biokids-splash
in a child's session only.

A child tapped their picture and looked at a black screen for several seconds:
a child's session has no desktop to draw a background, and the launcher is a
browser that takes a moment to start. Now the login screen's own picture - the
logo over the warm off-white, Zio and Sio - stays, with a spinner, until the
launcher's window is on the screen, so the pictures go and the launcher comes
with nothing in between. It closes itself once the launcher is there, and in
any case after `LONGEST_S`: a launcher that never comes is the launcher's
problem to show, not the splash's to hide.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

from biokids_shell.kiosk import KIOSK_WINDOW_CLASS

log = logging.getLogger("biokids-splash")

BACKGROUND: Final = Path("/usr/share/biokids/greeter/background.png")
"""The login screen's picture, so that the two are one screen."""
LOOK_EVERY_MS: Final = 400
"""How often the launcher's window is looked for."""
AFTER_MS: Final = 700
"""How long the splash stays once the launcher's window is there: Chromium maps
its window a moment before it has painted the page."""
LONGEST_S: Final = 60
CSS: Final = b"""
window { background-color: #FBF7F2; }
spinner { color: #4B54C8; min-width: 48px; min-height: 48px; }
"""


def launcher_shown() -> bool:
    """Whether the launcher's kiosk window is mapped on the screen."""
    if shutil.which("xdotool") is None:
        return False
    try:
        found = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--class", KIOSK_WINDOW_CLASS],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return found.returncode == 0 and bool(found.stdout.split())


class Splash:
    """One full-screen window: the picture, a spinner, nothing to tap."""

    def __init__(self) -> None:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gdk, GLib, Gtk

        self.Gtk, self.GLib = Gtk, GLib
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_geometry()
        self.width, self.height = area.width, area.height

        window = Gtk.Window(title="BioKids")
        window.set_decorated(False)
        window.set_accept_focus(False)
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        window.set_keep_above(True)
        window.set_type_hint(Gdk.WindowTypeHint.SPLASHSCREEN)
        window.set_default_size(self.width, self.height)
        window.move(area.x, area.y)
        overlay = Gtk.Overlay()
        overlay.add(self._picture())
        spinner = Gtk.Spinner()
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_valign(Gtk.Align.START)
        spinner.set_margin_top(round(self.height * 0.5))
        spinner.start()
        overlay.add_overlay(spinner)
        window.add(overlay)
        window.connect("destroy", lambda *_a: Gtk.main_quit())
        self.window = window
        self.seen = False
        self.closed = False

    def _picture(self) -> Any:
        from gi.repository import GdkPixbuf

        image = self.Gtk.Image()
        try:
            source = GdkPixbuf.Pixbuf.new_from_file(str(BACKGROUND))
            scale = max(self.width / source.get_width(), self.height / source.get_height())
            width = max(self.width, round(source.get_width() * scale))
            height = max(self.height, round(source.get_height() * scale))
            scaled = source.scale_simple(width, height, GdkPixbuf.InterpType.BILINEAR)
            left, top = (width - self.width) // 2, (height - self.height) // 2
            image.set_from_pixbuf(scaled.new_subpixbuf(left, top, self.width, self.height))
        except Exception:
            log.exception("no picture: the plain colour instead")
        return image

    def _look(self) -> bool:
        if not self.seen and launcher_shown():
            self.seen = True
            self.GLib.timeout_add(AFTER_MS, self._close)
            return False
        return True

    def _close(self) -> bool:
        if not self.closed:
            self.closed = True
            self.window.destroy()
        return False

    def run(self) -> None:
        self.window.show_all()
        self.GLib.timeout_add(LOOK_EVERY_MS, self._look)
        self.GLib.timeout_add_seconds(LONGEST_S, self._close)
        self.Gtk.main()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    try:
        Splash().run()
    except Exception:
        # Never in the child's way: no splash is a black screen for a moment.
        log.exception("the splash could not be shown")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
