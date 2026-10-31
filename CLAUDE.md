# CLAUDE.md — BioKids

> Read this file first, every session. It is the project constitution.
> Detailed specs live in `docs/`. Read the one relevant to your task before writing code.

---

## 1. What this is

**BioKids** is a Debian-based Linux system for children aged **4–10**, with parental
controls, a child-friendly launcher, an optional on-device assistant ("Sor Bana"),
and two text-to-speech companions named **Zio** (mother voice) and **Sio** (father
voice).

Two deliverables, in this order:

1. **BioGuard** — a set of `.deb` packages that install on any Debian 13 / Ubuntu system.
   This is the product. It must work standalone.
2. **BioKids ISO** — Debian 13 + BioGuard preinstalled + a child-themed desktop.
   This is just a distribution format for #1.

Never work on the ISO before the packages work. See `docs/ROADMAP.md`.

**Repository:** `github.com/bilalgurkansanli/BioKids` (single monorepo)
**License:** MIT
**Author:** Bilal Gürkan Şanlı (Furkan), under the BioStudio label
**Site:** biokids.bilalgurkansanli.com, built from `website/` (D-256)

---

## 2. Non-negotiable principles

These override any other instruction in this repo. If a task conflicts with one of
them, stop and ask.

1. **Nothing leaves the device.** No telemetry, no analytics, no cloud inference, no
   crash reporting, no "anonymous usage stats". No screen is recorded: the one live
   view (D-253) is a parent's, asked for in the panel, shown to the child while it
   lasts and kept nowhere. What the assistant hears is turned into text in memory and
   dropped. The only outbound
   network calls BioKids itself makes are APT updates, DNS, the parent's own SMTP
   server, Tailscale once a parent sets up phone access, and the downloads a parent
   starts (the assistant's models, the natural voice, the Wikipedia packs). Every one
   of those is listed in `docs/SECURITY.md` §4 and must stay listed there.
2. **The child knows.** Supervision is visible to the child by default. No hidden
   screenshotting, no silent keylogging. A parent may reduce the indicator, but the
   feature is never covert by design. A parent watching the screen live is always
   shown on it ("👁 Annen ekranını görüyor"), and that sign cannot be turned off.
3. **Fail safe, not silent.** If the daemon dies, the filter can't load, or the AI
   worker crashes, the child session locks and the parent is notified. Never
   "continue unprotected".
4. **Never brick the machine.** Anything that can lock a parent out (GRUB password,
   recovery-mode removal, firewall lockdown) is opt-in, reversible, and has a
   documented recovery path.
5. **Offline first.** Every core feature works with the network cable unplugged.
   Cloud services are optional add-ons the parent explicitly enables.
6. **The parent panel must be understandable by a non-technical parent.** If a setting
   needs a paragraph of explanation, it belongs behind "Gelişmiş / Advanced".

---

## 3. Decisions that are already made

Do not re-litigate these. Full rationale in `docs/DECISIONS.md`.

| Area | Decision |
|---|---|
| Base | Debian 13 "Trixie", **stable**, amd64 only for v1 |
| Session | **X11 only.** Wayland is out of scope for v1 (screenshot + window tracking) |
| Desktop | XFCE 4.20 as the substrate; the child never sees it |
| Child UI | Custom fullscreen web launcher (`biokids-shell`) in Chromium kiosk |
| Browser | Chromium, managed via `/etc/chromium/policies/managed/` |
| Backend | Python 3.13, FastAPI, SQLite (WAL) |
| Frontend | Jinja2 + vanilla CSS, plain forms, `EventSource` for live counters. **No build step, no npm, no CDN, no framework** (D-32, revised in D-129) |
| Time enforcement | Own implementation on systemd-logind + `CLOCK_BOOTTIME`. Not timekpr |
| Filtering | Local `dnsmasq` + `nftables` + a managed Chromium extension. **No MITM proxy** |
| AI | A local assistant (Gemma via `llama.cpp`, Whisper, an offline Wikipedia), optional package, off until a parent turns it on for a child. No screen watcher (D-180) |
| TTS | Piper's natural voice, downloaded on the parent's request (D-196, D-223); espeak-ng otherwise; a parent's own recordings (D-174). No voice cloning (D-181) |
| Remote access | Tailscale (`tailscale serve`). No port forwarding, ever |
| Auth | 6-digit PIN, argon2 hashed, rate-limited |
| Languages | UI in Turkish + English. **Code, comments, commits, docs in English** |

---

## 4. Repository layout

```
biokids/
├── CLAUDE.md                  # this file
├── README.md
├── LICENSE                    # MIT
├── docs/                      # specs — read before touching the matching area
├── src/
│   ├── biokids_core/          # config, db models, policy engine, i18n
│   ├── biokids_daemon/        # time accounting + enforcement (root)
│   ├── biokids_panel/         # FastAPI parent panel (root, 127.0.0.1:7878)
│   ├── biokids_shell/         # child launcher server (child user, 127.0.0.1:7879+uid%100)
│   ├── biokids_voice/         # Zio & Sio TTS service
│   └── biokids_ai/            # optional local assistant (Sor Bana, English with Sio)
├── extension/                 # Chromium managed extension (URL reporting)
├── debian/                    # single source package -> multiple binary packages
├── iso/                       # live-build config (Phase 3 only)
├── website/                   # biokids.bilalgurkansanli.com, static, on Vercel (D-256, D-258)
├── assets/                    # icons, character art, themes, sounds
├── tests/
└── .github/workflows/
```

**One Debian source package, several binary packages:**
`biokids` (metapackage), `biokids-core`, `biokids-daemon`, `biokids-panel`,
`biokids-shell`, `biokids-voice`, `biokids-desktop` (metapackage), `biokids-ai`
(Suggests, never Depends), `biokids-archive-keyring`.

---

## 5. Runtime layout

| Path | Purpose |
|---|---|
| `/etc/biokids/config.toml` | machine-level config (ports, paths, feature flags) |
| `/var/lib/biokids/biokids.db` | SQLite, mode 0600, root-owned |
| `/var/lib/biokids-ai/models/` | the assistant's models and Wikipedia packs, downloaded from the panel |
| `/run/biokids/daemon.sock` | Unix socket, daemon IPC |
| `/usr/share/biokids/` | static assets, app catalog, blocklists |

| Service | User | Bind |
|---|---|---|
| `biokids-daemon.service` | root | unix socket only |
| `biokids-panel.service` | root | `127.0.0.1:7878` |
| `biokids-shell.service` | child (user unit) | `127.0.0.1:7879 + uid % 100` (D-188) |
| `biokids-voice.service` | child (user unit) | unix socket |
| `biokids-ai.service` | dedicated `biokids-ai` user | unix socket |

---

## 6. How to work in this repo

**Development happens in a VirtualBox VM running Debian 13, never on bare metal.**
Take a snapshot before anything that touches boot, PAM, nftables, or GRUB.

- Work in **large blocks**, not step-by-step confirmations. Finish a coherent unit
  (a module, a screen, a package), then report.
- **Ask before deciding** anything that changes architecture, adds a dependency with a
  non-permissive license, or affects the security model. Otherwise proceed.
- Never add a GPL-3 library to a package we ship code with. Separate-process usage
  (DBus, HTTP, CLI) is fine; document it.
- Python: `ruff` for lint and format, `mypy --strict` on `biokids_core` and
  `biokids_daemon`. Type hints required everywhere.
- Tests: `pytest`. Mandatory for time accounting, the policy engine, and risk scoring.
  Not required for templates or CSS.
- Commits: Conventional Commits, English, e.g. `feat(daemon): add boottime quota ledger`.
- Branches: `feat/...`, `fix/...`, `docs/...`. PR into `main`. `main` must always build.
- Every user-facing string goes through i18n from day one. Never hardcode Turkish or
  English in a template.

**Before writing code, read the matching doc:**

| Task | Read |
|---|---|
| anything structural | `docs/ARCHITECTURE.md` |
| time limits, rewards, breaks | `docs/ARCHITECTURE.md` §Time |
| filtering, hardening, threat model | `docs/SECURITY.md` |
| the AI watcher | `docs/AI-LAYER.md` |
| Zio & Sio | `docs/VOICE.md` |
| child launcher or parent panel | `docs/UX.md` |
| .deb, APT repo, ISO, CI | `docs/PACKAGING.md` |
| the website | `website/README.md` |
| "what should I do next" | `docs/ROADMAP.md`, then `docs/TASKS-PHASE0.md` |

---

## 7. Current phase

**Release: 2.0.0, the packages and the ISO (D-249 to D-255).** The packages are built,
tested on the Debian 13 VM and in the owner's own rounds of testing (D-219 to D-254);
the ISO was installed beside Windows on a real computer by the owner. The decisions are
in `docs/DECISIONS.md`, newest last.

**2.0.0 is released (2026-10-31, tag `v2.0.0`), and the repository is public.** The
packages are in the APT repository (`BioKids-apt`, `apt.biokids.bilalgurkansanli.com`,
signed with the archive key `F6A4 D652 … 5F17 E9B0`), the .debs and the signed
`SHA256SUMS` on the GitHub Release, the ISO on archive.org (`iso/published.toml`), and
the site's button downloads it (`website/site.toml`, `iso_url`). What is left:
1. SourceForge: the project waits on SourceForge's support (phone verification). Once
   it is open, the ISO goes there as well and `iso_url` moves to it (D-257).
2. The assistant's runtime, llama.cpp and whisper.cpp rebuilt for Trixie (D-165), is
   in the ISO but not yet in the APT repository, so `biokids-ai` installs from the ISO
   only, not on a Debian 13 that adds the repository.
3. The announcement.

The assistant is measured with `dev/assistant_eval.py` against a real model (D-248):
run it after any change to `src/biokids_ai/prompts.py`.
