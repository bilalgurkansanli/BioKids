# CLAUDE.md — BioKids

> Read this file first, every session. It is the project constitution.
> Detailed specs live in `docs/`. Read the one relevant to your task before writing code.

---

## 1. What this is

**BioKids** is a Debian-based Linux system for children aged **4–10**, with parental
controls, a child-friendly launcher, optional on-device AI supervision, and two
text-to-speech companions named **Zio** (mother voice) and **Sio** (father voice).

Two deliverables, in this order:

1. **BioGuard** — a set of `.deb` packages that install on any Debian 13 / Ubuntu system.
   This is the product. It must work standalone.
2. **BioKids ISO** — Debian 13 + BioGuard preinstalled + a child-themed desktop.
   This is just a distribution format for #1.

Never work on the ISO before the packages work. See `docs/ROADMAP.md`.

**Repository:** `github.com/bilalgurkansanli/BioKids` (single monorepo)
**License:** MIT
**Author:** Bilal Gürkan Şanlı (Furkan), under the BioStudio label
**Site:** biokids.bilalgurkansanli.com (built separately, not in this repo)

---

## 2. Non-negotiable principles

These override any other instruction in this repo. If a task conflicts with one of
them, stop and ask.

1. **Nothing leaves the device.** No telemetry, no analytics, no cloud inference, no
   crash reporting, no "anonymous usage stats". Screenshots of a child's screen are
   processed in RAM on the local GPU and discarded. The only outbound network calls
   BioKids itself makes are: APT updates, blocklist downloads, DNS, and the parent's
   own SMTP server. Every one of those is listed in `docs/SECURITY.md` and must stay
   listed there.
2. **The child knows.** Supervision is visible to the child by default. No hidden
   screenshotting, no silent keylogging. A parent may reduce the indicator, but the
   feature is never covert by design.
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
| AI | Local VLM via `llama.cpp`, event-triggered, optional package, default OFF |
| TTS | Piper for stock voices; OpenVoice V2 for cloning. Both MIT-compatible |
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
│   ├── biokids_shell/         # child launcher server (child user, 127.0.0.1:7879)
│   ├── biokids_voice/         # Zio & Sio TTS service
│   └── biokids_ai/            # optional VLM watcher
├── extension/                 # Chromium managed extension (URL reporting)
├── debian/                    # single source package -> multiple binary packages
├── iso/                       # live-build config (Phase 3 only)
├── assets/                    # icons, character art, themes, sounds
├── tests/
└── .github/workflows/
```

**One Debian source package, several binary packages:**
`biokids-core`, `biokids-daemon`, `biokids-panel`, `biokids-shell`, `biokids-voice`,
`biokids-desktop` (metapackage), `biokids-ai` (Suggests, never Depends).

---

## 5. Runtime layout

| Path | Purpose |
|---|---|
| `/etc/biokids/config.toml` | machine-level config (ports, paths, feature flags) |
| `/var/lib/biokids/biokids.db` | SQLite, mode 0600, root-owned |
| `/var/lib/biokids/evidence/` | flagged screenshots, 0700, auto-pruned |
| `/var/log/biokids/` | logs, rotated |
| `/run/biokids/daemon.sock` | Unix socket, daemon IPC |
| `/usr/share/biokids/` | static assets, app catalog, blocklists |

| Service | User | Bind |
|---|---|---|
| `biokids-daemon.service` | root | unix socket only |
| `biokids-panel.service` | root | `127.0.0.1:7878` |
| `biokids-shell.service` | child (user unit) | `127.0.0.1:7879` |
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
| "what should I do next" | `docs/ROADMAP.md`, then `docs/TASKS-PHASE0.md` |

---

## 7. Current phase

**Phase 0 — BioGuard MVP.** See `docs/TASKS-PHASE0.md`.
Definition of done: on a Debian 13 VM, a parent installs the `.deb`, completes the
setup wizard, reaches the panel from their phone over Tailscale, sets a daily quota,
and the child session enforces it with spoken warnings.

T0–T8 are written and tested; `tests/test_acceptance.py` runs the exit criterion end
to end in one process. **T9 is not done**, and what is left is the VM. The packages
now build, pass lintian and install on a Debian 13 VM, the wizard completes there and
a child logs in to the launcher — which took D-153, D-154 and D-155 to get to.
Still ahead in `docs/VM-RUNBOOK.md`: Tailscale and the phone (§3.3), the spoken
warnings, and the upgrade, clean-install, reboot and clock runs (§5–§7).

The voice question is closed (D-137): there is no Turkish Piper voice anyone may
redistribute and `piper-tts` is not in Debian 13, so espeak-ng speaks the warnings and
Piper is used only where a family installs it themselves.

Nothing about the ISO belongs in Phase 0.
