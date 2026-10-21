# VOICE.md

Spec for `biokids-voice` — **Zio** (mother) and **Sio** (father).

---

## 1. What they are

Two text-to-speech personas that speak short, warm lines at moments the parent chooses.
Zio uses a female voice, Sio a male voice. A small illustrated character card appears
on screen while a line plays.

**One-way only.** The child never speaks to Zio or Sio. They do not listen and they
do not chat. The assistant (D-165) is a separate tile that a parent switches on: Zio
reads its answers aloud through the `say` operation below, and its push-to-talk
button (D-166) is the only place in BioKids that uses the microphone.

A parent can record the lines themselves (D-174, section 4) and Zio or Sio speaks
them in that voice. That is the emotional core of the whole product: a 6-year-old
hearing "on dakika kaldı yavrum" in their actual mother's voice is a different
experience from hearing a robot.

---

## 2. Voice stack

```
line (event)
     │
     ├── the parent's own recording of it (D-174)         ← when they made one
     │
     ├── Piper  (ONNX, CPU, offline)                      ← one button in the panel (D-196)
     │
     ├── espeak-ng  (tr, in Debian main, offline)        ← the default in practice
     │
     └── the character card alone                          ← nothing could speak
```

> **Corrected 2026-09-20.** This diagram called Piper "always available". It is
> not available at all on a stock Debian 13 machine: `piper-tts` is not in the
> archive, and the one Turkish voice left in the Piper catalogue is licensed
> CC-BY-NC-SA, so BioKids may not ship it either. See
> `assets/voices/LICENSES.md` for what the catalogue actually contains and
> D-137 for the decision. espeak-ng is what speaks by default, and since D-196 a
> parent can fetch Piper and that voice with one button (section 3a).

### Why this stack

- **Piper** is MIT, fully offline, CPU-only, real-time on weak hardware, and has one
  Turkish voice. It is the best engine here and it is used whenever it is installed —
  but it is not in Debian and BioKids may not ship it, so it arrives only when a parent
  asks for it (section 3a).
- **espeak-ng** is the permanent fallback, and on most machines the only engine present.
  GPL-3+, in Debian main, speaks Turkish, carries its voices inside the package, needs
  no model file and no network. It sounds like a 1990s robot. That is the honest trade
  against a four-year-old being shown a card they cannot read yet. Where a family
  installs `mbrola-tr1` and `mbrola-tr2`, Zio and Sio speak with those instead (§3); they
  are a `Suggests`, because the voices are non-free and `mbrola` is in contrib.
- **A parent's own recordings** (D-174) replaced voice cloning (D-181). Cloning needed
  ninety seconds of audio, OpenVoice, a GPU and a month, and most of what a child would
  hear in it is seven sentences a parent can simply say. The cloud backend that was
  planned beside it (ElevenLabs) is gone too: it was the one place audio would have
  left the machine, against principle 1.

---

## 3. Stock voices

Two per language ship with the package:

| Persona | Locale | Source |
|---|---|---|
| Zio | tr_TR | the natural voice, lifted 1.2 times, once downloaded (D-196); else MBROLA `tr2` (a woman) when installed, else espeak-ng `-v tr+f3`, higher pitch |
| Sio | tr_TR | the natural voice as it is, once downloaded (D-196); else MBROLA `tr1` (a man) when installed, else espeak-ng `-v tr+m3`, lower pitch |
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
downloaded at first run either: espeak-ng is already there. The CC-BY-NC-SA voice is
downloaded only when a parent asks (section 3a).

---

## 3a. The natural voice (D-196)

espeak-ng is clear and robotic; Piper sounds like a person. A family may use Piper's
Turkish voice at home, and BioKids may not ship it, so the panel fetches it from its
authors on a parent's button:

1. **Ayarlar → Zio ve Sio'nun sesi** (`/voices`) says which voice speaks now — "robot
   sesi" or "doğal ses" — and offers **Doğal sesi indir (yaklaşık 90 MB)**, with one
   sentence on the licence (free for a family's personal, non-commercial use;
   downloaded from its authors, not shipped by BioKids) and a reminder that the
   parent's own recordings (section 4) are the most natural of all.
2. The button asks the daemon (`voice_setup`) to start `biokids-voice-setup.service`,
   which runs `biokids-admin voice-setup download` and nothing else: no capability,
   writable only `/var/lib/biokids-voices`, the only part of the voices with a network.
3. It fetches, each pinned by SHA-256 and size (`biokids_core/natural_voice.py`):

   | What | From | Licence | To |
   |---|---|---|---|
   | Piper 2023.11.14-2, `piper_linux_x86_64.tar.gz` (26 MB) | `github.com/rhasspy/piper` releases (archived) | MIT; its bundled espeak-ng fork is GPL-3, run as a separate process (D-30) | `/var/lib/biokids-voices/piper/` |
   | `tr_TR-dfki-medium.onnx`, `.onnx.json`, `MODEL_CARD` (63 MB) | `huggingface.co/rhasspy/piper-voices` at commit `c10ece1a` | dataset CC BY-NC-SA 4.0 (DFKI "OT", one male speaker) | `/var/lib/biokids-voices/models/piper/` |

   Each file is written beside its place under a temporary name, checked, and only
   then renamed; a mismatch is deleted and the unit fails. The archive is unpacked
   through tarfile's `data` filter and only under its own `piper/`. The voice is then
   hard-linked as `tr-sio.onnx` and `tr-zio.onnx`, each with its `.onnx.json`, because
   Piper reads a model's settings from its path plus `.json`. Piper goes in before
   the voice: a voice's cache key changes when its model appears, and the first line
   under the new key must be Piper's.
4. `/voice-natural` follows the download, refreshing itself, with the percentage
   read from the files on disk; `/voices` itself never refreshes, because a
   recording may be under way. A failed download says so and offers the button again.
5. The voice service runs `/var/lib/biokids-voices/piper/piper` (else a `piper` on
   PATH), looked up on every line, so a voice downloaded while a child is at the
   machine is heard from the next sentence.

**Zio.** The dfki voice is a man's and there is no Turkish woman in the catalogue.
Sio speaks it as it is. Zio speaks the same model *lifted*: Piper is asked for speech
1.2 times slower (`--length_scale`), and the WAV's sample rate is then raised 1.2
times, so pitch and formants rise about three semitones and the pace comes back to
where it was. No sample is changed, so there is none of the warble of a
pitch-shifting effect, and nothing else to install. It is a lighter, clearly
different person, not a woman; a family who wants a mother's voice records it
(section 4). The lift applies only while Zio's model is the very file Sio's is: a
voice of her own at `tr-zio.onnx` is spoken as it was recorded. Piper speaks at
`--length_scale 1.1` for both, a little slower for the youngest children (D-161).

Turkish only: a child whose language is English still hears espeak-ng.
`biokids-admin voice-setup status` prints the state as JSON, and
`biokids-admin voice-setup remove` deletes what the download installed. Purging
biokids-core with the family's data deletes it with the recordings.

---

## 4. A parent's own voice (D-174)

In the panel, under **Ayarlar → Zio ve Sio'nun sesi → Kendi sesiniz**:

1. Say which character the voice belongs to: the mother's (Zio is shown) or the
   father's (Sio).
2. For each of the seven lines - welcome, 10, 5 and 1 minute left, time is up, yes and
   not now to a request - read the suggested sentence (or one of your own) and press
   Record, then Stop.
3. The page records a mono 16-bit WAV in the browser itself and sends it to the panel,
   over localhost or the tailnet. The panel keeps it only if it is PCM, mono, 8-48 kHz
   and 0.5-15 seconds long.
4. It is stored in `/var/lib/biokids-voices/<locale>/<event>.wav` with the sentence in
   `<event>.txt`: world-readable, because the voice service is the child's process, and
   outside the family's 0700 data directory. Nothing in it ever leaves the machine.
5. Each line can be played back and deleted. A line nobody recorded is spoken by the
   computer, as before.

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
| `bedtime_10`, `bedtime_5`, `bedtime_1` | on | "Akşam oldu Elif. 10 dakika sonra yatma vakti." |
| `bedtime` | on | "İyi geceler, Elif. Yarın görüşürüz." |
| `request_approved` | on | "Annen izin verdi, şimdi girebilirsin." |

The `bedtime` lines are the evening's (D-214): when a day ends at night, the engine
asks for `bedtime_10` instead of `warn_10` and `bedtime` instead of `time_up`, with Zio
sleepy. A machine with no night line says the daytime one, and a parent's own
recording of the daytime line (D-174) is played for the night one too: their voice
matters more than the night's wording. `bedtime` is high priority, like `time_up`.

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
- **Priority:** `time_up`, its evening form `bedtime`, and `blocked` are high;
  everything else is normal.
- **Duck, don't mute.** Lower other audio to 30% via PipeWire for the duration, then
  restore. Muting a child's video abruptly is jarring.
- **Respect quiet hours.** A parent-set window where the character card appears silently.
- **Cache aggressively.** Key on `sha256(text + persona + voice id)`, store WAV
  in `/var/lib/biokids/voice-cache/`, LRU capped at 200 MB. Most lines are identical
  every day; after the first day almost nothing is synthesised live.
- **Fallback chain:** the parent's recording → Piper → espeak-ng → on-screen card only. Never
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
which is 0700 root: the cache is the child's own (`CacheDirectory=`), and a parent's
recordings and any Piper voices a family adds live in `/var/lib/biokids-voices`,
which the child's session may read. A directory it cannot read is treated as absent
(D-160).

The daemon never calls this socket: the launcher's relay forwards each SPEAK
(`biokids_shell/relay.py`), and keeps the reply, so the screen shows the sentence the
service actually chose and the character actually speaking.

Response is immediate (`queued`), with a follow-up event when playback finishes so the
shell can dismiss the card.

`{"op": "preview", "text": "...", "persona": "zio"}` speaks any short text at once,
for testing a voice.

`{"op": "say", "text": "...", "locale": "tr", "persona": "zio"}` reads out the
assistant's answer (D-165). An ordinary line in the queue, so a time warning still
goes first; cut after a whole sentence at 1,500 characters.

---

## 9. Testing

- Every stock voice renders every default line without error, in both locales.
- Queueing: three simultaneous requests play in priority order, none is lost, none
  overlaps.
- Cache hit rate above 90% on the second simulated day.
- A parent's recording is played instead of any synthesiser, with their sentence on
  the card; a recording that is not a short mono WAV is refused.
- Ducking restores the previous volume even if playback is killed.
- Card renders and dismisses correctly with audio disabled entirely.
- The natural voice (`tests/test_natural_voice.py`, never over the network): a file
  that does not match its pin is refused and deleted, half a voice is never
  installed, an archive reaching outside `piper/` is refused, each persona finds the
  voice under its own name, the downloaded Piper is found before PATH, Zio's line is
  rendered slower and lifted, and a Piper that arrives mid-session speaks the next line.
