#!/usr/bin/env python3
"""Make the website's pictures from the project's own.

    .venv/Scripts/python website/make_images.py          # everything
    .venv/Scripts/python website/make_images.py --og     # only the share cards

Like dev/make_icons.py, this is run by hand and its output is committed; the
build only copies. It needs Pillow, and Chrome or Chromium for the share cards
(a page drawn in Fredoka and photographed headless, so the cards use the real
typeface rather than a font Pillow can read).

What it makes, under static/:

- img/<lang>/<name>.webp and a smaller copy - the README's screenshots
  (assets/screenshots/<lang>/), and any extra ones in dist/website-shots/<lang>/
  (taken with dev/screenshots.py's method; see website/README.md);
- img/brand/ - the header's logo (the kite beside the logo's own "BioKids"),
  the same in cream for the dark footer, and the kite alone;
- img/illus/ - the boot menu's background (debian/grub/theme) and the
  installer's banner, for the guides' drawings;
- icons/ and the root's favicon.ico and apple-touch-icon.png;
- og/<lang>-<key>.jpg - a 1200 x 630 share card for every page.

and images.json beside this file: each picture's size, for width and height.
"""

from __future__ import annotations

import argparse
import contextlib
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
STATIC = HERE / "static"
BRAND = ROOT / "src" / "biokids_core" / "data" / "static" / "brand"
FONTS = ROOT / "src" / "biokids_core" / "data" / "static" / "fonts"
BG = (0xFB, 0xF7, 0xF2)
CREAM = (0xF6, 0xF0, 0xE8)

sys.path.insert(0, str(HERE))
from build import LANGS, load_pages, load_toml  # noqa: E402

CHROME = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "chromium",
    "google-chrome",
)

SMALL = 760
"""The width of the copy a phone loads instead (srcset)."""


def webp(src: Image.Image, dest: Path, quality: int = 84) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.save(dest, "WEBP", quality=quality, method=6)


def screenshots(sizes: dict[str, dict[str, int]]) -> None:
    for lang in LANGS:
        sources = sorted((ROOT / "assets" / "screenshots" / lang).glob("*.png"))
        extra = ROOT / "dist" / "website-shots" / lang
        if extra.is_dir():
            sources += sorted(extra.glob("*.png"))
        for src in sources:
            im = Image.open(src)
            im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
            if src.stem.startswith("wizard-") and im.width == 1100:
                # The wizard is a 680-px column in the middle of a 1100-px
                # page; the margins only make its words small on the site.
                im = im.crop((190, 0, 910, im.height))
            name = f"{lang}/{src.stem}"
            webp(im, STATIC / "img" / f"{name}.webp")
            entry = {"w": im.width, "h": im.height}
            if im.width > SMALL * 1.2:
                small = im.resize((SMALL, round(im.height * SMALL / im.width)), Image.LANCZOS)
                webp(small, STATIC / "img" / f"{name}-{SMALL}.webp", quality=80)
                entry["small"] = SMALL
            sizes[name] = entry
            print("  img", name, entry)


def lighten_ink(im: Image.Image) -> Image.Image:
    """The logo's dark ink turned cream, its colours kept: for the indigo
    footer. As dev/make_icons.py's light_ink: the logo's edges are soft in its
    alpha, not mixed into its colour, so the ink can be swapped outright."""
    im = im.convert("RGBA")
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a and max(r, g, b) < 120 and max(r, g, b) - min(r, g, b) < 40:
                px[x, y] = (*CREAM, a)
    return im


def word_of(logo: Image.Image) -> Image.Image:
    """The logo's "BioKids" and the small amber kite, without the big kite:
    every shape whose middle lies right of a third of the logo's width, as
    dev/make_icons.py's kite_only(word=True) cuts it."""
    alpha = logo.split()[3]
    w, h = logo.size
    a = alpha.load()
    seen = bytearray(w * h)
    drop = Image.new("L", (w, h), 0)
    d = drop.load()
    for y0 in range(h):
        for x0 in range(w):
            if seen[y0 * w + x0] or a[x0, y0] <= 128:
                continue
            stack, pixels = [(x0, y0)], []
            seen[y0 * w + x0] = 1
            while stack:
                x, y = stack.pop()
                pixels.append((x, y))
                for nx in (x - 1, x, x + 1):
                    for ny in (y - 1, y, y + 1):
                        if (
                            0 <= nx < w
                            and 0 <= ny < h
                            and not seen[ny * w + nx]
                            and a[nx, ny] > 128
                        ):
                            seen[ny * w + nx] = 1
                            stack.append((nx, ny))
            cx = sum(p[0] for p in pixels) / len(pixels)
            if cx < w * 0.34:
                for x, y in pixels:
                    d[x, y] = 255
    # The soft edge around a dropped shape goes with it.
    from PIL import ImageFilter

    drop = drop.filter(ImageFilter.MaxFilter(7))
    out = logo.copy()
    out.putalpha(Image.composite(Image.new("L", (w, h), 0), alpha, drop))
    return out.crop(out.getbbox())


def brand(sizes: dict[str, dict[str, int]]) -> None:
    out = STATIC / "img" / "brand"
    out.mkdir(parents=True, exist_ok=True)
    logo = Image.open(BRAND / "logo.png").convert("RGBA")
    kite = Image.open(BRAND / "kite.png").convert("RGBA")

    # The logo's "BioKids" and the little amber kite over its "s": everything
    # right of the big kite's sail, above the string's loop.
    word = word_of(logo)
    # The lockup, at twice its shown size (156 x 48): kite, then the word.
    h = 96
    k = kite.resize((round(kite.width * h / kite.height), h), Image.LANCZOS)
    wh = round(h * 0.62)
    w = word.resize((round(word.width * wh / word.height), wh), Image.LANCZOS)
    gap = 10
    lock = Image.new("RGBA", (k.width + gap + w.width, h), (0, 0, 0, 0))
    lock.alpha_composite(k, (0, 0))
    lock.alpha_composite(w, (k.width + gap, h - wh - 14))
    # Fit exactly 312 x 96 so the HTML's width and height are honest.
    canvas = Image.new("RGBA", (312, 96), (0, 0, 0, 0))
    scale = min(312 / lock.width, 1.0)
    if scale < 1:
        lock = lock.resize(
            (round(lock.width * scale), round(lock.height * scale)), Image.LANCZOS
        )
    canvas.alpha_composite(lock, (0, (96 - lock.height) // 2))
    canvas.save(out / "lockup.png", optimize=True)
    lighten_ink(canvas).save(out / "lockup-light.png", optimize=True)

    kite.resize((240, round(kite.height * 240 / kite.width)), Image.LANCZOS).save(
        out / "kite.png", optimize=True
    )
    full = logo.resize((480, round(logo.height * 480 / logo.width)), Image.LANCZOS)
    full.save(out / "logo.png", optimize=True)
    sizes["brand/logo"] = {"w": full.width, "h": full.height}
    print("  brand: lockup, lockup-light, kite, logo")


def sail() -> Image.Image:
    """The kite's sail alone, square: what the favicon shows."""
    kite = Image.open(BRAND / "kite.png").convert("RGBA")
    # dev/make_icons.py's SAIL_SHARE: the top half of the kite is its sail.
    top = kite.crop((0, 0, kite.width, round(kite.height * 0.5)))
    top = top.crop(top.getbbox())
    side = max(top.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.alpha_composite(top, ((side - top.width) // 2, (side - top.height) // 2))
    return sq


def icons() -> None:
    out = STATIC / "icons"
    out.mkdir(parents=True, exist_ok=True)
    root = HERE / "root"
    root.mkdir(exist_ok=True)
    shutil.copy2(BRAND / "favicon.ico", root / "favicon.ico")
    s = sail()
    for size in (96, 192):
        s.resize((size, size), Image.LANCZOS).save(out / f"favicon-{size}.png", optimize=True)
    kite = Image.open(BRAND / "kite.png").convert("RGBA")

    def on_square(size: int, share: float, rounded: bool) -> Image.Image:
        sq = Image.new("RGBA", (size, size), (*BG, 255))
        kh = round(size * share)
        k = kite.resize((round(kite.width * kh / kite.height), kh), Image.LANCZOS)
        sq.alpha_composite(k, ((size - k.width) // 2, (size - kh) // 2))
        if rounded:
            mask = Image.new("L", (size * 4, size * 4), 0)
            from PIL import ImageDraw

            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, size * 4 - 1, size * 4 - 1), radius=size * 4 * 0.22, fill=255
            )
            sq.putalpha(mask.resize((size, size), Image.LANCZOS))
        return sq

    on_square(180, 0.78, False).convert("RGB").save(
        root / "apple-touch-icon.png", optimize=True
    )
    on_square(192, 0.74, True).save(out / "icon-192.png", optimize=True)
    on_square(512, 0.74, True).save(out / "icon-512.png", optimize=True)
    # Maskable: full bleed, the kite inside the middle 80% a launcher keeps.
    on_square(512, 0.62, False).save(out / "icon-maskable-512.png", optimize=True)
    print("  icons: favicon.ico, favicon-96/192, apple-touch-icon, icon-192/512, maskable")


def illustrations(sizes: dict[str, dict[str, int]]) -> None:
    out = STATIC / "img" / "illus"
    grub = Image.open(ROOT / "debian" / "grub" / "theme" / "background.png").convert("RGB")
    grub = grub.resize((1280, 720), Image.LANCZOS)
    webp(grub, out / "grub.webp", quality=82)
    sizes["illus/grub"] = {"w": 1280, "h": 720}
    banner = ROOT / "iso" / "config" / "includes.installer" / "usr" / "share" / "graphics"
    b = Image.open(banner / "logo_debian.png").convert("RGB")
    webp(b, out / "installer-banner.webp", quality=88)
    sizes["illus/installer-banner"] = {"w": b.width, "h": b.height}
    greeter = Image.open(ROOT / "assets" / "greeter" / "background.png").convert("RGB")
    greeter = greeter.resize((1280, 720), Image.LANCZOS)
    webp(greeter, out / "greeter.webp", quality=80)
    sizes["illus/greeter"] = {"w": 1280, "h": 720}
    print("  illus: grub, installer-banner, greeter")


# --------------------------------------------------------------- share cards

OG_PAGE = """<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><style>
@font-face {{ font-family: Fredoka; font-weight: 400;
  src: url("{fonts}/Fredoka-Regular.woff2"); }}
@font-face {{ font-family: Fredoka; font-weight: 500;
  src: url("{fonts}/Fredoka-Medium.woff2"); }}
html, body {{ margin: 0; width: 1200px; height: 630px; overflow: hidden; }}
body {{ font-family: Fredoka, sans-serif; color: #221E1C; background: #FBF7F2;
  position: relative; }}
body::before {{ content: ""; position: absolute; inset: 0; background:
  radial-gradient(45% 60% at 0% 0%, rgba(255,107,91,.22), transparent 70%),
  radial-gradient(45% 60% at 100% 0%, rgba(255,176,32,.22), transparent 70%),
  radial-gradient(50% 65% at 100% 100%, rgba(31,182,166,.2), transparent 70%),
  radial-gradient(50% 65% at 0% 100%, rgba(75,84,200,.16), transparent 70%); }}
.text {{ position: absolute; left: 72px; top: 64px; width: {tw}px; }}
.lock {{ height: 64px; display: block; margin-bottom: 44px; }}
h1 {{ font-weight: 500; font-size: {fs}px; line-height: 1.08; margin: 0 0 22px;
  letter-spacing: -.5px; }}
p {{ font-size: 28px; line-height: 1.35; color: #6B625C; margin: 0; }}
.foot {{ position: absolute; left: 72px; bottom: 54px; display: flex; gap: 10px; }}
.chip {{ padding: 8px 18px; border-radius: 999px; background: #fff;
  border: 1px solid #E7DED5; font-size: 22px; }}
.media {{ position: absolute; right: 56px; top: 50%; transform: translateY(-50%); }}
.media img.shot {{ display: block; width: {mw}px; border-radius: 22px;
  box-shadow: 0 24px 60px rgba(34,30,28,.18); border: 1px solid #E7DED5; }}
.tile {{ width: 300px; height: 300px; border-radius: 72px; display: grid;
  place-items: center; font-size: 170px; background: {tint};
  font-family: "Segoe UI Emoji", "Noto Color Emoji", "Apple Color Emoji", sans-serif;
  box-shadow: 0 24px 60px rgba(34,30,28,.12); }}
.kite {{ height: 420px; transform: rotate(-6deg); }}
</style></head><body>
<div class="text"><img class="lock" src="{lockup}" alt="">
<h1>{title}</h1><p>{sub}</p></div>
<div class="foot">{chips}</div>
<div class="media">{media}</div>
</body></html>"""

TINTS = ["#FFE7E3", "#FFF1D6", "#DDF4F1", "#E5E7FA"]
SHOT_FOR = {
    "home": "hero",
    "features": "launcher",
    "download": "launcher",
    "privacy": "first-run",
    "guide-live": "panel-live",
    "guide-assistant": "assistant",
    "guide-time": "panel-time",
    "guide-web": "blocked",
}
HOME_TITLE = {
    "tr": "Çocuklar için güvenli bir bilgisayar",
    "en": "A safe computer for young children",
}
CHIPS = {
    "tr": ["Ücretsiz", "Açık kaynak", "Bulut yok", "4–10 yaş"],
    "en": ["Free", "Open source", "No cloud", "Ages 4–10"],
}


def find_chrome() -> str:
    for c in CHROME:
        if Path(c).is_file():
            return c
        found = shutil.which(c)
        if found:
            return found
    sys.exit("make_images: no Chrome, Edge or Chromium found for the share cards")


def photograph(chrome: str, url: str, png: Path, profile: Path) -> None:
    """A 1200 x 630 picture of `url`. Headless Chrome on Windows sometimes
    writes the picture and never exits, so wait for the file, not the process,
    and end the whole process tree once the file has stopped growing."""
    proc = subprocess.Popen(
        [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            "--allow-file-access-from-files",
            f"--user-data-dir={profile}",
            "--window-size=1200,630",
            "--force-device-scale-factor=1",
            "--virtual-time-budget=2000",
            f"--screenshot={png}",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    last = -1
    while time.monotonic() < deadline and proc.poll() is None:
        if png.exists():
            size = png.stat().st_size
            if size and size == last:
                break
            last = size
        time.sleep(0.5)
    if proc.poll() is None:
        proc.kill()
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        # Otherwise it goes in its own time; the picture is written.
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)


def og_cards() -> None:
    chrome = find_chrome()
    pages = load_pages()
    out = STATIC / "og"
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpd:
        tmp = Path(tmpd)
        for page in pages:
            if page.noindex:
                continue
            for lang in LANGS:
                v = page.v(lang)
                title = HOME_TITLE[lang] if page.key == "home" else v.short
                sub = v.summary if page.kind == "guide" else v.description
                if len(sub) > 120:
                    # The first sentence if it fits, else words up to the edge.
                    first = sub.split(". ")[0].rstrip(".") + "."
                    sub = first if len(first) <= 125 else sub[:117].rsplit(" ", 1)[0] + "…"
                shot_name = SHOT_FOR.get(page.key)
                if shot_name:
                    shot = ROOT / "assets" / "screenshots" / lang / f"{shot_name}.png"
                    tall = Image.open(shot).height > Image.open(shot).width
                    mw = 300 if tall else 560
                    media = f'<img class="shot" src="{shot.as_uri()}" alt="">'
                    tw = 1200 - 72 - mw - 56 - 48
                elif page.kind == "guide":
                    tint = TINTS[(page.guide["order"] - 1) % 4]
                    icon = page.guide["icon"]
                    media = f'<div class="tile" style="background:{tint}">{icon}</div>'
                    mw, tw = 300, 1200 - 72 - 300 - 56 - 60
                else:
                    kite = (STATIC / "img" / "brand" / "kite.png").as_uri()
                    media = f'<img class="kite" src="{kite}" alt="">'
                    mw, tw = 250, 1200 - 72 - 250 - 56 - 80
                fs = 64 if len(title) <= 28 else 54 if len(title) <= 40 else 46
                doc = OG_PAGE.format(
                    lang=lang,
                    fonts=FONTS.as_uri(),
                    lockup=(STATIC / "img" / "brand" / "lockup.png").as_uri(),
                    title=html.escape(title),
                    sub=html.escape(sub),
                    chips="".join(
                        f'<span class="chip">{html.escape(c)}</span>' for c in CHIPS[lang]
                    ),
                    media=media,
                    tw=tw,
                    mw=mw,
                    fs=fs,
                    tint=TINTS[0],
                )
                src = tmp / f"{lang}-{page.key}.html"
                src.write_text(doc, encoding="utf-8")
                png = tmp / f"{lang}-{page.key}.png"
                # A profile per card: a Chrome still closing holds its own.
                for attempt in range(3):  # Chrome now and then draws nothing at all
                    profile = tmp / f"profile-{lang}-{page.key}-{attempt}"
                    photograph(chrome, src.as_uri(), png, profile)
                    if png.exists():
                        break
                if not png.exists():
                    sys.exit(f"make_images: Chrome made no picture for {lang}-{page.key}")
                im = Image.open(png).convert("RGB")
                if im.size != (1200, 630):
                    im = ImageOps.fit(im, (1200, 630), Image.LANCZOS, centering=(0, 0))
                im.save(
                    out / f"{lang}-{page.key}.jpg",
                    "JPEG",
                    quality=84,
                    optimize=True,
                    progressive=True,
                )
                print("  og", f"{lang}-{page.key}.jpg")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--og", action="store_true", help="only the share cards")
    args = ap.parse_args()
    load_toml("site.toml")  # fail early on a broken file
    manifest = HERE / "images.json"
    sizes: dict[str, dict[str, int]] = (
        json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    )
    if not args.og:
        screenshots(sizes)
        brand(sizes)
        icons()
        illustrations(sizes)
        manifest.write_text(
            json.dumps(dict(sorted(sizes.items())), indent=1) + "\n", encoding="utf-8"
        )
    og_cards()
    return 0


if __name__ == "__main__":
    sys.exit(main())
