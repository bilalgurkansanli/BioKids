"""BioKids' boot menu on an installed computer, and the installer's banner (D-255).

GRUB reads the theme before Linux starts, so a mistake in it shows on no test
machine's screen until a computer is started: a font name that matches no
font falls back to another, an unknown global property throws the whole theme
away, and a picture GRUB cannot read leaves the ground bare. These checks read
the files the way GRUB and the packaging will.
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
THEME = ROOT / "debian" / "grub" / "theme"
CFG = ROOT / "debian" / "grub" / "biokids.cfg"
RULES = ROOT / "debian" / "rules"
POSTINST = ROOT / "debian" / "biokids-desktop.postinst"
POSTRM = ROOT / "debian" / "biokids-desktop.postrm"
HOOK = ROOT / "iso" / "config" / "hooks" / "normal" / "9000-biokids-image.hook.chroot"
BANNERS = ROOT / "iso" / "config" / "includes.installer" / "usr" / "share" / "graphics"

BOOT_THEME = "/boot/grub/themes/biokids"
SHIPPED = "/usr/share/biokids/grub/theme"

GLOBAL_PROPERTIES = {
    # GRUB 2.12's theme_loader.c; any other name aborts loading the theme.
    "title-text", "title-font", "title-color",
    "message-font", "message-color", "message-bg-color",
    "desktop-image", "desktop-image-scale-method",
    "desktop-image-h-align", "desktop-image-v-align", "desktop-color",
    "terminal-font", "terminal-box", "terminal-border",
    "terminal-left", "terminal-top", "terminal-width", "terminal-height",
}  # fmt: skip

GRUB_OWN_FONTS = {"Unifont Regular 16"}
"""unicode.pf2, which Debian's grub.cfg loads before the theme's fonts."""


def theme_text() -> str:
    return (THEME / "theme.txt").read_text(encoding="utf-8")


def png_header(path: Path) -> tuple[int, int, int, int, int]:
    """Width, height, bit depth, colour type and interlace of a PNG."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height, depth, colour, _compression, _filter, interlace = struct.unpack(
        ">IIBBBBB", data[16:29]
    )
    return width, height, depth, colour, interlace


def fonts_made() -> set[str]:
    """The names grub-mkfont gives the fonts debian/rules makes:
    "<-n> Regular <-s>", since Fredoka's files are neither bold nor italic."""
    rules = RULES.read_text(encoding="utf-8")
    mkfont = re.compile(r'grub-mkfont -n ("[^"]+"|\S+) -s (\S+)')
    made: set[str] = set()
    for loop in re.finditer(r"for size in ([\d ]+); do(.*?)\bdone", rules, re.S):
        for name, _size in mkfont.findall(loop.group(2)):
            for size in loop.group(1).split():
                made.add(f"{name.strip(chr(34))} Regular {size}")
        rules = rules.replace(loop.group(0), "")
    for name, size in mkfont.findall(rules):
        made.add(f"{name.strip(chr(34))} Regular {size}")
    return made


def test_every_font_the_theme_names_is_made_by_the_build() -> None:
    named = set(re.findall(r'font\s*[:=]\s*"([^"]+)"', theme_text()))
    assert named, "the theme names its fonts"
    missing = named - fonts_made() - GRUB_OWN_FONTS
    assert not missing, f"GRUB would fall back to another font for {missing}"


def test_the_fonts_carry_turkish_letters() -> None:
    match = re.search(r"^GRUB_FONT_RANGE\s*:=\s*(\S+)", RULES.read_text(encoding="utf-8"), re.M)
    assert match
    ranges = [
        tuple(int(end, 16) for end in part.split("-")) for part in match.group(1).split(",")
    ]
    for letter in "çÇğĞıİöÖşŞüÜ":
        assert any(low <= ord(letter) <= high for low, high in ranges), letter


def test_the_theme_has_only_properties_grub_knows() -> None:
    names = {
        line.split(":", 1)[0].strip()
        for line in theme_text().splitlines()
        if re.match(r"^[a-z-]+\s*:", line)
    }
    assert names <= GLOBAL_PROPERTIES, names - GLOBAL_PROPERTIES
    assert 'title-text: ""' in theme_text(), "no 'GRUB Boot Menu' over the logo"


def test_every_picture_the_theme_names_is_shipped_and_readable_by_grub() -> None:
    text = theme_text()
    pictures = re.findall(r'desktop-image:\s*"([^"]+)"', text)
    for style in re.findall(r'pixmap_style\s*=\s*"([^"]+)"', text):
        pictures += [style.replace("*", side) for side in ("w", "c", "e")]
    assert pictures
    for name in pictures:
        assert (THEME / name).is_file(), name
    for path in THEME.glob("*.png"):
        _w, _h, depth, colour, interlace = png_header(path)
        # GRUB's PNG reader: 8 bits, RGB or RGBA, not interlaced.
        assert (depth, colour, interlace) in {(8, 2, 0), (8, 6, 0)}, path.name


def test_the_selection_is_drawn_for_the_items_height() -> None:
    match = re.search(r"item_height\s*=\s*(\d+)", theme_text())
    assert match
    for side in ("w", "c", "e"):
        assert png_header(THEME / f"select_{side}.png")[1] == int(match.group(1)), side


def test_grub_is_pointed_at_the_copy_in_boot_and_only_where_it_is() -> None:
    cfg = CFG.read_text(encoding="utf-8")
    assert f"if [ -f {BOOT_THEME}/theme.txt ]; then" in cfg
    assert f"GRUB_THEME={BOOT_THEME}/theme.txt" in cfg
    background = re.search(rf"GRUB_BACKGROUND={BOOT_THEME}/(\S+)", cfg)
    assert background and (THEME / background.group(1)).is_file()
    assert not re.search(r"^\s*GRUB_DISTRIBUTOR\s*=", cfg, re.M), (
        "Secure Boot's shim and the UEFI entry expect /EFI/debian (D-249)"
    )


def test_the_package_and_the_image_copy_the_theme_where_grub_is_pointed() -> None:
    postinst = POSTINST.read_text(encoding="utf-8")
    assert f"THEME={SHIPPED}\n" in postinst and f"BOOT_THEME={BOOT_THEME}\n" in postinst
    # Only when something changed (D-250), and never fatal.
    assert 'if [ -n "$regenerate" ]; then' in postinst
    assert "update-grub >/dev/null 2>&1 ||" in postinst
    postrm = POSTRM.read_text(encoding="utf-8")
    assert f"BOOT_THEME={BOOT_THEME}\n" in postrm and 'rm -rf "$BOOT_THEME"' in postrm
    hook = HOOK.read_text(encoding="utf-8")
    assert f"cp {SHIPPED}/* {BOOT_THEME}/" in hook
    install = (ROOT / "debian" / "biokids-desktop.install").read_text(encoding="utf-8")
    assert SHIPPED.lstrip("/") in install.split()


@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX shell")
@pytest.mark.parametrize("script", [CFG, POSTINST, POSTRM, HOOK], ids=lambda p: p.name)
def test_the_shell_files_parse(script: Path) -> None:
    sh = shutil.which("sh")
    assert sh
    subprocess.run([sh, "-n", str(script)], check=True)


def test_the_installer_banner_has_debians_size_under_both_names() -> None:
    for name in ("logo_debian.png", "logo_debian_dark.png"):
        width, height, depth, colour, _interlace = png_header(BANNERS / name)
        assert (width, height) == (800, 75), name
        assert (depth, colour) == (8, 2), name
