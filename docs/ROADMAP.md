# ROADMAP.md

Phases are ordered by risk, not by excitement. Each has an exit criterion that can be
demonstrated. Do not start a phase before the previous one meets its criterion.

There is no deadline. The effort estimates assume roughly one focused day per week.

---

## Phase 0 — BioGuard MVP  *(current)*

**Goal:** a parent can install a `.deb` on stock Debian 13, set a daily limit, and the
child's session enforces it with spoken warnings, manageable from a phone.

Scope:

- `biokids-core`: config, SQLite schema + Alembic, i18n scaffold, policy engine
- `biokids-daemon`: logind session watcher, BOOTTIME ledger, enforcer, IPC socket
- `biokids-panel`: setup wizard, Today dashboard, per-child quota editor, PIN auth,
  audit log
- `biokids-voice`: Piper with stock Turkish voices, warning lines only
- `biokids-shell`: minimal launcher — app tiles, countdown ring, banners
- one `.deb` per component, installable and removable cleanly
- Tailscale `serve` integration and documentation
- pytest suite for the ledger and the policy engine

Out of scope: filtering, AI, voice cloning, rewards, categories, ISO.

**Exit criterion.** On a fresh Debian 13 VM: `apt install biokids` → wizard → phone
reaches the panel over Tailscale → set a 20-minute quota → log in as the child → the
ring counts down → warnings at 10/5/1 spoken in Turkish → the machine shuts down →
the parent's audit log shows every event. Reboot; the counter resumes correctly.
Change the system clock forward 3 hours; nothing changes.

*Estimate: 6–8 sessions.*

---

## Phase 1 — Filtering and the full child experience

**Goal:** the machine is genuinely safe to hand to a 5-year-old.

- dnsmasq resolver, blocklist ingestion, SafeSearch and YouTube enforcement
- nftables DNS redirect and DoH blocking
- Chromium managed policies, per-child, allowlist and blocklist modes
- the Chromium extension and navigation reporting
- the block page, the "ask a parent" flow, the request queue in the panel
- the full launcher: age bands, catalog curation, character cards, brightness and
  volume tiles, shutdown tile
- LightDM avatar greeter and theming
- category limits, forced breaks, earned time
- history and reports, email digests

**Exit criterion.** In allowlist mode a 4-year-old profile can reach exactly four
approved sites and nothing else; typing another address shows the block page; pressing
"ask a parent" produces a request on the phone; approving it makes the site work within
five seconds. Thirty minutes in an educational app grants thirty bonus minutes of games.

*Estimate: 10–14 sessions.*

---

## Phase 2 — Voices and AI

Order matters: voices first, because they are self-contained and delightful; AI second,
because it is the part most likely to eat a month.

**2a — Zio & Sio**
- guided voice recording in the panel
- Piper → OpenVoice V2 cloning pipeline, validated on Turkish
- per-event speech toggles, editable voice lines
- character card animations and expressions
- optional ElevenLabs backend behind an explicit warning

**2b — AI supervision**
- trigger plumbing and the event contract
- layer 1 (lists) and layer 2 (OCR keyword) scoring
- llama.cpp packaging, model downloader, hardware tiering
- the VLM prompt, structured output, risk scoring, age-band thresholds
- evidence storage, retention pruning, the parent's alert view
- the supervision indicator and the child explanation screen

**Exit criterion.** A parent records 90 seconds of audio and the 10-minute warning is
spoken in a recognisable version of their own voice. Separately: opening an unapproved
site produces a scored alert on the parent's phone within 10 seconds, with the reason,
and no screenshot is written to disk for a benign page.

*Estimate: 12–16 sessions. The cloning pipeline and the Blackwell CUDA build are the
two things most likely to go long.*

---

## Phase 3 — The ISO

**3a — live image**
- `live-build` config, package lists, preseed, branding, Plymouth theme
- BioKids packages preinstalled from our own APT repo
- first-boot wizard
- boots in VirtualBox and from a USB stick on at least two real machines

**3b — installer**
- Calamares with a simplified, child-distro-appropriate flow
- partitioning, user creation wired into the BioKids wizard
- Secure Boot verification on real hardware

**Exit criterion.** A stranger downloads the ISO, writes it to a USB stick, installs it
on an old laptop, and has a working supervised machine in under 20 minutes without
reading documentation.

*Estimate: 10–15 sessions, most of it on real hardware.*

---

## Phase 4 — Hardening and polish

- per-application network control via nftables cgroups
- `biokids-harden` script with check/apply/undo
- multi-child scheduling conflicts, shared-machine edge cases
- accessibility: contrast, font scaling, dyslexia-friendly font option
- Kiwix offline content packaging
- documentation site, screenshots, a short demo video
- translation infrastructure so other languages can be contributed

---

## Explicitly deferred

Do not start these. They are recorded so they stop being distractions.

| Idea | Why later |
|---|---|
| ARM64 / Raspberry Pi image | doubles the test matrix; no GPU for the AI layer |
| Wayland support | rewrites screenshot, focus tracking and kiosk. v2 |
| Android companion app | the PWA covers it |
| Cloud sync / family accounts | directly contradicts the privacy promise |
| A chat interface for the child | huge safety surface; Zio/Sio stay one-way |
| Flatpak / Steam support | sandbox escape routes |
| School or multi-device management | different product |
| Ages 11–13 | different threat model; would require honesty about what we can't do |

---

## Milestones worth announcing publicly

1. First working `.deb` (end of Phase 0) — a post with a screen recording
2. Filtering + launcher (end of Phase 1) — the first real portfolio entry
3. Voice cloning demo (2a) — the most shareable moment in the project
4. First ISO (3a) — a Reddit r/linux post and a DistroWatch submission

Do not submit to DistroWatch before Phase 3b. They do not list unfinished projects and
a rejection is hard to undo.
