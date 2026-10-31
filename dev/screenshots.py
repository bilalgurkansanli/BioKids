#!/usr/bin/env python3
"""Capture the screenshots the README needs.

A parental-control project with no screenshots gets no stars
(`docs/PACKAGING.md` §6), and a screenshot taken by hand goes stale the first
time a margin changes. This drives a headless Chromium over a running panel and
launcher and writes the images where the README expects them.

It is a development tool, not part of the build: run it, look at what came out,
commit the images.

    python dev/screenshots.py --panel http://127.0.0.1:7878 \\
                             --shell http://127.0.0.1:7879

The panel needs a signed-in session, so a PIN is passed and the login is done
first in the same profile.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "screenshots"

CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "chromium",
    "chromium-browser",
    "google-chrome",
)


@dataclass(frozen=True, slots=True)
class Shot:
    name: str
    path: str
    width: int = 1280
    height: int = 900
    target: str = "panel"


# No phone shot. Headless Chromium's `--window-size` sizes the *image*, not the
# viewport, so a 420-wide capture is a crop of a desktop layout rather than a
# phone rendering — which looks like a responsive bug that is not there, and
# would hide a real one. Phone screenshots come from a phone, or from devtools
# emulation, and the layout itself is asserted at 375px in the browser.
SHOTS: tuple[Shot, ...] = (
    Shot("panel-today", "/today", 980, 1000),
    Shot("panel-child-time", "/children/1/time", 980, 1250),
    Shot("panel-history", "/history", 1100, 1000),
    Shot("panel-audit", "/audit", 1100, 900),
    Shot("launcher", "/", 1280, 800, target="shell"),
    Shot("blocked", "/blocked?url=https://example.com", 1280, 800, target="shell"),
)

SHELL_LOAD_MS = 5000
"""How long a launcher page loads before it is captured. The launcher holds an
`EventSource` open, so its page never finishes loading and
`--virtual-time-budget` alone waits until the subprocess times out;
`--timeout` stops the load and captures what is drawn."""


def find_browser() -> str | None:
    for candidate in CANDIDATES:
        if Path(candidate).is_file():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def wait_for(url: str, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def run_browser(browser: str, profile: Path, url: str, extra: list[str]) -> str:
    result = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
            "--no-sandbox",
            # Bounded rather than waiting for a quiet network: both UIs hold an
            # `EventSource` open, so the page is never "finished loading".
            "--virtual-time-budget=3000",
            # No `--force-device-scale-factor`. With it, `--window-size` stops
            # describing the CSS viewport and the phone shot silently becomes a
            # 420px-wide crop of a desktop layout — which is exactly the bug a
            # phone screenshot exists to catch.
            *extra,
            url,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    return result.stderr.strip()


def sign_in(browser: str, profile: Path, panel: str, pin: str) -> None:
    """Get a session cookie into the shared profile.

    Chromium will not navigate to a top-level `data:` URL, so the login is a
    one-line HTML file on disk that submits itself. It is a real sign-in through
    the real form: nothing in the panel knows this is a screenshot run, and
    there is no bypass to leave behind by accident.
    """
    page = profile / "login.html"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "<!DOCTYPE html><meta charset=utf-8>"
        f'<body onload="document.forms[0].submit()">'
        f'<form method="post" action="{panel}/login">'
        f'<input name="pin" value="{pin}"><input name="next" value="/today">'
        "</form></body>",
        encoding="utf-8",
    )
    run_browser(browser, profile, page.as_uri(), ["--window-size=900,900"])


def capture(
    browser: str,
    profile: Path,
    url: str,
    out: Path,
    width: int,
    height: int,
    load_ms: int | None = None,
) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    # Remove it first. Otherwise a failed capture leaves the previous run's image
    # in place and this reports success — which is how a screenshot quietly stops
    # matching the thing it is a screenshot of.
    out.unlink(missing_ok=True)
    extra = [f"--window-size={width},{height}", f"--screenshot={out}"]
    if load_ms is not None:
        extra.append(f"--timeout={load_ms}")
    stderr = run_browser(browser, profile, url, extra)
    if not out.is_file():
        print(f"  ! {out.name}: nothing written. {stderr[:200]}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", default="http://127.0.0.1:7878")
    parser.add_argument("--shell", default="http://127.0.0.1:7879")
    parser.add_argument("--pin", default="284913", help="the demo family's PIN")
    parser.add_argument(
        "--with-shell",
        action="store_true",
        help=(
            "also capture the launcher, from one served with a fake daemon. "
            "Emoji come from the machine taking the screenshot, so on Windows "
            "the tiles show Microsoft's rather than Noto's."
        ),
    )
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    browser = find_browser()
    if browser is None:
        print("no Chromium or Chrome found; cannot take screenshots")
        return 1
    print(f"using {browser}")

    bases = {"panel": args.panel.rstrip("/"), "shell": args.shell.rstrip("/")}
    wanted = ["panel"] + (["shell"] if args.with_shell else [])
    reachable = {
        name: (wait_for(f"{bases[name]}/healthz", timeout=8) if name in wanted else False)
        for name in bases
    }
    for name in wanted:
        if not reachable[name]:
            print(f"  ! {name} is not answering at {bases[name]}; its shots are skipped")

    written = 0
    # One profile for the whole run, so the sign-in cookie survives into every
    # panel shot that follows it.
    with tempfile.TemporaryDirectory(prefix="biokids-shot-") as raw_profile:
        profile = Path(raw_profile)
        if reachable["panel"]:
            print("  signing in")
            sign_in(browser, profile, bases["panel"], args.pin)

        for shot in SHOTS:
            if shot.target == "shell" and not args.with_shell:
                continue
            if not reachable[shot.target]:
                continue
            url = f"{bases[shot.target]}{shot.path}"
            out = args.out / f"{shot.name}.png"
            print(f"  {shot.name:<22} {url}")
            load_ms = SHELL_LOAD_MS if shot.target == "shell" else None
            if capture(browser, profile, url, out, shot.width, shot.height, load_ms):
                written += 1

    print(f"\n{written} screenshot(s) in {args.out}")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
