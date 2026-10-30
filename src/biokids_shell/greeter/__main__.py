"""biokids-greeter: LightDM's greeter, drawn with GTK (D-251).

Run by /usr/lib/biokids/biokids-greeter, as LightDM's `lightdm` user, on the
login screen's own X display. It exits with `CANNOT_START` before it has spoken
to LightDM when it cannot run at all - no LightDM library, no display - and the
wrapper then runs Debian's GTK greeter in its place.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from biokids_shell.greeter import (
    CANNOT_START,
    Answer,
    Face,
    child_accounts,
    faces,
    on_question,
)

log = logging.getLogger("biokids-greeter")

BACKGROUND = Path("/usr/share/biokids/greeter/background.png")
FALLBACK_PICTURE = (
    Path(__file__).resolve().parents[2] / "biokids_core" / "data" / "avatars" / "fox.png"
)
"""A picture for someone AccountsService has none for."""
PICTURE_PX = 168

CSS = b"""
window { background-color: #FBF7F2; }
.face {
  background-image: none;
  background-color: #FFFFFF;
  border: 3px solid #E8E1D6;
  border-radius: 32px;
  padding: 14px 18px;
  box-shadow: none;
}
.face:hover, .face:focus { border-color: #4B54C8; }
.face.is-chosen { border-color: #FF6B5B; }
.face-name { font-size: 22px; font-weight: bold; color: #2B2B3A; }
.panel {
  background-color: #FFFFFF;
  border: 2px solid #E8E1D6;
  border-radius: 24px;
  padding: 16px 20px;
}
.prompt { font-size: 20px; color: #2B2B3A; }
entry { font-size: 22px; min-height: 46px; min-width: 320px; }
.go {
  background-image: none;
  background-color: #4B54C8;
  color: #FFFFFF;
  font-size: 20px;
  font-weight: bold;
  border-radius: 999px;
  padding: 8px 26px;
  border: none;
}
.quiet {
  background-image: none;
  background-color: #FFFFFF;
  color: #2B2B3A;
  font-size: 17px;
  border-radius: 999px;
  padding: 6px 18px;
  border: 2px solid #E8E1D6;
}
.message { font-size: 20px; font-weight: bold; color: #B4320F; }
"""


ROOT_COLOUR = "#FBF7F2"
"""The launcher's background, for the root window while a session starts."""


def paint_root() -> None:
    """The root window in the launcher's colour; nothing when xsetroot is missing."""
    if shutil.which("xsetroot") is None:
        return
    try:
        subprocess.run(
            ["xsetroot", "-solid", ROOT_COLOUR],
            timeout=3,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("xsetroot failed", exc_info=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s biokids-greeter: %(message)s"
    )
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        gi.require_version("LightDM", "1")
        from gi.repository import Gtk, LightDM

        if not Gtk.init_check(sys.argv)[0]:
            raise RuntimeError("no display to draw on")
        from biokids_core.i18n import normalise_locale, translator

        tr = translator(normalise_locale(os.environ.get("LANG", "tr")[:2]))
        greeter = LightDM.Greeter()
        greeter.connect_to_daemon_sync()
    except Exception:
        log.exception("the BioKids login screen cannot start; Debian's will")
        return CANNOT_START
    LoginScreen(greeter, tr).show()
    Gtk.main()
    return 0


class LoginScreen:
    """The pictures, the one question PAM may ask, and the way back."""

    def __init__(self, greeter: Any, tr: Any) -> None:
        from gi.repository import Gdk, Gtk, LightDM

        self.Gtk, self.Gdk, self.LightDM = Gtk, Gdk, LightDM
        self.greeter = greeter
        self.tr = tr
        self.chosen: Face | None = None
        self.buttons: dict[str, Any] = {}
        greeter.connect("show-prompt", self._on_prompt)
        greeter.connect("show-message", self._on_message)
        greeter.connect("authentication-complete", self._on_complete)

        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        display = Gdk.Display.get_default()
        # Without a window manager the root window shows X's cross, not an arrow.
        Gdk.get_default_root_window().set_cursor(
            Gdk.Cursor.new_for_display(display, Gdk.CursorType.LEFT_PTR)
        )
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        area = monitor.get_geometry()
        self.width, self.height = area.width, area.height

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_decorated(False)
        self.window.move(area.x, area.y)
        self.window.set_default_size(self.width, self.height)

        self.window.connect("key-press-event", self._on_key)

        overlay = Gtk.Overlay()
        overlay.add(self._background())
        overlay.add_overlay(self._middle())
        overlay.add_overlay(self._corner())
        self.window.add(overlay)

    # ------------------------------------------------------------- drawing

    def show(self) -> None:
        self.window.show_all()
        self.panel.hide()
        self.message.set_text("")
        if self.buttons:
            next(iter(self.buttons.values())).grab_focus()

    def _background(self) -> Any:
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
            log.exception("no background: the plain colour instead")
        return image

    def _middle(self) -> Any:
        from gi.repository import Gtk

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        column.set_halign(Gtk.Align.CENTER)
        # From a fixed height under the logo, so that the password question
        # opens downwards rather than pushing the pictures up into it.
        column.set_valign(Gtk.Align.START)
        column.set_margin_top(round(self.height * 0.36))

        users = []
        if not self.greeter.get_hide_users_hint():
            users = self.LightDM.UserList.get_instance().get_users()
        shown = faces(users, child_accounts())
        # One centred row, as many as fit; a family with more people than that
        # wraps onto rows of the same width.
        per_row = max(1, min(len(shown), (self.width - 120) // 250))
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        rows.set_halign(Gtk.Align.CENTER)
        for start in range(0, len(shown), per_row):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=22)
            row.set_halign(Gtk.Align.CENTER)
            row.set_homogeneous(True)
            for face in shown[start : start + per_row]:
                row.pack_start(self._face(face), False, False, 0)
            rows.pack_start(row, False, False, 0)
        column.pack_start(rows, False, False, 0)

        self.message = Gtk.Label()
        self.message.get_style_context().add_class("message")
        self.message.set_line_wrap(True)
        self.message.set_max_width_chars(48)
        self.message.set_justify(Gtk.Justification.CENTER)
        column.pack_start(self.message, False, False, 0)

        self.panel = self._question()
        column.pack_start(self.panel, False, False, 0)
        return column

    def _face(self, face: Face) -> Any:
        from gi.repository import GdkPixbuf, Gtk

        button = Gtk.Button()
        button.get_style_context().add_class("face")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        picture = Gtk.Image()
        for path in (face.picture, str(FALLBACK_PICTURE)):
            try:
                picture.set_from_pixbuf(
                    GdkPixbuf.Pixbuf.new_from_file_at_scale(path, PICTURE_PX, PICTURE_PX, True)
                )
                break
            except Exception as error:
                log.info("no picture at %r: %s", path, error)
                continue
        name = Gtk.Label(label=face.name)
        name.get_style_context().add_class("face-name")
        box.pack_start(picture, False, False, 0)
        box.pack_start(name, False, False, 0)
        button.add(box)
        button.connect("clicked", lambda _button: self._tap(face))
        self.buttons[face.user] = button
        return button

    def _question(self) -> Any:
        from gi.repository import Gtk

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        panel.get_style_context().add_class("panel")
        panel.set_halign(Gtk.Align.CENTER)
        self.prompt = Gtk.Label()
        self.prompt.get_style_context().add_class("prompt")
        self.entry = Gtk.Entry()
        self.entry.connect("activate", self._answer)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        buttons.set_halign(Gtk.Align.CENTER)
        go = Gtk.Button(label=self.tr("greeter.go"))
        go.get_style_context().add_class("go")
        go.connect("clicked", self._answer)
        back = Gtk.Button(label=self.tr("greeter.back"))
        back.get_style_context().add_class("quiet")
        back.connect("clicked", lambda _button: self._start_over())
        buttons.pack_start(back, False, False, 0)
        buttons.pack_start(go, False, False, 0)
        for widget in (self.prompt, self.entry, buttons):
            panel.pack_start(widget, False, False, 0)
        return panel

    def _corner(self) -> Any:
        from gi.repository import Gtk

        corner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        corner.set_halign(Gtk.Align.END)
        corner.set_valign(Gtk.Align.START)
        corner.set_margin_top(18)
        corner.set_margin_end(18)
        other = Gtk.Button(label=self.tr("greeter.other"))
        other.get_style_context().add_class("quiet")
        other.connect("clicked", lambda _button: self._tap(None))
        corner.pack_start(other, False, False, 0)
        if self.LightDM.get_can_shutdown():
            off = Gtk.Button(label=f"⏻ {self.tr('greeter.shutdown')}")
            off.get_style_context().add_class("quiet")
            off.connect("clicked", self._shutdown)
            corner.pack_start(off, False, False, 0)
        return corner

    # ------------------------------------------------------------- talking

    def _tap(self, face: Face | None) -> None:
        """A picture, or "another account" (None): PAM is asked about it."""
        from gi.repository import GLib

        self._cancel()
        self.chosen = face
        for user, button in self.buttons.items():
            context = button.get_style_context()
            if face is not None and user == face.user:
                context.add_class("is-chosen")
            else:
                context.remove_class("is-chosen")
        self.message.set_text(self.tr("greeter.opening") if face and face.child else "")
        try:
            self.greeter.authenticate(face.user if face is not None else None)
        except GLib.Error as error:
            log.warning("LightDM did not start the login: %s", error.message)
            self.message.set_text(self.tr("greeter.failed"))

    def _on_prompt(self, _greeter: Any, text: str, prompt_type: Any) -> None:
        if on_question(self.chosen) is Answer.CHILD_KEPT_OUT:
            self._cancel()
            self.message.set_text(self.tr("greeter.child_open"))
            return
        secret = prompt_type == self.LightDM.PromptType.SECRET
        self.prompt.set_text(self.tr("greeter.password") if secret else self.tr("greeter.name"))
        self.entry.set_visibility(not secret)
        self.entry.set_text("")
        self.panel.show_all()
        self.entry.grab_focus()

    def _on_message(self, _greeter: Any, text: str, _message_type: Any) -> None:
        if text:
            self.message.set_text(text)

    def _answer(self, *_args: Any) -> None:
        from gi.repository import GLib

        text = self.entry.get_text()
        self.entry.set_text("")
        try:
            self.greeter.respond(text)
        except GLib.Error as error:
            log.warning("LightDM did not take the answer: %s", error.message)

    def _on_complete(self, greeter: Any) -> None:
        from gi.repository import GLib

        if greeter.get_is_authenticated():
            self.message.set_text(self.tr("greeter.opening"))
            # The screen between this and the session's own is the root window:
            # the launcher's colour rather than X's black (D-254).
            paint_root()
            try:
                greeter.start_session_sync(None)
            except GLib.Error as error:
                log.warning("the session did not start: %s", error.message)
                self.message.set_text(self.tr("greeter.failed"))
            return
        # A wrong password: ask the same person again.
        chosen = self.chosen
        self.panel.hide()
        self.message.set_text(self.tr("greeter.wrong"))
        if chosen is not None and not chosen.child:
            self._tap(chosen)
            self.message.set_text(self.tr("greeter.wrong"))

    def _cancel(self) -> None:
        if self.greeter.get_in_authentication():
            self.greeter.cancel_authentication()
        self.panel.hide()

    def _start_over(self) -> None:
        self._cancel()
        self.chosen = None
        self.message.set_text("")
        for button in self.buttons.values():
            button.get_style_context().remove_class("is-chosen")

    def _on_key(self, _window: Any, event: Any) -> bool:
        if event.keyval == self.Gdk.KEY_Escape:
            self._start_over()
            return True
        return False

    def _shutdown(self, _button: Any) -> None:
        from gi.repository import GLib

        try:
            self.LightDM.shutdown()
        except GLib.Error as error:
            log.warning("the computer did not switch off: %s", error.message)


if __name__ == "__main__":
    sys.exit(main())
