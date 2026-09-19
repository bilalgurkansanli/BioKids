# biokids-voice

Zio and Sio. See `docs/VOICE.md` for the specification and `docs/DECISIONS.md`
D-91 … D-94 for why the feature has the shape it has.

## No voice model is bundled yet

**This package ships no Piper voice, in any language.** Bundling one is deferred
(T6.1), and it is deferred for a licensing reason rather than a scheduling one:
Piper's catalogue aggregates datasets under different terms and some of them are
not redistributable. `docs/VOICE.md` §3 is the rule.

Before any `.onnx` voice is added to this repository or to a `.deb`:

1. Record the voice, its upstream dataset and its licence in
   `assets/voices/LICENSES.md`.
2. Confirm the licence is MIT-compatible (D-30). A voice that may not be
   redistributed is downloaded at first run instead of being bundled.

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
