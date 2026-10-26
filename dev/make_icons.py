#!/usr/bin/env python3
"""Make the logo files and the app icons from the logo (D-243).

    python3 dev/make_icons.py

The logo is `assets/brand/logo.jpg`: the kite, its string ending in a heart, and
"BioKids" beside it, on the warm off-white of `--bg`. It was made as a picture,
not drawn as vectors, so everything here is cut from that picture:

- `src/biokids_core/data/static/brand/logo.png` - the whole logo on a
  transparent background, for the panel's top bar, its login and setup pages,
  the login screen's background and the README;
- `src/biokids_core/data/static/brand/kite.png` - the kite alone, the same way;
- `icon-192.png` and `icon-512.png` for the panel and the launcher, which are
  also the desktop's icon (debian/rules): the kite on a rounded `--bg` square.
  Maskable-safe - the kite sits inside the inner 80% so a circular mask does not
  clip it.
- `iso/config/includes.chroot/usr/share/plymouth/themes/biokids/watermark.png` -
  the logo while the image's computer starts (D-251);
- `debian/parent/biokids-desktop.png` - a parent's desktop background (D-245):
  the logo in the middle of the warm off-white, the four brand colours glowing
  softly in the corners, as the login screen does;
- `favicon.png` (64 px) and `favicon.ico` (16, 32 and 48 px) for the browser's
  tab (D-245): the kite's sail alone, filling the square. At sixteen pixels the
  string and the heart are a smudge, and the icon's square leaves the kite a
  dot in the middle of the tab.

The background is taken out by colour: every region of the background colour
that the dark outline or the open page encloses goes, and the pixels at its edge
are un-mixed from it, so the logo keeps a soft edge on any surface. The cream
spars across the kite are the background's colour too, but they lie between the
kite's coloured panels, and stay.

The output is committed; a build does not run this. Needs NumPy, SciPy and
Pillow, which are not BioKids dependencies: run it in a throwaway virtualenv.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "assets" / "brand" / "logo.jpg"
BRAND = ROOT / "src" / "biokids_core" / "data" / "static" / "brand"
SAIL_SHARE = 0.5
"""How much of the kite, from the top, is its sail: the string starts below."""
PANEL_ICONS = ROOT / "src" / "biokids_panel" / "static" / "icons"
SHELL_ICONS = ROOT / "src" / "biokids_shell" / "static" / "icons"

TILE = (251, 247, 242)  # --bg
DESKTOP = ROOT / "debian" / "parent" / "biokids-desktop.png"
SPLASH = ROOT / "iso" / "config" / "includes.chroot" / "usr" / "share" / "plymouth" / "themes"
SPLASH_WIDTH = 420
DESKTOP_SIZE = (1920, 1080)
GLOWS = (  # corner, colour: --bio-coral, --bio-amber, --bio-teal, --bio-indigo
    ((0.0, 0.0), (255, 107, 91)),
    ((1.0, 0.0), (255, 176, 32)),
    ((1.0, 1.0), (31, 182, 166)),
    ((0.0, 1.0), (75, 84, 200)),
)
LOGO_WIDTH = 960
KITE_HEIGHT = 512

BACKGROUND_TOLERANCE = 18
"""How far, in the largest channel, a pixel may be from the background colour
and still be background. The picture is a JPEG: its flat background is not."""

SPECK = 1500
"""An enclosed region smaller than this is noise inside a spar, not a counter
of a letter or the inside of the heart, which are all far larger."""

EDGE = 3
"""How many pixels into the drawing from the background are un-mixed from it."""


def transparent(rgb: np.ndarray) -> np.ndarray:
    """The picture as RGBA, its background taken out."""
    height, width, _ = rgb.shape
    pixels = rgb.astype(np.float64)
    corners = np.concatenate(
        [rgb[:16, :16], rgb[:16, -16:], rgb[-16:, :16], rgb[-16:, -16:]]
    ).reshape(-1, 3)
    background = np.median(corners, axis=0)

    near = np.abs(pixels - background).max(axis=2) <= BACKGROUND_TOLERANCE
    # JPEG noise breaks a spar into specks; closing joins them back up.
    # Padded first: closing erodes from the border of the array, and would leave
    # a frame of "not background" around the picture.
    near = ndimage.binary_closing(np.pad(near, 4, constant_values=True), iterations=2)[
        4:-4, 4:-4
    ]
    regions, count = ndimage.label(near)

    # A region of background colour bordered by colour is a spar; one that
    # touches the edge of the picture, or is bordered by the dark outline and
    # the letters, is background.
    saturation = pixels.max(axis=2) - pixels.min(axis=2)
    coloured = saturation > 60
    clear = np.zeros(count + 1, dtype=bool)
    edge_regions = np.unique(
        np.concatenate([regions[0], regions[-1], regions[:, 0], regions[:, -1]])
    )
    clear[edge_regions] = True
    for index, box in enumerate(ndimage.find_objects(regions), start=1):
        if clear[index] or box is None:
            continue
        box = tuple(slice(max(s.start - 4, 0), s.stop + 4) for s in box)
        own = regions[box] == index
        if own.sum() < SPECK:
            continue
        ring = ndimage.binary_dilation(own, iterations=3) & ~own
        clear[index] = coloured[box][ring].mean() < 0.25
    clear[0] = False
    gone = clear[regions]

    alpha = np.ones((height, width))
    alpha[gone] = 0.0
    band = ndimage.binary_dilation(gone, iterations=EDGE) & ~gone
    # Un-mix each edge pixel from the background: the least opacity at which
    # some colour, laid over the background, gives exactly this pixel.
    over = np.where(
        pixels > background,
        (pixels - background) / np.maximum(255.0 - background, 1.0),
        (background - pixels) / np.maximum(background, 1.0),
    ).max(axis=2)
    alpha[band] = np.clip(over[band] * 1.6, 0.0, 1.0)

    out = pixels.copy()
    safe = np.maximum(alpha, 1e-3)[..., None]
    unmixed = (pixels - (1.0 - alpha[..., None]) * background) / safe
    out[band] = np.clip(unmixed[band], 0, 255)
    rgba = np.dstack([out, alpha * 255.0]).round().astype(np.uint8)
    rgba[gone] = 0
    return rgba


def crop(image: Image.Image, margin: float = 0.03) -> Image.Image:
    left, top, right, bottom = image.getbbox() or (0, 0, *image.size)
    pad = round(max(right - left, bottom - top) * margin)
    return image.crop((left - pad, top - pad, right + pad, bottom + pad))


def kite_only(rgba: np.ndarray) -> np.ndarray:
    """The kite, its string and its bows, without the word or the small kite."""
    shapes, count = ndimage.label(rgba[..., 3] > 128, structure=np.ones((3, 3)))
    word_starts = rgba.shape[1] * 0.34
    keep = np.zeros(count + 1, dtype=bool)
    for index, centre in enumerate(
        ndimage.center_of_mass(np.ones(shapes.shape), shapes, range(1, count + 1)), start=1
    ):
        keep[index] = centre[1] < word_starts
    # The soft edge of each shape belongs to the shape next to it.
    nearest = ndimage.distance_transform_edt(
        shapes == 0, return_distances=False, return_indices=True
    )
    owner = shapes[nearest[0], nearest[1]]
    out = rgba.copy()
    out[~keep[owner]] = 0
    return out


def icon(kite: Image.Image, size: int) -> Image.Image:
    scale = 4
    big = size * scale
    tile = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(tile).rounded_rectangle(
        (big * 0.04, big * 0.04, big * 0.96, big * 0.96), radius=big * 0.22, fill=(*TILE, 255)
    )
    tile = tile.resize((size, size), Image.Resampling.LANCZOS)
    inner = round(size * 0.66)
    fitted = kite.copy()
    fitted.thumbnail((inner, inner), Image.Resampling.LANCZOS)
    x = (size - fitted.width) // 2
    y = (size - fitted.height) // 2
    tile.alpha_composite(fitted, (x, y))
    return tile


def favicon(kite: Image.Image, size: int) -> Image.Image:
    """The sail, which is the top of the kite down to where its string starts."""
    sail = crop(kite.crop((0, 0, kite.width, round(kite.height * SAIL_SHARE))), margin=0.02)
    side = max(sail.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.alpha_composite(sail, ((side - sail.width) // 2, (side - sail.height) // 2))
    return square.resize((size, size), Image.Resampling.LANCZOS)


def desktop(logo: Image.Image) -> Image.Image:
    """The logo on the warm off-white, each corner glowing in a brand colour."""
    width, height = DESKTOP_SIZE
    y, x = np.mgrid[0:height, 0:width].astype(np.float32)
    canvas = np.empty((height, width, 3), dtype=np.float32)
    canvas[:] = TILE
    reach = 0.42 * width
    for (cx, cy), colour in GLOWS:
        distance = np.hypot(x - cx * width, y - cy * height)
        weight = (0.22 * np.clip(1 - distance / reach, 0, 1) ** 2)[..., None]
        canvas = canvas * (1 - weight) + np.array(colour, dtype=np.float32) * weight
    image = Image.fromarray(canvas.round().astype(np.uint8)).convert("RGBA")
    fitted = logo.resize(
        (round(width * 0.3), round(logo.height * width * 0.3 / logo.width)),
        Image.Resampling.LANCZOS,
    )
    image.alpha_composite(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
    return image.convert("RGB")


def save(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, optimize=True)
    print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")


def main() -> int:
    rgba = transparent(np.asarray(Image.open(MASTER).convert("RGB")))
    logo = crop(Image.fromarray(rgba))
    kite = crop(Image.fromarray(kite_only(rgba)))

    width = LOGO_WIDTH
    save(
        logo.resize((width, round(logo.height * width / logo.width)), Image.Resampling.LANCZOS),
        BRAND / "logo.png",
    )
    height = KITE_HEIGHT
    save(
        kite.resize(
            (round(kite.width * height / kite.height), height), Image.Resampling.LANCZOS
        ),
        BRAND / "kite.png",
    )
    for target in (PANEL_ICONS, SHELL_ICONS):
        for size in (192, 512):
            save(icon(kite, size), target / f"icon-{size}.png")
    save(desktop(logo), DESKTOP)
    save(
        logo.resize(
            (SPLASH_WIDTH, round(logo.height * SPLASH_WIDTH / logo.width)),
            Image.Resampling.LANCZOS,
        ),
        SPLASH / "biokids" / "watermark.png",
    )
    save(favicon(kite, 64), BRAND / "favicon.png")
    ico = BRAND / "favicon.ico"
    favicon(kite, 48).save(ico, sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"wrote {ico.relative_to(ROOT)} ({ico.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
