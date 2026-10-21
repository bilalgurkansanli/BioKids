"""How a screen is drawn for the person in front of it (D-217).

Three choices: the size of the text, high contrast, and - on a child's screen
only - a font made for children who find reading hard. Two places set them:

* each parent for themselves, under Ayarlar → Görünüm. Stored on their own
  `parent` row rather than in the browser, so the choice made at the computer
  is there on the phone too;
* a parent for each child, under Çocuklar → Düzenle → Ekran, on the `child`
  row. The daemon hands it to the launcher with the rest of the child's state;
  the launcher cannot read the database itself.

Either way a page gets three attributes on `<html>` - `data-text-size`,
`data-contrast` and `data-font` - and `tokens.css` turns them into token
overrides. Nothing else in a page knows about them, which is what keeps the
defaults exactly what they were.

The reading font is OpenDyslexic from Debian's `fonts-opendyslexic`, which
biokids-desktop recommends. BioKids does not ship it: the browser finds it among
the machine's fonts by name, and where it is not installed the page keeps its
usual font. The panel says so beside the switch rather than pretending.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

TEXT_SIZES: Final = ("normal", "large", "x-large")
"""Normal, Büyük, Çok büyük: 100, 125 and 150 per cent (`tokens.css`). Three
named steps rather than a slider: a parent picks one in a second and a child's
screen can be measured at each of them."""

DEFAULT_TEXT_SIZE: Final = "normal"

READING_FONT_DIRS: Final = (
    Path("/usr/share/fonts/opentype/opendyslexic"),
    Path("/usr/share/fonts/truetype/opendyslexic"),
)
"""Where `fonts-opendyslexic` puts the font (trixie: `opentype/`). Checked so the
panel can say whether the switch will change anything on this machine."""


def text_size(value: object) -> str:
    """One of `TEXT_SIZES`; anything else - a hand-edited row, a forged form - is
    the normal size, never an error."""
    return value if isinstance(value, str) and value in TEXT_SIZES else DEFAULT_TEXT_SIZE


@dataclass(frozen=True, slots=True)
class Appearance:
    """One person's choices, already checked."""

    text_size: str = DEFAULT_TEXT_SIZE
    high_contrast: bool = False
    reading_font: bool = False

    @classmethod
    def of(
        cls, text_size_value: object, high_contrast: object, reading_font: object = False
    ) -> Appearance:
        return cls(
            text_size=text_size(text_size_value),
            high_contrast=high_contrast is True or high_contrast == 1,
            reading_font=reading_font is True or reading_font == 1,
        )

    @classmethod
    def from_state(cls, raw: object) -> Appearance:
        """From what the daemon sent the launcher. Checked again here: the page
        writes these into attributes, and only known values may reach them."""
        if not isinstance(raw, Mapping):
            return cls()
        return cls.of(raw.get("text_size"), raw.get("high_contrast"), raw.get("reading_font"))

    @property
    def contrast(self) -> str:
        """`data-contrast` on `<html>`."""
        return "high" if self.high_contrast else "normal"

    @property
    def font(self) -> str:
        """`data-font` on `<html>`: `dyslexic` switches the font stacks in tokens.css."""
        return "dyslexic" if self.reading_font else "default"

    @property
    def is_default(self) -> bool:
        return self == Appearance()

    def as_dict(self) -> dict[str, object]:
        """What the daemon puts in the launcher's state."""
        return {
            "text_size": self.text_size,
            "high_contrast": self.high_contrast,
            "reading_font": self.reading_font,
        }


def reading_font_installed(dirs: Iterable[Path] = READING_FONT_DIRS) -> bool:
    """Whether OpenDyslexic is among this machine's fonts."""
    for directory in dirs:
        try:
            if any(directory.glob("OpenDyslexic-Regular.*")):
                return True
        except OSError:
            continue
    return False
