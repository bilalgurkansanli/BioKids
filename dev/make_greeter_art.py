#!/usr/bin/env python3
"""Draw the login screen's background and the children's pictures (D-170).

    python3 dev/make_greeter_art.py background [--browser PATH]
    python3 dev/make_greeter_art.py avatars [--font PATH]

`background` writes `assets/greeter/background.png` (1920x1080; LightDM's GTK
greeter scales it to the screen): Zio and Sio from `assets/characters`, happy,
under the BioKids mark, drawn as HTML and photographed by a headless
Chromium-family browser. Not on a BioKids machine: the children's managed
browser policy is machine-wide, and under it headless Chromium loads nothing.
Edge or Chrome on the Windows workstation does it.

`avatars` writes `src/biokids_core/data/avatars/<slug>.png` (256x256, a pastel
disc with the panel's emoji, transparent outside it) with Pillow and Noto Color
Emoji, the font the children's screens draw emoji with. Run it on Debian, where
both are packaged (python3-pil, fonts-noto-color-emoji).

The output is committed; a build does not run this.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from biokids_panel.context import AVATARS  # noqa: E402

CHARACTERS = ROOT / "assets" / "characters"
BACKGROUND = ROOT / "assets" / "greeter" / "background.png"
AVATAR_DIR = ROOT / "src" / "biokids_core" / "data" / "avatars"
AVATAR_SIZE = 256

# docs/UX.md section 1.
BG = "#FBF7F2"
CORAL = "#FF6B5B"
AMBER = "#FFB020"
TEAL = "#1FB6A6"
INDIGO = "#4B54C8"

DISC = {
    "fox": "#FFE1D6",
    "bear": "#F3E3D3",
    "cat": "#FFF0C7",
    "owl": "#E6E3FA",
    "rabbit": "#FCE4EC",
    "turtle": "#D8F3EE",
    "whale": "#DCEBFB",
    "bee": "#FFF3C4",
    "parent": "#D6EFE4",
}
"""A soft colour behind each animal, so two children's pictures differ at a
glance even before the animal is recognised."""

PARENT = ("parent", "🏡")
"""The parent's own account (D-221): a house, which is not among the children's
animals, so the grown-up's login never looks like a child's."""

MARK = f"""<svg viewBox="0 0 100 100" width="96" height="96" aria-hidden="true">
  <rect x="4" y="4" width="92" height="92" rx="22" fill="{INDIGO}"/>
  <path d="M30 68 A20 20 0 0 0 70 68 Z" fill="{AMBER}"/>
  <circle cx="38" cy="44" r="17" fill="{CORAL}"/>
  <circle cx="62" cy="44" r="17" fill="{TEAL}"/>
</svg>"""
"""The PWA icon (dev/make_icons.py) as vectors."""


def character(name: str) -> str:
    """A character's SVG, happy. The expression is a class on the root element."""
    svg = (CHARACTERS / f"{name}.svg").read_text(encoding="utf-8")
    return svg.replace(f'class="bk-char bk-{name}"', f'class="bk-char bk-{name} is-happy"', 1)


def background_html() -> str:
    blob = "position:absolute;border-radius:50%;filter:blur(2px);"
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html, body {{ margin: 0; width: 1920px; height: 1080px; overflow: hidden; background: {BG}; }}
body {{ position: relative; font-family: "Noto Sans", "DejaVu Sans", sans-serif; }}
.brand {{ position: absolute; top: 96px; left: 0; right: 0; display: flex;
          align-items: center; justify-content: center; gap: 24px; }}
.brand span {{ font-size: 72px; font-weight: 800; color: {INDIGO}; letter-spacing: -1px; }}
.char {{ position: absolute; bottom: 70px; width: 380px; height: 380px; }}
.char svg {{ width: 100%; height: 100%; }}
</style></head><body>
<div style="{blob}width:900px;height:900px;left:-330px;top:-420px;
            background:radial-gradient(circle, {CORAL}33 0%, {CORAL}00 70%);"></div>
<div style="{blob}width:1000px;height:1000px;right:-380px;bottom:-460px;
            background:radial-gradient(circle, {TEAL}33 0%, {TEAL}00 70%);"></div>
<div style="{blob}width:620px;height:620px;right:-120px;top:-260px;
            background:radial-gradient(circle, {AMBER}2e 0%, {AMBER}00 70%);"></div>
<div style="{blob}width:700px;height:700px;left:-260px;bottom:-380px;
            background:radial-gradient(circle, {INDIGO}1f 0%, {INDIGO}00 70%);"></div>
<div class="brand">{MARK}<span>BioKids</span></div>
<div class="char" style="left:170px">{character("zio")}</div>
<div class="char" style="right:170px">{character("sio")}</div>
</body></html>"""


def screenshot(chromium: str, html: str, width: int, height: int, out: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(html, encoding="utf-8")
        shot = Path(tmp) / "shot.png"
        subprocess.run(
            [
                chromium,
                "--headless=new",
                "--disable-gpu",
                "--hide-scrollbars",
                "--force-device-scale-factor=1",
                "--default-background-color=00000000",
                f"--user-data-dir={tmp}/profile",
                f"--window-size={width},{height}",
                f"--screenshot={shot}",
                page.as_uri(),
            ],
            check=True,
            capture_output=True,
            timeout=120,
        )
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(shot, out)
    print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size} bytes)")


BROWSERS = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
)

EMOJI_FONT = Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf")
EMOJI_STRIKE = 109
"""Noto Color Emoji is a bitmap font with one size; Pillow draws it at this one."""


def make_background(browser: str | None) -> int:
    found = browser or next((b for b in BROWSERS if shutil.which(b) or Path(b).exists()), None)
    if found is None:
        print("needs Edge, Chrome or Chromium (not on a BioKids machine)", file=sys.stderr)
        return 1
    screenshot(shutil.which(found) or found, background_html(), 1920, 1080, BACKGROUND)
    return 0


def make_avatars(font_path: Path) -> int:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(font_path), EMOJI_STRIKE)
    size = AVATAR_SIZE
    big = size * 4  # drawn large and scaled down, for a smooth edge
    for slug, emoji in [*AVATARS, PARENT]:
        disc = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        ImageDraw.Draw(disc).ellipse((0, 0, big - 1, big - 1), fill=DISC.get(slug, "#EEEEEE"))
        disc = disc.resize((size, size), Image.Resampling.LANCZOS)

        glyph = Image.new("RGBA", (EMOJI_STRIKE * 2, EMOJI_STRIKE * 2), (0, 0, 0, 0))
        ImageDraw.Draw(glyph).text((0, 0), emoji, font=font, embedded_color=True)
        box = glyph.getbbox()
        if box is None:
            print(f"{slug}: the font drew nothing for {emoji!r}", file=sys.stderr)
            return 1
        glyph = glyph.crop(box)
        side = int(size * 0.62)
        scale = side / max(glyph.size)
        glyph = glyph.resize(
            (round(glyph.width * scale), round(glyph.height * scale)), Image.Resampling.LANCZOS
        )
        disc.alpha_composite(glyph, ((size - glyph.width) // 2, (size - glyph.height) // 2))

        out = AVATAR_DIR / f"{slug}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        disc.save(out, optimize=True)
        print(f"wrote {out.relative_to(ROOT)} ({out.stat().st_size} bytes)")
    return 0


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = parser.add_subparsers(dest="what", required=True)
    background = sub.add_parser("background")
    background.add_argument("--browser", help="a Chromium-family browser to draw with")
    avatars = sub.add_parser("avatars")
    avatars.add_argument("--font", type=Path, default=EMOJI_FONT)
    args = parser.parse_args(argv)
    if args.what == "background":
        return make_background(args.browser)
    return make_avatars(args.font)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
