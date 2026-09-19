# Vendored fonts

`tokens.css` declares `@font-face` rules for Inter, Fredoka and OpenDyslexic and
serves them from `/static/fonts/`. **They are not in the repository.**

Fonts are never loaded from Google Fonts: offline is a hard requirement, and a
font request is a tracking request (UX.md §1). So they have to be vendored — but
vendoring a font means shipping it, and shipping it means checking its licence
and recording it, which has not been done yet.

Until then the panel and the launcher fall back to the system stack declared
alongside each rule, which is why every `@font-face` sits behind
`font-display: swap` and a full fallback list. Nothing is invisible and nothing
breaks; the type is merely the platform's rather than ours.

## To vendor them

1. Download the WOFF2 files:
   - Inter (SIL Open Font License 1.1) — `Inter-Regular.woff2`, `Inter-SemiBold.woff2`
   - Fredoka (SIL Open Font License 1.1) — `Fredoka-SemiBold.woff2`
   - OpenDyslexic (SIL Open Font License 1.1) — `OpenDyslexic-Regular.woff2`
2. Put them in this directory.
3. Record each font, its version and its licence in `LICENSES.md` beside them.
4. `debian/rules` installs this directory to `/usr/share/biokids/fonts/`, which
   both static mounts serve as `/static/fonts/`.

All three are OFL-1.1, which is compatible with an MIT project as long as the
licence text ships with the fonts. Do not add a font that is not.
