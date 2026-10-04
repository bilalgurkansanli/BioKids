"""D-163: a child can always get out of an app.

Until the owner tried it, a child's windows had no buttons and a maximised
window had no title bar, so Stellarium - which starts full screen - could only
be left through menus it hides. These pin every part of the fix: the window
manager settings and the file new children get, the theme's button size, the
rule that fills the screen, the windowed start of every app that would
otherwise go full screen, and the browser using the same title bar.
"""

from __future__ import annotations

import asyncio
import json
import struct
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

import pytest

from biokids_shell import windows
from biokids_shell.launcher import AppLauncher, LaunchRequest, prepare_browser_profile
from biokids_shell.windows import (
    THEME_NAME,
    WINDOW_MANAGER_SETTINGS,
    WindowRules,
    apply_window_manager_settings,
)

ROOT = Path(__file__).resolve().parent.parent
XFWM4_XML = ROOT / "src" / "biokids_shell" / "xfce" / "xfwm4.xml"
THEME_DIR = ROOT / "src" / "biokids_shell" / "xfce" / "theme" / THEME_NAME / "xfwm4"
RULES = ROOT / "src" / "biokids_shell" / "devilspie2" / "biokids.lua"
CATALOG = ROOT / "src" / "biokids_core" / "data" / "catalog" / "catalog.toml"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


# ------------------------------------------------------ the window manager


class Recorder:
    def __init__(self, *, ok: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.ok = ok

    async def __call__(self, argv: Sequence[str]) -> bool:
        self.calls.append(list(argv))
        return self.ok


async def test_every_setting_is_applied_live_in_the_childs_session() -> None:
    """Live, every login: provisioning never overwrites an existing child's file."""
    run = Recorder()
    applied = await apply_window_manager_settings(run, xfconf_query="/usr/bin/xfconf-query")

    assert applied == len(WINDOW_MANAGER_SETTINGS)
    props = {call[call.index("-p") + 1]: call[call.index("-s") + 1] for call in run.calls}
    assert props["/general/theme"] == THEME_NAME
    assert props["/general/button_layout"] == "|C", "one button, close, on the right"
    assert props["/general/titleless_maximize"] == "false", "a maximised window keeps its bar"
    for call in run.calls:
        assert call[:3] == ["/usr/bin/xfconf-query", "-c", "xfwm4"]
        assert "--create" in call, "a key the file never had must still be set"


async def test_without_xfconf_the_launcher_still_starts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    run = Recorder()
    monkeypatch.setattr(windows.shutil, "which", lambda _name: None)
    applied = await apply_window_manager_settings(run)
    assert applied == 0
    assert run.calls == []
    assert "xfconf-query is not installed" in caplog.text


async def test_a_setting_that_does_not_take_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    applied = await apply_window_manager_settings(
        Recorder(ok=False), xfconf_query="xfconf-query"
    )
    assert applied == 0
    assert "window manager settings could be applied" in caplog.text


def test_a_new_childs_file_says_the_same_as_the_live_settings() -> None:
    """Two places carry these keys; they must never disagree."""
    tree = ET.parse(XFWM4_XML)
    general = {
        prop.get("name"): (prop.get("type"), prop.get("value"))
        for prop in tree.getroot().iter("property")
        if prop.get("value") is not None
    }
    for path, kind, value in WINDOW_MANAGER_SETTINGS:
        name = path.rsplit("/", 1)[-1]
        assert general.get(name) == (kind, value), f"xfwm4.xml disagrees about {name}"


# ------------------------------------------------------------------ theme


def test_the_close_button_is_big_enough_for_a_four_year_old() -> None:
    """UX.md section 2.4: at least 64 x 64 for the 4-7 band."""
    for state in ("active", "inactive", "prelight", "pressed"):
        width, height = png_size(THEME_DIR / f"close-{state}.png")
        assert width >= 64, f"close-{state}.png is {width} wide"
        assert height >= 64, f"close-{state}.png is {height} tall"


def test_the_theme_has_every_image_xfwm4_asks_for() -> None:
    """A missing image makes xfwm4 fall back to the default theme's tiny buttons."""
    assert (THEME_DIR / "themerc").is_file()
    names = {path.name for path in THEME_DIR.iterdir()}
    for state in ("active", "inactive"):
        for n in range(1, 6):
            assert f"title-{n}-{state}.png" in names
        for part in (
            "top-left",
            "top-right",
            "left",
            "right",
            "bottom",
            "bottom-left",
            "bottom-right",
        ):
            assert f"{part}-{state}.png" in names
    for button in ("close", "hide", "maximize", "menu", "shade", "stick"):
        for state in ("active", "inactive", "prelight", "pressed"):
            assert f"{button}-{state}.png" in names


def test_the_title_bar_is_as_tall_as_the_button() -> None:
    _, bar = png_size(THEME_DIR / "title-3-active.png")
    _, button = png_size(THEME_DIR / "close-active.png")
    assert bar == button


# ------------------------------------------------------------------ rules


def test_the_rule_maximises_app_windows_and_leaves_the_launcher_alone() -> None:
    text = RULES.read_text(encoding="utf-8")
    assert 'get_window_type() == "WINDOW_TYPE_NORMAL"' in text, "dialogs stay small"
    assert '~= "biokids-kiosk"' in text
    assert "maximize()" in text


def test_the_rule_names_the_class_the_kiosk_actually_sets() -> None:
    from biokids_shell.kiosk import KIOSK_WINDOW_CLASS

    assert f'"{KIOSK_WINDOW_CLASS}"' in RULES.read_text(encoding="utf-8")


def test_devilspie2_is_pointed_at_the_rules_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(windows.shutil, "which", lambda name: f"/usr/bin/{name}")
    argv = WindowRules(tmp_path).argv()
    assert argv == ["/usr/bin/devilspie2", "--folder", str(tmp_path)]


async def test_no_devilspie2_means_no_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(windows.shutil, "which", lambda _name: None)
    rules = WindowRules(tmp_path)
    await rules.supervise(asyncio.Event())
    assert rules.process is None
    assert "devilspie2 is not installed" in caplog.text


# ------------------------------------------------------------- the catalog


FULL_SCREEN_BY_DEFAULT = {
    # slug: the option that starts it windowed, from each program's --help
    "tuxpaint": ["--windowed"],
    "tuxmath": ["--windowed"],
    "gcompris": ["--window"],
    "supertux": ["--window"],
    "frozen-bubble": ["--no-fullscreen"],
}


@pytest.mark.parametrize(("slug", "option"), sorted(FULL_SCREEN_BY_DEFAULT.items()))
def test_apps_that_would_go_full_screen_start_windowed(slug: str, option: list[str]) -> None:
    """Full screen hides the title bar, and with it the only way out."""
    apps = {
        app["slug"]: app for app in tomllib.loads(CATALOG.read_text(encoding="utf-8"))["app"]
    }
    argv = apps[slug]["exec"]
    joined = " ".join(argv)
    assert " ".join(option) in joined, f"{slug} starts as {argv}"


# -------------------------------------------------------------- the browser


def test_the_childs_browser_uses_the_window_manager_title_bar(tmp_path: Path) -> None:
    profile = tmp_path / "browser"
    assert prepare_browser_profile(profile) is True
    prefs = json.loads((profile / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["browser"]["custom_chrome_frame"] is False


def test_the_childs_other_browser_preferences_are_kept(tmp_path: Path) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(
        json.dumps({"browser": {"show_home_button": True}, "homepage": "x"}), encoding="utf-8"
    )

    assert prepare_browser_profile(tmp_path) is True

    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert prefs["browser"] == {"show_home_button": True, "custom_chrome_frame": False}
    assert prefs["homepage"] == "x"


def test_a_running_browser_is_not_written_under(tmp_path: Path) -> None:
    """It holds its preferences in memory and would write them straight back."""
    lock = tmp_path / "SingletonLock"
    try:
        # What Chromium leaves: a symlink to "<host>-<pid>", dangling by design.
        lock.symlink_to("biokids-vm-4242")
    except OSError:  # pragma: no cover - Windows without symlink rights
        lock.write_text("", encoding="utf-8")
    assert prepare_browser_profile(tmp_path) is False
    assert not (tmp_path / "Default" / "Preferences").exists()


def test_a_preferences_file_that_cannot_be_read_is_left_alone(tmp_path: Path) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text("{not json", encoding="utf-8")

    assert prepare_browser_profile(tmp_path) is False
    assert prefs_path.read_text(encoding="utf-8") == "{not json"


def test_the_childs_browser_asks_websites_for_the_familys_language(tmp_path: Path) -> None:
    """YouTube Kids set itself up in English: the profile asked for en-US."""
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(
        json.dumps({"intl": {"selected_languages": "en-US,en"}}), encoding="utf-8"
    )

    assert prepare_browser_profile(tmp_path, "tr") is True

    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert prefs["intl"]["selected_languages"] == "tr-TR,tr,en-US,en"


def test_languages_someone_chose_are_kept(tmp_path: Path) -> None:
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(
        json.dumps({"intl": {"selected_languages": "de-DE,de"}}), encoding="utf-8"
    )

    assert prepare_browser_profile(tmp_path, "tr") is True

    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert prefs["intl"]["selected_languages"] == "de-DE,de"


async def test_a_web_tile_sets_the_title_bar_before_the_browser_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Path] = []
    monkeypatch.setattr(
        "biokids_shell.launcher.prepare_browser_profile",
        lambda profile, _locale: seen.append(profile) or True,
    )
    launcher = AppLauncher()
    monkeypatch.setattr(launcher, "child_browser_profile", lambda: tmp_path)
    monkeypatch.setattr("biokids_shell.launcher.shutil.which", lambda _name: None)

    async def no_process(*_args: object, **_kwargs: object) -> object:
        raise OSError("not starting anything in a test")

    monkeypatch.setattr("biokids_shell.launcher.asyncio.create_subprocess_exec", no_process)

    await launcher.launch(
        LaunchRequest(slug="eba", name="EBA", kind="web", url="https://www.eba.gov.tr")
    )

    assert seen == [tmp_path]
