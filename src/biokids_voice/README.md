# biokids-voice

Zio and Sio. See `docs/VOICE.md` for the specification and `docs/DECISIONS.md`
D-91 … D-94 for why the feature has the shape it has.

## No voice model is bundled, and the question is settled

**This package ships no Piper voice, in any language** — not because the
decision is pending, but because it was made. `docs/VOICE.md` §3 required each
licence to be checked before bundling; that was done on 2026-09-20 and the answer
was that there is nothing to bundle:

* `tr_TR-dfki-medium` — the only Turkish voice left in the catalogue — is trained
  on a dataset licensed CC-BY-NC-SA-4.0, which is not redistributable here.
* `tr_TR-fahrettin-*` and `tr_TR-fettah-*` were **removed from the catalogue at
  their contributors' request**. A voice whose contributor asked for it to be
  taken down is not one to keep a copy of.
* `piper-tts` is not packaged in Debian 13 at all.

So **espeak-ng** speaks: Debian main, GPL-3+, Turkish, voices inside the package,
no model file and no network. It sounds like a 1990s robot, which is the honest
trade against a four-year-old being shown a card they cannot read yet. Piper is
still tried first and still wins wherever a family installs it. See D-137 and
D-145.

If a Turkish Piper voice with a redistributable dataset ever appears, bundling it
is still the better outcome. Before any `.onnx` is added to this repository:

1. Record the voice, its upstream dataset and its licence in
   `assets/voices/LICENSES.md`.
2. Confirm the licence is MIT-compatible (D-30). A voice that may not be
   redistributed is not bundled.

Until then the fallback chain ends where VOICE.md §6 says it should: the
character card appears with the sentence written on it, and nothing is spoken.
That is a supported state, not a broken one — `VoiceService.status()` reports
it, and the log says why on every line.

## Where things are at runtime

| Path | What |
|---|---|
| `/usr/share/biokids/voice/<locale>/<event>.txt` | line variants, editable |
| `/var/lib/biokids/voices/<persona>/<locale>.onnx` | a family's cloned voice |
| `/var/lib/biokids/models/piper/<locale>-<persona>.onnx` | the stock voice |
| `/var/lib/biokids/voice-cache/<sha256>.wav` | rendered audio, LRU capped |
| `/var/lib/biokids/voice-cache/silent/` | the stub backend's silence, kept apart |
| `/run/user/<uid>/biokids-voice.sock` | the service socket, mode 0600 |

The service runs as the **child** user, so the cache directory has to be
writable by that user — the packaging owns that, not this code. It deliberately
cannot read `/var/lib/biokids/biokids.db`, which is root-only; parent-edited
lines reach it through the installed text files, and the panel is the only
caller that passes a database session to `LineBook`.

## Running it without a sound card

    python -m biokids_voice --no-audio --log-level DEBUG

`--no-audio` makes the service behave exactly as a machine with no playback
command does: the card appears with the sentence written on it, the finished
event says `banner`, and nothing is synthesised at all — rendering a WAV that no
command exists to play would spend a child's laptop on silence.

When `piper` is the only thing missing, `StubSynth` writes silent WAVs of roughly
the right length instead, so the queue, the cache, the card and the finished
notifications all behave as they will on a machine that can speak. That silence
lives in `voice-cache/silent/` rather than beside real audio: the cache key is
the sentence, the persona and the voice profile (VOICE.md §6) and says nothing
about which engine produced the file, so a family who install Piper afterwards
would otherwise go on hearing yesterday's nothing. The test suite needs neither
Piper nor an audio device.
