"""The kite while a child's screen opens (D-254)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from biokids_shell import splash

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "debian" / "xsession" / "45biokids-splash"


def test_the_splash_goes_once_the_launcher_window_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter(["", "62914561\n"])
    asked: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        asked.append(argv)
        out = next(answers)
        return subprocess.CompletedProcess(argv, 0 if out else 1, stdout=out, stderr="")

    monkeypatch.setattr(splash.shutil, "which", lambda _name: "/usr/bin/xdotool")
    monkeypatch.setattr(splash.subprocess, "run", run)
    assert not splash.launcher_shown()
    assert splash.launcher_shown()
    assert asked[0] == ["xdotool", "search", "--onlyvisible", "--class", "biokids-kiosk"]


def test_without_xdotool_the_splash_waits_for_its_longest() -> None:
    assert splash.LONGEST_S <= 60, "a launcher that never comes is not hidden for long"


def test_the_session_hook_is_for_a_child_only_and_never_stops_the_session() -> None:
    text = HOOK.read_text(encoding="utf-8")
    assert '*" biokids-child "*)' in text
    assert "python3 -m biokids_shell.splash >/dev/null 2>&1 &" in text, "in the background"
    assert "exit" not in text and "set -e" not in text, "sourced by Xsession"
    install = (ROOT / "debian" / "biokids-shell.install").read_text(encoding="utf-8")
    assert "debian/xsession/45biokids-splash etc/X11/Xsession.d/" in install
