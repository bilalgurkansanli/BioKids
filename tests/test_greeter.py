"""The login screen: the family's pictures (D-251)."""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path

from biokids_shell.greeter import Answer, Face, faces, on_question

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class User:
    name: str
    shown: str | None = None
    image: str | None = None
    logged_in: bool = False

    def get_name(self) -> str:
        return self.name

    def get_display_name(self) -> str | None:
        return self.shown

    def get_image(self) -> str | None:
        return self.image

    def get_logged_in(self) -> bool:
        return self.logged_in


def test_the_children_come_first_and_each_by_their_name() -> None:
    users = [
        User("bilal", "Annem", "/var/lib/AccountsService/icons/bilal"),
        User("zeynep", "Zeynep"),
        User("ahmet", None, "/var/lib/AccountsService/icons/ahmet", logged_in=True),
    ]
    shown = faces(users, {"ahmet", "zeynep"})
    assert [f.user for f in shown] == ["ahmet", "zeynep", "bilal"]
    assert shown[0] == Face(
        user="ahmet",
        name="ahmet",
        picture="/var/lib/AccountsService/icons/ahmet",
        child=True,
        logged_in=True,
    )
    assert shown[2].name == "Annem" and shown[2].child is False


def test_a_child_asked_for_a_password_is_kept_out_and_a_grown_up_is_asked() -> None:
    """D-155: a child cannot open their own session left behind the screen."""
    child = Face("ahmet", "Ahmet", "", child=True, logged_in=True)
    parent = Face("bilal", "Annem", "", child=False, logged_in=False)
    assert on_question(child) is Answer.CHILD_KEPT_OUT
    assert on_question(parent) is Answer.ASK
    assert on_question(None) is Answer.ASK, "another account: its name and password"


def test_lightdm_runs_it_and_falls_back_to_its_own() -> None:
    conf = (ROOT / "debian" / "pam" / "50-biokids.conf").read_text(encoding="utf-8")
    assert "greeter-session=biokids-greeter" in conf
    entry = configparser.ConfigParser(interpolation=None)
    desktop = ROOT / "debian" / "greeter" / "biokids-greeter.desktop"
    entry.read_string(desktop.read_text(encoding="utf-8"))
    assert entry["Desktop Entry"]["Exec"] == "/usr/lib/biokids/biokids-greeter"
    wrapper = (ROOT / "debian" / "greeter" / "biokids-greeter").read_text(encoding="utf-8")
    assert "exec /usr/sbin/lightdm-gtk-greeter" in wrapper, "a parent is never locked out"
    assert '[ "$status" -lt 128 ]' in wrapper, "not when LightDM stopped it for a session"
    control = (ROOT / "debian" / "control").read_text(encoding="utf-8")
    assert "lightdm-gtk-greeter" in control and "gir1.2-lightdm-1" in control
