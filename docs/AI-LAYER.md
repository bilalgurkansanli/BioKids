# AI-LAYER.md

Spec for `biokids-ai`, the optional on-device supervision worker.

**Status: optional package, default off, `Suggests:` never `Depends:`.**
Everything in BioKids works without it. If this component is absent, broken, or
disabled, the rest of the system behaves exactly as if it had never existed.

---

## 1. What it is and is not

**Is:** a local process that, when something noteworthy happens, looks at the screen
once, produces a risk score, and tells the parent.

**Is not:** a monitor, a recorder, or a live feed. There is no "watch my child's
screen" feature and there will not be one. See `docs/SECURITY.md` §7.

---

## 2. Why the original design was changed

The initial idea was a screenshot every second, analysed by an AI. Three problems:

1. **Cost and heat.** One hour of use is 3,600 frames. Even locally that is minutes of
   continuous GPU time per child per day for almost no additional information, because
   consecutive frames are nearly identical.
2. **Latency.** Deciding "this is a bad site" after a queue of 3,600 frames drains is
   useless. The child has already seen it.
3. **Privacy.** Continuously capturing a child's screen creates a dataset that should
   not exist, even locally.

The replacement is **event-triggered, layered evaluation**: react to the moment
something changes, spend the cheapest possible resource first, and only reach for the
VLM when the cheap layers cannot decide.

---

## 3. Triggers

The daemon emits a trigger; the AI worker never polls.

| Trigger | Source |
|---|---|
| navigation to a domain this child has not visited before | Chromium extension |
| an application outside the child's catalog gains focus | focus watcher |
| a video goes fullscreen | window manager state |
| a file is opened from removable media | udev + daemon |
| the parent presses "Check now" | panel |

Rate limits, per child: at most 1 evaluation per 20 seconds, at most 40 per hour, at
most 200 per day. Exceeding a limit is itself an event the parent sees, because it
usually means something is looping.

Expected real volume: **10–30 VLM calls per hour of use**, most hours far fewer,
because layers 1 and 2 resolve the majority.

---

## 4. Pipeline

```
trigger
  │
  ├─ L1  lists            URL / app against allowlist, blocklist, known-good catalog
  │        decided? → emit score, stop.                             ~1 ms
  │
  ├─ L2  cheap signals    capture frame (RAM only) → downscale → OCR (tur+eng)
  │                       keyword lexicon scoring + page structure heuristics
  │        confident?  → emit score, stop.                          ~50–150 ms
  │
  └─ L3  VLM              full frame at 1024px long edge → structured prompt
                          → JSON risk assessment                    ~0.5–2 s on GPU
```

The frame lives in a `bytes` object. It is written to disk **only** if the final score
lands in the block band, and then only to `/var/lib/biokids/evidence/`, 0700.

### L2 details

- OCR: **RapidOCR** (ONNX, fast, no system deps) with Turkish and English. Fall back to
  `tesseract --oem 1 -l tur+eng` if RapidOCR is unavailable.
- The lexicon lives in `/usr/share/biokids/lexicon/{tr,en}.yaml`, weighted terms in
  categories. It ships with a conservative default list and is editable by the parent.
- Structural heuristics catch things OCR alone misses: a page that is mostly a single
  video element, a page with a login form on an unknown domain, a chat-shaped layout.
- L2 emits a score **and a confidence**. Only `confidence >= 0.8` short-circuits.

---

## 5. Model selection

Tiered by detected hardware, chosen at install time by `biokids-ai-setup`:

| VRAM | Model | Quant | Notes |
|---|---|---|---|
| ≥ 8 GB | Qwen2.5-VL-3B-Instruct | Q4_K_M GGUF | default target, best quality/size |
| 4–8 GB | Moondream2 | Q4 | fast, adequate for a coarse score |
| CPU only | SmolVLM-500M | Q8 | last resort; warn the parent quality is limited |

Runtime: **`llama.cpp` server**, one binary, CPU fallback, GGUF weights we can mirror.
vLLM was considered and rejected: too heavy for a `.deb`, no CPU path.

### Hardware note for this project

The development machine has an **RTX 5050 (Blackwell, compute capability 12.0)**. This
needs **CUDA 12.8 or newer** and a llama.cpp build with `sm_120` support. Verify this
before anything else in Phase 2b; if the build fights back, develop against Moondream2
on CPU and revisit. Do not let a CUDA toolchain problem stall the rest of the phase.

VirtualBox has no GPU passthrough, so the AI worker cannot be meaningfully tested in the
development VM. Run it on the Windows host or on the spare desktop.

Models are **not** bundled in the `.deb` or the ISO. `biokids-ai-setup` downloads them
once, verifies checksums, and stores them in `/var/lib/biokids/models/`. After that,
the service runs with `PrivateNetwork=yes`.

---

## 6. VLM contract

Temperature 0. Structured output enforced by a JSON grammar, not by asking politely.

**System prompt (English; the model reasons in English regardless of screen language):**

```
You assess whether what is on a child's computer screen is appropriate for a child of
the stated age. You are given one screenshot and metadata.

Return ONLY a JSON object:
{
  "score": <integer 0-100>,
  "category": "safe" | "ads" | "violence" | "sexual" | "scary" | "gambling" |
              "purchase" | "social_contact" | "misinformation" | "unknown",
  "reason_key": "<short snake_case key>",
  "reason_tr": "<one short sentence in Turkish for the parent>",
  "reason_en": "<one short sentence in English for the parent>",
  "confidence": <float 0-1>
}

Scoring guide:
  0-20   clearly appropriate for this age
  21-50  mildly unsuitable: heavy advertising, older-audience content, mild fright
  51-80  unsuitable: violence, gambling, adult themes, contact with strangers
  81-100 clearly harmful for a child

Be conservative about scoring high. A parent receiving false alarms will switch this
off, which protects the child less than a slightly permissive setting. When genuinely
uncertain, return a mid score with low confidence rather than guessing high.
Educational content, children's media, drawing tools and games are safe even when
visually chaotic.
```

**User message:** the image, plus
`{"child_age": 6, "url": "...", "app": "chromium", "page_title": "...", "locale": "tr"}`.

If the model returns invalid JSON, retry once with a stricter reminder; on a second
failure emit `score=0, confidence=0, reason_key="ai.unavailable"` and log it. A broken
AI layer must never punish a child.

---

## 7. Scoring and thresholds

The final score combines the layers:

```
final = max(L1_score, L2_score * L2_conf, L3_score * L3_conf)
```

`max`, not an average: any layer confidently finding something concerning should not be
diluted by the others.

Bands, by age (from `DECISIONS.md` D-79):

| Band | Log only | Notify parent | Block + notify |
|---|---|---|---|
| 4–7 | 0–29 | 30–64 | 65–100 |
| 8–10 | 0–44 | 45–79 | 80–100 |

Actions per band:

- **Log:** a row in `ai_alert`, visible in history. No notification, no evidence file.
- **Notify:** a panel alert and, if enabled, an email. The child is not interrupted.
  This is the band that must never punish, because it is the band where the model is
  most often wrong.
- **Block:** the page is closed and the block page is shown, the parent is notified
  immediately, an evidence frame is written. The 30-minute timeout applies **only if
  the parent enabled it** — default off.

The parent can mark any alert as "this was fine". That adds the domain to the child's
allowlist and stores a local calibration hint. We do not retrain anything; we just stop
asking about that site.

---

## 8. Evidence and retention

| | |
|---|---|
| Location | `/var/lib/biokids/evidence/<child>/<date>/<uuid>.webp` |
| Written when | final score is in the block band only |
| Format | WebP quality 70, long edge 1280 |
| Permissions | directory 0700, files 0600, owned by root |
| Retention | 7 days default, parent-settable 0–30, `biokids-prune.timer` daily |
| Access | parent panel, behind the PIN, watermarked with date and reason |
| Export | included in the data export as files plus a manifest |
| Delete | "Delete everything" wipes the directory |

A retention of 0 means "never write evidence": alerts still fire, with no image.
Some parents will want this, and they should.

---

## 9. The child's side

- A small **Zio/Sio badge** sits in the launcher corner. It pulses briefly when an
  evaluation runs. This is the supervision indicator from D-76.
- The first-login explanation screen (D-111), in the child's language, at their reading
  level. For 4–7 it is spoken by Zio or Sio with three pictures. For 8–10 it is four
  short sentences.
- When a page is blocked by the AI layer, the block page says the same thing as a
  filter block. The child does not need to know which subsystem decided, and framing it
  as "the computer thinks this isn't for you, ask a grown-up" keeps the
  ask-a-parent flow (D-65) available.

---

## 10. Performance budget

| Stage | Budget |
|---|---|
| Capture + downscale | < 40 ms |
| OCR | < 150 ms |
| VLM (GPU tier 1) | < 2 s |
| Trigger to parent notification | < 10 s end to end |
| Idle RAM (llama.cpp resident) | < 3 GB VRAM, < 500 MB system RAM |
| Idle CPU | < 1% |

If the machine cannot meet the idle budget, `biokids-ai-setup` should refuse to enable
the layer and say why, rather than installing something that makes the computer feel
broken.

---

## 11. Testing

- A fixture set of screenshots covering: EBA, TRT Çocuk, GCompris, YouTube Kids, a
  news site, a shopping site, an ad-heavy free-game portal, a login page, and a
  deliberately borderline case.
- Assert band placement, not exact scores. Scores drift between model versions; bands
  are the contract.
- **A false-positive budget is part of the test suite:** across the safe fixtures, no
  more than 1 in 20 may land above the notify threshold. If a model update breaks that,
  the model update fails CI.
- The scorer itself (`final = max(...)`, band mapping, rate limiting) is pure and
  unit-tested without a model.
