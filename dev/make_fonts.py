#!/usr/bin/env python3
"""Make the vendored Fredoka files from Google Fonts' variable font (D-243).

    python3 dev/make_fonts.py PATH/TO/Fredoka[wdth,wght].ttf

Fredoka is the logo's typeface, and every screen is set in it. It comes from
github.com/google/fonts (ofl/fredoka) as one variable font, version 2.001, which
has no ğ, Ğ, ş, Ş or İ: a Turkish word would be drawn half in Fredoka and half
in whatever font the browser found those five letters in. The font does carry
the marks they are made of (U+0306, U+0307, U+0327) and anchors that place them,
so this script

1. cuts the variable font into the two static weights the screens use - Regular
   (400) and Medium (500), at normal width; there is no bold (D-243),
2. adds the five letters as composites of a letter and its mark, placed by the
   font's own anchors, the way the font already builds ç and Ç, and kerned as
   the letter they are made from,
3. writes each as WOFF2 to `src/biokids_core/data/static/fonts/`.

The output is committed; a build does not run this. Needs fontTools and brotli,
which are not BioKids dependencies: run it in a throwaway virtualenv.
The font is OFL-1.1 with no Reserved Font Name, so a modified copy may keep it.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphComponent
from fontTools.varLib.instancer import instantiateVariableFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "src" / "biokids_core" / "data" / "static" / "fonts"

WEIGHTS = {"Regular": 400, "Medium": 500}

TURKISH = {
    # code point: (new glyph, base, mark, the font's own letter with that mark)
    0x011E: ("Gbreve", "G", "uni0306", None),
    0x011F: ("gbreve", "g", "uni0306", None),
    0x015E: ("Scedilla", "S", "uni0327", "Ccedilla"),
    0x015F: ("scedilla", "s", "uni0327", "ccedilla"),
    0x0130: ("Idotaccent", "I", "uni0307", None),
}
"""The cedilla has no anchors in this font; ç and Ç place it by hand. ş and Ş
take its height from them and are centred under the S, whose middle, unlike
an open C's, is the middle of its outline."""


def mark_offset(font: TTFont, base: str, mark: str, like: str | None) -> tuple[float, float]:
    """Where the font's own anchors put `mark` on `base`, as a component offset."""
    if like is not None:
        glyf: Any = font["glyf"]
        height = next(c.y for c in glyf[like].components if c.glyphName == mark)
        for name in (base, mark):
            glyf[name].recalcBounds(glyf)
        middle = (glyf[base].xMin + glyf[base].xMax) / 2
        return (middle - (glyf[mark].xMin + glyf[mark].xMax) / 2, height)
    for lookup in font["GPOS"].table.LookupList.Lookup:
        for sub in lookup.SubTable:
            if lookup.LookupType == 9:
                sub = sub.ExtSubTable
            if sub.LookupType != 4:
                continue
            marks = sub.MarkCoverage.glyphs
            bases = sub.BaseCoverage.glyphs
            if mark not in marks or base not in bases:
                continue
            record = sub.MarkArray.MarkRecord[marks.index(mark)]
            base_anchor = sub.BaseArray.BaseRecord[bases.index(base)].BaseAnchor[record.Class]
            if base_anchor is None:
                continue
            return (
                base_anchor.XCoordinate - record.MarkAnchor.XCoordinate,
                base_anchor.YCoordinate - record.MarkAnchor.YCoordinate,
            )
    raise SystemExit(f"no anchor places {mark} on {base}")


def kern_like(font: TTFont, new: str, base: str) -> None:
    """Give `new` the kerning classes of `base` in every class-based pair table."""
    for lookup in font["GPOS"].table.LookupList.Lookup:
        for sub in lookup.SubTable:
            if lookup.LookupType == 9:
                sub = sub.ExtSubTable
            if sub.LookupType != 2 or sub.Format != 2:
                continue
            if base in sub.Coverage.glyphs:
                sub.Coverage.glyphs.append(new)
            for class_def in (sub.ClassDef1, sub.ClassDef2):
                if class_def is not None and base in class_def.classDefs:
                    class_def.classDefs[new] = class_def.classDefs[base]


def add_turkish(font: TTFont) -> None:
    glyf: Any = font["glyf"]
    hmtx: Any = font["hmtx"]
    order = font.getGlyphOrder()
    for code, (name, base, mark, like) in TURKISH.items():
        x, y = mark_offset(font, base, mark, like)
        glyph = Glyph()
        glyph.numberOfContours = -1
        glyph.components = []
        for component_name, dx, dy in ((base, 0, 0), (mark, x, y)):
            component = GlyphComponent()
            component.glyphName = component_name
            component.x, component.y = dx, dy
            component.flags = 0x4  # ROUND_XY_TO_GRID
            glyph.components.append(component)
        order.append(name)
        glyf.glyphs[name] = glyph
        glyph.recalcBounds(glyf)
        hmtx.metrics[name] = (hmtx.metrics[base][0], glyph.xMin)
        for table in font["cmap"].tables:
            if table.isUnicode():
                table.cmap[code] = name
        if "GDEF" in font and font["GDEF"].table.GlyphClassDef is not None:
            font["GDEF"].table.GlyphClassDef.classDefs[name] = 1  # a base glyph
        kern_like(font, name, base)
        print(f"  {name}: {base} + {mark} at ({x}, {y})")
    font.setGlyphOrder(order)
    glyf.glyphOrder = order


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    source = Path(sys.argv[1])
    OUT.mkdir(parents=True, exist_ok=True)
    for style, weight in WEIGHTS.items():
        font = instantiateVariableFont(TTFont(source), {"wght": weight, "wdth": 100})
        print(f"Fredoka {style}")
        add_turkish(font)
        for table in ("hdmx", "LTSH", "VDMX"):
            if table in font:
                del font[table]
        names: Any = font["name"]
        names.setName(f"Fredoka {style}", 4, 3, 1, 0x409)
        names.setName(f"Fredoka-{style}", 6, 3, 1, 0x409)
        names.setName(
            "Version 2.001; BioKids: static instance, Turkish ğĞşŞİ added", 5, 3, 1, 0x409
        )
        font["OS/2"].usWeightClass = weight
        font.flavor = "woff2"
        path = OUT / f"Fredoka-{style}.woff2"
        font.save(path)
        print(f"  -> {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
