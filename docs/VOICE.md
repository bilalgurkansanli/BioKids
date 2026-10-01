# VOICE.md

Spec for `biokids-voice` — **Zio** (mother) and **Sio** (father).

---

## 1. What they are

Two text-to-speech personas that speak short, warm lines at moments the parent chooses.
Zio uses a female voice, Sio a male voice. A small illustrated character card appears
on screen while a line plays.

**One-way only.** The child never speaks to Zio or Sio. There is no microphone in the
child session, no speech recognition, no conversation. A talking AI companion for a
5-year-old is a much larger safety surface than this project should carry, and the
feature is more charming without it.

Optionally, a family records their own voices and Zio and Sio speak in them. That is
the emotional core of the whole product: a 6-year-old hearing "on dakika kaldı yavrum"
in their actual mother's voice is a different experience from hearing a robot.

---

## 2. Voice stack

```
line text (i18n)
     │
     ├── Piper  (tr_TR / en_US, ONNX, CPU, offline)      ← best, rarely present
     │        │
     │        └── OpenVoice V2 tone-color conversion      ← only if a cloned profile exists
     │
     ├── espeak-ng  (tr, in Debian main, offline)        ← the default in practice
     │
     └── ElevenLabs API                                    ← optional, opt-in, cloud
```

> **Corrected 2026-09-20.** This diagram called Piper "always available". It is
> not available at all on a stock Debian 13 machine: `piper-tts` is not in the
> archive, and the one Turkish voice left in the Piper catalogue is licensed
> CC-BY-NC-SA, so BioKids may not ship it either. See
> `assets/voices/LICENSES.md` for what the catalogue actually contains and
> D-137 for the decision. espeak-ng is what speaks by default.

### Why this stack

- **Piper** is MIT, fully offline, CPU-only, real-time on weak hardware, and has decent
  Turkish voices. It is the best engine here and it is used whenever it is installed —
  but it is not in Debian and it cannot be the thing a family relies on arriving.
- **espeak-ng** is the permanent fallback, and on most machines the only engine present.
  GPL-3+, in Debian main, speaks Turkish, carries its voices inside the package, needs
  no model file and no network. It sounds like a 1990s robot. That is the honest trade
  against a four-year-old being shown a card they cannot read yet. Where a family
  installs `mbrola-tr1` and `mbrola-tr2`, Zio and Sio speak with those instead (§3); they
  are a `Suggests`, because the voices are non-free and `mbrola` is in contrib.
- **OpenVoice V2** is **MIT** and performs timbre conversion on existing TTS audio, so
  it layers onto Piper cleanly and inherits Piper's Turkish.
- **XTTS-v2 is deliberately not used.** It is the obvious choice for cloning and it
  works well, but its CPML license prohibits commercial use, which is incompatible with
  an MIT project that other people may build on. It may be documented as something a
  user can install themselves; it is never bundled and never a dependency.
- **ElevenLabs** is supported because it is genuinely better, and some parents will want
  it. It is the single feature in BioKids that sends data off the device, so it is off
  by default, requires the parent's own API key, and is labelled in the UI with an
  explicit warning. Selecting "strict offline" in the wizard removes the option entirely.

**Validate the Piper → OpenVoice V2 path on Turkish early in Phase 2a.** If quality is
poor, evaluate F5-TTS (MIT) next. Do not reach for a non-permissive model.

---

## 3. Stock voices

Two per language ship with the package:

| Persona | Locale | Source |
|---|---|---|
| Zio | tr_TR | MBROLA `tr2` (a woman) when installed, else espeak-ng `-v tr+f3`, higher pitch; a Piper voice when one is installed |
| Sio | tr_TR | MBROLA `tr1` (a man) when installed, else espeak-ng `-v tr+m3`, lower pitch; a Piper voice when one is installed |
| Zio | en_US | espeak-ng `-v en+f3` |
| Sio | en_US | espeak-ng `-v en+m3` |

espeak-ng has one Turkish voice and no per-character models, so without MBROLA its
female and male variants and the pitch carry the two apart, at 140 words a minute
(D-161). It is crude, and it is enough: the character card names who is speaking, and
the audio only has to sound like a different person.

**MBROLA** (D-161) is what makes it sound like people. `mbrola-tr1` and `mbrola-tr2` are
diphone recordings of a Turkish man and woman; the voices are in `non-free` and the
`mbrola` program in `contrib`, so a family installs them and BioKids only suggests them.
When both the program and a database are present, espeak-ng is asked for the phonemes
alone (`--pho`), three phonemes its Turkish table never mapped are renamed, and `mbrola`
speaks the result. espeak-ng's own MBROLA path is not used because of those three: it
turns the "l" of every "kaldı" into silence. Which voices are in use is decided when the
service starts, and espeak-ng's cache is emptied when that changes.

**Check each voice's license before bundling.** Piper's catalog aggregates datasets
with different terms; some are not redistributable. Record the chosen voice, its
dataset, and its license in `assets/voices/LICENSES.md`. **This was done, and the
answer was no** — both Turkish alternatives to the CC-BY-NC-SA one were withdrawn from
the catalogue at their contributors' request. Nothing is bundled, and nothing is
downloaded at first run either: espeak-ng is already there.

---

## 4. Voice cloning flow

In the panel, under **Sesler / Voices**:

1. Choose which persona to record: Zio or Sio.
2. Read 8 short sentences aloud. About 90 seconds total. The sentences are chosen to
   cover Turkish phoneme coverage and are shown one at a time with a large record
   button, a level meter, and per-sentence re-record.
3. Recording happens **in the browser** (MediaRecorder) and is uploaded to the panel
   over localhost or the tailnet. Nothing goes to a third party.
4. The panel runs the extraction locally, produces a speaker embedding, and stores it in
   `/var/lib/biokids/voices/<persona>/`.
5. A preview plays immediately: "Merhaba, ben Zio." The parent keeps it or re-records.

Requirements and guards:

- Minimum 60 s of usable audio; reject and explain if the level is too low or clipping.
- Raw recordings are deleted after the embedding is produced, unless the parent ticks
  "keep my recordings" (off by default).
- Only a parent authenticated with the **passphrase**, not just the PIN, can record or
  delete a voice. It is biometric-adjacent data and deserves the stronger gate.
- "Delete my voice" is a single button and it removes the embedding and any cached
  audio.
- The privacy policy says plainly: voice data never leaves the device.

---

## 5. Events

Each independently switchable per child, all off is a valid configuration. Default-on
events are marked.

| Event | Default | Example line (tr) |
|---|---|---|
| `welcome` | on | "Merhaba Elif, bugün bir saatin var." |
| `warn_10` | on | "On dakikan kaldı." |
| `warn_5` | on | "Beş dakika sonra kapatıyoruz, işini kaydet." |
| `warn_1` | on | "Bir dakika kaldı." |
| `time_up` | on | "Bugünlük bu kadar yavrum. Yarın görüşürüz." |
| `blocked` | on | "Bu sayfa senin için değil. İstersen annene sor." |
| `break_start` | off | "Biraz mola verelim, beş dakika." |
| `break_end` | off | "Mola bitti, devam edebilirsin." |
| `earned` | on | "Aferin! On beş dakika kazandın." |
| `bedtime` | off | "Geç oldu, artık yatma vakti." |
| `request_approved` | on | "Annen izin verdi, şimdi girebilirsin." |

Lines are **data, not code**: `/usr/share/biokids/voice/<locale>/<event>.txt`, one
variant per line, chosen at random so it does not feel mechanical. The parent can edit
every line in the panel, including writing their own. Placeholders: `{name}`,
`{minutes}`, `{app}`.

Persona selection per event: `zio` \| `sio` \| `alternate` \| `random`.
Default is `alternate`, so the child hears both.

---

## 6. Playback rules

- **Never interrupt.** If a line is already playing, queue. Maximum queue depth 3;
  drop the oldest low-priority line rather than talking over anything.
- **Priority:** `time_up` and `blocked` are high; everything else is normal.
- **Duck, don't mute.** Lower other audio to 30% via PipeWire for the duration, then
  restore. Muting a child's video abruptly is jarring.
- **Respect quiet hours.** A parent-set window where the character card appears silently.
- **Cache aggressively.** Key on `sha256(text + persona + voice_profile_id)`, store WAV
  in `/var/lib/biokids/voice-cache/`, LRU capped at 200 MB. Most lines are identical
  every day; after the first day almost nothing is synthesised live.
- **Fallback chain:** cloned → stock Piper → espeak-ng → on-screen banner only. Never
  silent failure with no visual. Each engine caches into its own directory, because the
  cache key names the sentence and the voice and never the engine — one shared directory
  would leave a family who installed Piper on Tuesday hearing Monday's robot for every
  line they had already heard.

---

## 7. Character cards

Voice always has a visual companion (D-93).

- Two hand-made **SVG** characters, inline in the shell, no external assets, themeable
  with CSS variables.
- **Zio:** rounded, warm — coral and amber palette.
  **Sio:** rounded, cool — teal and indigo palette.
  Same geometric language so they read as a pair.
- Four expressions each: `neutral`, `happy`, `concerned`, `sleepy`. Mapped from the
  event: warnings use `neutral`, `earned` uses `happy`, `blocked` uses `concerned`,
  `bedtime` uses `sleepy`.
- Animation: slide in from the bottom-right over 300 ms, a gentle idle bob while
  speaking, slide out 400 ms after the audio ends. Respect
  `prefers-reduced-motion`.
- The card shows the spoken line as text as well, so a deaf child, a muted machine, or
  a child in a noisy room gets the same information. This is not optional.

---

## 8. Interface

```python
# unix socket: /run/user/<uid>/biokids-voice.sock
{
  "op": "speak",
  "event": "warn_10",
  "persona": "auto",              # auto | zio | sio
  "vars": {"name": "Elif", "minutes": 10},
  "locale": "tr",
  "priority": "normal",           # normal | high
  "expression": "neutral"
}
```

The service runs as the child. Nothing it reads may live under `/var/lib/biokids`,
which is 0700 root: the cache is the child's own (`CacheDirectory=`), and the
configured cloned-voice and model directories, which are under it, are treated as
absent when they cannot be read (D-160). Before cloning ships they have to move
somewhere a child's session may read.

The daemon never calls this socket: the launcher's relay forwards each SPEAK
(`biokids_shell/relay.py`), and keeps the reply, so the screen shows the sentence the
service actually chose and the character actually speaking.

Response is immediate (`queued`), with a follow-up event when playback finishes so the
shell can dismiss the card.

`{"op": "preview", "text": "...", "persona": "zio"}` is used by the panel for the
voice-recording preview and for the line editor.

---

## 9. Testing

- Every stock voice renders every default line without error, in both locales.
- Queueing: three simultaneous requests play in priority order, none is lost, none
  overlaps.
- Cache hit rate above 90% on the second simulated day.
- Fallback: with the cloned model file deleted mid-run, the next line still speaks in
  the stock voice and logs a warning.
- Ducking restores the previous volume even if playback is killed.
- Card renders and dismisses correctly with audio disabled entirely.
