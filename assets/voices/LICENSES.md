# Voice models

**No voice model is bundled with BioKids, and none may be added to this
directory until its entry below is filled in.** `debian/rules` installs whatever
is here to `/usr/share/biokids/voices/`, so an unchecked file in this directory
is an unchecked file in everyone's `.deb`.

That is deliberate. `docs/VOICE.md` §3 is explicit: Piper's catalogue aggregates
datasets with different terms, and some of them are not redistributable. T6.1
says to record the licence **before** bundling, and until that is done for a
Turkish voice, BioKids ships without one.

## What the catalogue actually has, checked 2026-09-20

T6.1 asks for "one Turkish female plus one Turkish male voice". Neither exists
in a form BioKids may ship, and this is not a matter of nobody having looked:

| Voice | State | Source |
|---|---|---|
| `tr_TR-dfki-medium` | Dataset is **CC-BY-NC-SA-4.0** — non-commercial, so excluded by the rule below | its `MODEL_CARD`, which names `github.com/marytts/dfki-ot-data/` |
| `tr_TR-fahrettin-*` | **Withdrawn.** "Remove fahrettin and fettah voices at request of contributors" | `rhasspy/piper-voices` commit `b145f2a` |
| `tr_TR-fettah-*` | **Withdrawn**, same commit | as above |

A voice whose contributor asked for it to be taken down is not one to keep a
copy of, whatever its file says.

And Piper itself is not packaged: **`piper-tts` is not in Debian 13.** The only
`piper` in trixie is a GTK utility for configuring gaming mice. So the old
`Recommends: piper-tts` named nothing at all, and on a fresh machine the
fallback chain went straight from "no Piper" to silence.

## So espeak-ng speaks, and Piper is the upgrade

`biokids-voice` now recommends **espeak-ng**: in Debian main, GPL-3+, speaks
Turkish, carries its voices inside the package, needs no model file and no
network. Personas are distinguished by pitch, which is crude and enough.

It sounds like a 1990s robot. That is the honest trade: a four-year-old who
hears "beş dakika kaldı" has been warned, and a four-year-old shown a card they
cannot read yet has not. `PiperSynth` is still tried first and still wins
whenever a family installs Piper themselves.

`mbrola-tr1` and `mbrola-tr2` improve espeak-ng's Turkish considerably and are
**Suggests** rather than Recommends, because they are in `non-free` and BioKids
must install cleanly from `main` alone.

When no engine is available at all the chain ends at the character card, which
always carries the spoken line as text (VOICE.md §7). A machine with no TTS
warns the child on screen. It does not fail silently and it does not pretend.

**Revisit this** if a Turkish Piper voice with a redistributable dataset
appears, or if `piper-tts` enters Debian. Bundling one is still the better
outcome; there is simply nothing to bundle today.

## The table

One row per bundled model. Every column must be filled in before the file lands.

| Persona | Locale | Model file | Dataset | Licence | Redistributable | Checked by | Date |
|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — |

## How to add one

1. Pick a voice from the Piper catalogue and find the **dataset** it was trained
   on, not just the model's own repository. The model file and the dataset can
   have different terms, and the dataset's are the ones that bind.
2. Confirm the licence permits redistribution in a package. MIT, BSD, Apache-2.0,
   CC0, CC-BY and CC-BY-SA are fine; CC-BY-NC, "research only" and anything
   silent on redistribution are not.
3. Add the row above, and the licence text to `debian/copyright` as a separate
   `Files: usr/share/biokids/voices/<name>/*` stanza.
4. Put the `.onnx` and `.onnx.json` in this directory.
5. If the answer at step 2 is no, do **not** add it here. Add it to
   `biokids-voice`'s first-run download instead, where the user fetches it
   themselves under the licence they accept — the same pattern
   `biokids-ai-setup` uses for models (`docs/AI-LAYER.md` §5). That path is a
   new outbound network call and so needs a row in `docs/SECURITY.md` §4 and a
   decision entry, which is exactly the friction it should have.

## Piper voice naming

Piper files are `<locale>-<voice>-<quality>.onnx` with a `.onnx.json` beside
them. Both are needed; the JSON carries the sample rate and the phoneme map, and
a model without it will not load.
