#!/usr/bin/env python3
"""Generate the PWA icons.

A tiny pure-Python PNG writer rather than Pillow: the icons are two flat shapes
on a rounded square, they change roughly never, and adding an image library to
the build for this would be silly. The output is committed, so a build does not
need to run this — it is here so the mark can be regenerated rather than being a
binary nobody can edit.

The mark: an indigo rounded square, with Zio's coral dot and Sio's teal dot
overlapping the way the two characters stand together. Maskable-safe — every
shape sits inside the inner 80% so a circular mask does not clip it.
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PANEL_ICONS = ROOT / "src" / "biokids_panel" / "static" / "icons"
SHELL_ICONS = ROOT / "src" / "biokids_shell" / "static" / "icons"

BG = (75, 84, 200)  # --bio-indigo
CORAL = (255, 107, 91)  # --bio-coral, Zio
TEAL = (31, 182, 166)  # --bio-teal, Sio
AMBER = (255, 176, 32)  # --bio-amber

Pixel = tuple[int, int, int]


def write_png(path: Path, size: int, pixels: list[list[Pixel]]) -> None:
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 (None) for every scanline
        for r, g, b in row:
            raw += bytes((r, g, b))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit truecolour
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


SUPERSAMPLE = 4
"""Rendered at this multiple and averaged down. Circles drawn a pixel at a time
have visibly jagged edges at 192px, and the icon is the first thing anyone sees."""


def render(size: int) -> list[list[Pixel]]:
    """Supersample, then average each block back down to one pixel."""
    big = _render_flat(size * SUPERSAMPLE)
    out: list[list[Pixel]] = []
    for y in range(size):
        row: list[Pixel] = []
        for x in range(size):
            totals = [0, 0, 0]
            for dy in range(SUPERSAMPLE):
                source = big[y * SUPERSAMPLE + dy]
                for dx in range(SUPERSAMPLE):
                    pixel = source[x * SUPERSAMPLE + dx]
                    totals[0] += pixel[0]
                    totals[1] += pixel[1]
                    totals[2] += pixel[2]
            count = SUPERSAMPLE * SUPERSAMPLE
            row.append((totals[0] // count, totals[1] // count, totals[2] // count))
        out.append(row)
    return out


def _render_flat(size: int) -> list[list[Pixel]]:
    radius = size * 0.22  # the rounded-square corner
    zio = (size * 0.38, size * 0.44, size * 0.17)
    sio = (size * 0.62, size * 0.44, size * 0.17)
    smile = (size * 0.5, size * 0.68, size * 0.20)

    rows: list[list[Pixel]] = []
    for y in range(size):
        row: list[Pixel] = []
        for x in range(size):
            colour: Pixel = (251, 247, 242)  # --bg, outside the rounded square
            if _inside_rounded_square(x, y, size, radius):
                colour = BG
                if _inside_circle(x, y, *smile) and y > smile[1] - 2:
                    colour = AMBER
                if _inside_circle(x, y, *zio):
                    colour = CORAL
                if _inside_circle(x, y, *sio):
                    colour = TEAL
            row.append(colour)
        rows.append(row)
    return rows


def _inside_rounded_square(x: int, y: int, size: int, radius: float) -> bool:
    margin = size * 0.04
    left, top = margin, margin
    right, bottom = size - margin, size - margin
    if not (left <= x <= right and top <= y <= bottom):
        return False
    cx = min(max(x, left + radius), right - radius)
    cy = min(max(y, top + radius), bottom - radius)
    return (x - cx) ** 2 + (y - cy) ** 2 <= radius**2


def _inside_circle(x: int, y: int, cx: float, cy: float, r: float) -> bool:
    return (x - cx) ** 2 + (y - cy) ** 2 <= r**2


def main() -> int:
    for target in (PANEL_ICONS, SHELL_ICONS):
        for size in (192, 512):
            path = target / f"icon-{size}.png"
            write_png(path, size, render(size))
            print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
