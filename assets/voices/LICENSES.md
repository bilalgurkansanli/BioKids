# Voice models

**No voice model is bundled with BioKids, and none may be added to this
directory until its entry below is filled in.** `debian/rules` installs whatever
is here to `/usr/share/biokids/voices/`, so an unchecked file in this directory
is an unchecked file in everyone's `.deb`.

That is deliberate. `docs/VOICE.md` §3 is explicit: Piper's catalogue aggregates
datasets with different terms, and some of them are not redistributable. T6.1
says to record the licence **before** bundling, and until that is done for a
Turkish voice, BioKids ships without one.

## What happens meanwhile

`biokids-voice` works and is not blocked on this:

* it renders every line, falls back correctly, queues correctly and caches
  correctly, against whatever Piper voices the machine has;
* `biokids-voice` **Recommends** `piper-tts`, so a normal `apt install` pulls in
  Piper and its packaged voices;
* when no voice is available at all, the fallback chain ends at the character
  card, which always carries the spoken line as text (VOICE.md §7). The child
  reads the warning instead of hearing it.

So a machine with no voice model warns the child on screen. It does not fail
silently and it does not pretend.

**This is the one part of the Phase 0 exit criterion that is not met yet.** That
criterion asks for warnings *spoken* in Turkish, and speaking them needs a
Turkish voice whose licence has been checked. It is a packaging task, not a code
one.

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
