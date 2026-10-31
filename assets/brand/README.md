# The logo and the typeface

## The logo

`logo.jpg` is the master: the kite, its string ending in a heart, and "BioKids"
beside it, on the warm off-white of `--bg` (D-243). It was made as a picture
rather than drawn as vectors, so every other logo file is cut from it by
[`dev/make_icons.py`](../../dev/make_icons.py), and the output is committed:

| File | Where it is used |
|---|---|
| `src/biokids_core/data/static/brand/logo.png` | the whole logo, transparent: over the panel's login, setup and help pages before a parent signs in, the panel's offline page, the login screen's background (`dev/make_greeter_art.py`), the README |
| `src/biokids_core/data/static/brand/kite.png` | the kite alone, transparent: beside "BioKids" in the panel's top bar |
| `src/biokids_{panel,shell}/static/icons/icon-{192,512}.png` | the kite on a rounded `--bg` square: the phone's home-screen icon, the browser tab, and the desktop icon (`debian/rules` installs the panel's into hicolor) |

To change the logo, replace `logo.jpg` and run the two scripts again.

`logo-card.png` is the logo on a white card, for the top of the READMEs: GitHub's
dark theme would otherwise put the dark lettering on a dark page.

`social-preview.png` (1280 x 640) is the card GitHub shows where the repository is
linked: the logo, one line, and the English launcher. GitHub does not read it from
the repository; it is uploaded in the repository's settings (General, Social
preview), which only a public repository shows.

The READMEs' pictures are in `assets/screenshots/en/` and `assets/screenshots/tr/`,
one set per language, so neither README shows the other's language. They were taken
from the real panel and launcher over `dev/seed.py`'s family with a stand-in
daemon, through Chrome's DevTools protocol, so a page could be used first (the live
view started, a question asked) and the phone is a phone-sized viewport. Two things
in them are not what a BioKids computer shows: the assistant's answer was written
for the picture, since the stand-in has no model, and the emoji are those of the
Windows machine that took them, where a BioKids computer draws Noto's.

## The typeface

Every screen, the parent's and the child's, is set in **Fredoka**, the logo's
typeface - in Regular and Medium only. The logo is bold; the screens are not
(D-243). `tokens.css` gives every heavier weight a stylesheet asks for to Medium.

The files are in `src/biokids_core/data/static/fonts/`, beside `OFL.txt`, and are
served with `tokens.css` from `/static/shared/`. Never loaded from Google Fonts:
offline is a hard requirement, and a font request is a tracking request (UX.md §1).

Fredoka 2.001 from github.com/google/fonts (`ofl/fredoka`) is one variable font
with no ğ, Ğ, ş, Ş or İ. [`dev/make_fonts.py`](../../dev/make_fonts.py) cuts it into
the two static weights and adds those five letters from the font's own letters
and marks. Fredoka is SIL OFL 1.1 with no Reserved Font Name, which allows a
modified copy under the same name; `debian/copyright` records it.

## OpenDyslexic is not vendored

The reading font a parent can choose for a child (D-217) is OpenDyslexic from
Debian's own `fonts-opendyslexic` (in trixie main), which `biokids-desktop`
recommends. It is installed system-wide and the child's browser finds it by
name, so `tokens.css` has no `@font-face` for it: a rule pointing at a file that
is not there would hide the installed font behind it. Where the package is
missing, the font stacks go on to Fredoka, and the panel says the font is not
installed.
