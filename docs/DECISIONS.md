# DECISIONS.md

Every decision that shapes BioKids, with the reason. Two kinds of entry:

- **[owner]** — decided by the project owner. Do not change without asking.
- **[claude]** — decided on the owner's behalf ("sen karar ver"). Reversible, but
  read the rationale before reversing, and update this file if you do.

**How to read it.** The log is compact: each entry is its bold number and title, then
what was decided, why, and any constraint that still binds — not the story of the bug
behind it. Numbers are never reused or renumbered, because code and docs cite them; a
search for `D-<n>` lands on the entry. Gaps in the numbering were never used.

- An entry that a later one replaced is cut to a line or two and ends in *Superseded
  by*, *Reversed by* or *Completed by* D-xxx: read that one instead. *Partly
  superseded* means the rest still stands.
- The full original wording, bug histories included, is in git:
  `git show 435a526:docs/DECISIONS.md`.
- A new decision takes the next free number, at the end. When it replaces an older
  one, cut the older one down and point it at the new one.

Replaced in full or in part: D-14 → D-188 · D-16 → D-185 · D-32 → D-129 ·
D-53 → D-189 · D-55 → D-184 · D-57 → D-171 · D-59, D-61, D-62 → D-167 ·
D-70, D-72–D-75, D-77–D-80 → D-180 · D-86 → D-174, D-181 · D-87, D-88 → D-137 ·
D-89, D-90 → D-181 · D-92 → D-165, D-166 · D-149 → D-151. Completed by a later entry:
D-38 → D-177 · D-44 → D-193 · D-64 → D-187 · D-65 → D-173.

## Scope

**D-01 [owner] Target age is 4–10, not 0–13.**
An 11–13 year old is a different product and a much harder adversary (live USB, GRUB
edit, VM). The narrow range keeps the threat model honest and the UI coherent.

**D-02 [owner] Multiple child profiles, configurable.** One Linux user account per
child. Policies are per child, not per machine.

**D-03 [owner] Open source (MIT), public, portfolio piece.** MIT over GPL keeps the
door open, and it means no GPL-3 library in-process (D-30).

**D-04 [owner] Name: BioKids.** Package prefix `biokids-`. The control subsystem is
called "BioGuard" in docs and UI, but ships as `biokids-daemon`.

**D-05 [owner] UI in Turkish and English. Code, comments, commits and docs in English.**
i18n from the first line. Turkish is the default locale on the ISO; English is the
default for the source tree.

**D-06 [owner] No deadline.** Correctness over speed. This is a learning project first.

## Base system

**D-10 [claude] Debian 13 "Trixie", stable.**
Testing breaks weekly and makes bug reports unreproducible; stable freezes Python 3.13
and XFCE 4.20 and brings predictable security updates. When Trixie goes oldstable we
re-target in one planned sprint.

**D-11 [claude] amd64 only for v1. No ARM64 / Raspberry Pi.** ARM doubles the build
matrix and the testing surface. A Pi image is a good v2 headline, after the ISO.

**D-12 [claude] X11 only. Wayland is explicitly out of scope.**
The most consequential technical decision. Reading the active window, raising and
maximising windows (D-160, D-163), suppressing global hotkeys and forcing a kiosk window
are trivial on X11 and hard or impossible on Wayland without per-compositor portals.
LightDM pins the X11 session. A documented limitation that will eventually need solving.

**D-13 [claude] XFCE 4.20 as substrate, but the child never sees it.**
Chosen over LXQt and GNOME Kiosk for its 4 GB footprint, native X11 and well-documented
lockdown. The real decision: **we do not make a desktop environment child-friendly.**
Our own fullscreen launcher is the surface; fighting a DE's defaults is where earlier
kids distros (Qimo, DoudouLinux, LinuxKidX) burned their energy.

**D-14 [claude] Child launcher is a web app in Chromium kiosk, not a native GTK app.**
`biokids-shell` serves HTML on `127.0.0.1:7879`, opened by a dedicated Chromium with
`--kiosk --app=` and its own `--user-data-dir`. The owner iterates far faster in HTML
and CSS, and the launcher shares templates and i18n with the panel; cost ~250 MB RAM.
Escape is prevented by a managed Chromium policy (D-149, D-151, D-156, D-167, D-171).
*The single port is revised by D-188: one launcher port per child.*

**D-15 [owner] Minimum hardware: 4 GB RAM.** Idle footprint under 1.2 GB with shell,
daemon and panel; 2 GB is unsupported; disk 25 GB. The optional assistant needs 7 GB or
more (D-165) and is never installed by default.

**D-16 [owner+claude] Both live and installable ISO, but sequenced.** A live image
first (Phase 3a), a Calamares installer after (3b). *Superseded by D-185.*

**D-17 [claude] Secure Boot: inherit Debian's signed shim, do not sign anything ourselves.**
No custom kernel or out-of-tree modules, so Debian's signed `shim` and
`grub-efi-amd64-signed` should chain-load; if not, document "disable Secure Boot" rather
than buy a key. Verify on real hardware before claiming support.

**D-18 [owner] Full offline operation required.** Blocklists are bundled (D-167), DNS
resolves locally, speech and the assistant run locally. With no network the machine is
still usable and still protected.

**D-19 [claude] Split update policy.** `unattended-upgrades` for Debian **security**
updates only. BioKids' own packages never auto-update — a silently updated
parental-control daemon that regresses is worse than a stale one; the panel shows
"Update available" and the parent clicks.

**D-20 [claude] Bundled application catalog.**
Apps and web tiles are metadata in `/usr/share/biokids/catalog/`, not hardcoded; the
parent picks, and unpicked apps are not shown. The starting set was the Debian classics
(GCompris, TuxPaint, TuxMath, Klavaro, SuperTux, Frozen Bubble…), LibreOffice and a
home-only Thunar for 8–10, and EBA as the target of earned time (D-45). Revised by
D-163 and D-164.

**D-21 [owner] Chromium, not Firefox.** Its enterprise policy
(`/etc/chromium/policies/managed/*.json`) is far more capable than Firefox's, and the
managed-extension mechanism for URL reporting is Chromium-native.

**D-22 [claude] No Steam, no emulators, no Flatpak in v1.** Each is a sandbox escape
route and a support burden; Flatpak would let a child install an unmanaged browser.

**D-23 [owner] Account model.** Child: normal user, no sudo, no polkit admin, locked
into the shell. Parent: separate account with sudo and a normal XFCE desktop.

**D-24 [claude] Parent can log in graphically and also manage from the panel.** The
panel is the primary interface; the parent desktop is for real work and recovery.

## Packaging and distribution

**D-25 [owner] Phase 0 = `.deb` only. ISO comes later.** See `docs/ROADMAP.md`. v1.0
ships packages only (D-185).

**D-26 [owner] Single monorepo:** `github.com/bilalgurkansanli/BioKids`. One Debian
*source* package producing several *binary* packages.

**D-27 [claude] We run our own APT repository, built with `aptly`, hosted on GitHub Pages.**
Free, HTTPS, no server, driven by GitHub Actions; the signing key in GitHub Secrets, the
public key shipped as `biokids-archive-keyring`, the custom domain used from day one so
users never migrate. It matters more than the ISO: a family adopts BioGuard on an
existing Debian install in two commands, and updates ship without a new image.

**D-28 [claude] ISO hosting: archive.org primary, GitHub Releases secondary.**
GitHub caps assets at 2 GB; archive.org is free, unmetered and permanent. A GitHub
Release carries the `.deb` files, `SHA256SUMS` and a signature, and mirrors the ISO when
it fits. Applies from v1.1 (D-185).

**D-29 [owner] GitHub Actions CI from the start.** See `docs/PACKAGING.md`.

**D-30 [claude] License hygiene is a hard constraint.**
MIT-compatible dependencies only for anything we link or vendor. GPL software run as a
separate process (dnsmasq, Chromium, espeak-ng, mbrola, devilspie2, kiwix-serve) is
fine, and documented. This ruled out **timekpr-next** (GPL-3, D-40) and **XTTS-v2**
(CPML, non-commercial, D-89).

## Parent panel

**D-31 [owner] FastAPI + Python 3.13. SQLite with WAL.** One language across panel,
daemon, voice and the assistant.

**D-32 [claude] Jinja2 + hand-written CSS. No React, no npm, no CDN.**
The panel is forms and tables; a build step would ship `node_modules` in a `.deb`, and a
CDN breaks offline use. Those constraints stand. *Its choice of HTMX was revised by
D-129.*

**D-33 [claude] Bind `127.0.0.1:7878` always. Remote access exclusively via `tailscale serve`.**
`tailscale serve` publishes the panel inside the tailnet over real HTTPS, authenticated
by the tailnet: no open port, no reverse proxy, no TLS of our own (D-52). Without
Tailscale the panel is localhost-only and says so. Its public cost: D-157; setup: D-169.

**D-34 [owner] Responsive web, installable as a PWA.** No native mobile app: manifest,
service worker, offline shell, "Add to Home Screen".

**D-35 [claude] Panel starts at boot** as `biokids-panel.service`, `Restart=always`.

**D-36 [claude] Home screen = "Today".**
One card per child: remaining time as a ring, status, the app on screen (its name, not a
screenshot), and big buttons — **+10 min**, **Lock now**, **Shut down now** (more in
D-173). Everything else is one level down; a parent must understand it in three seconds.

**D-37 [owner] History and reports.** Daily and weekly usage, top apps and domains,
blocked attempts and alerts, rendered server-side as SVG — no chart library, no CDN.

**D-38 [owner] Email summaries, parent-supplied SMTP.** Digests and alerts through the
parent's own server; BioKids runs no mail service. *Completed by D-177.*

**D-39 [owner] No Telegram bot.** Email only.

## Time management

**D-40 [claude] Write our own time engine. Do not build on timekpr-next.**
timekpr-next is GPL-3 and single-maintainer and lacks what we need (category limits,
earned time, timeouts, our own reporting); we would wrap it and bypass it constantly.
Its years of edge cases are handled explicitly, each with tests: accounting runs on
`CLOCK_BOOTTIME`, and wall time only picks *which day* applies (guarded, D-172);
suspended time is not usage (D-125); only `active` logind sessions count; quota is per
Linux user, summed across sessions, never double-counted; the ledger is persisted every
30 s.

**D-41 [owner] Clock tampering must not work.** Implemented by D-40 and D-172.

**D-42 [owner] Parent chooses daily quota, time windows, or both.** Quota = "2 hours a
day", window = "16:00–20:00", both = the intersection; weekday and weekend apart.
Per-day exceptions: D-176; gaps between windows: D-143.

**D-43 [owner] End-of-time sequence.**
Warnings at **10, 5 and 1 minute**, spoken by Zio or Sio and shown on screen (D-160). At
zero the screen locks with a friendly message and a 2-minute countdown, then the machine
shuts down cleanly; the parent may choose "lock only" or "log out". Scope: D-138.

**D-43b [owner] Remaining time always visible to the child.** A ring in the launcher
corner, green → amber → red, with the minutes in it (D-162).

**D-44 [claude] Per-category limits, not per-app limits.**
**Oyun / Games**, **Eğitim / Learning**, **İnternet / Web**, **Serbest / Free**, a
default per catalog app that the parent can change. Four sliders are usable, thirty are
not; per-app overrides go under Advanced. Not built in Phase 0 (D-132). *Completed by
D-193* for the four limits; per-app overrides are still not built.

**D-45 [owner] Earned time.**
Time in **Eğitim** earns **Oyun** time at a parent-set ratio (default 1:1) up to a daily
cap (default +60 min), and the child sees the minutes arrive. A session counts only from
10 minutes, and 3 minutes without input pauses the earning. Where the minutes land when
there is no Games limit: D-132.

**D-46 [owner] Forced breaks, optional, default off.** Default when enabled: 40 minutes
of use → 5 minutes locked. A break does not consume quota.

**D-47 [owner] Parent can grant +10 minutes and can lock or shut down instantly**, from
the panel, including remotely.

## Authentication and hardening

**D-48 [claude] 6-digit PIN, argon2id hashed, plus an optional passphrase for dangerous actions.**
A PIN is what a parent uses on a phone. 5 failures → 15-minute lockout, exponential
after that, every attempt audited. Turning protection off, uninstalling or wiping data
needs the longer passphrase from the wizard, itself rate limited (D-171).

**D-49 [claude] Setup wizard on first run.** Language, parent, PIN and passphrase,
recovery code, children, apps, quota, filter mode, Tailscale and extras; under five
minutes, every step skippable with a sane default. *Its email step removed by D-182.*

**D-50 [claude] PIN recovery: two paths.** An 8-word recovery code shown once during
setup, and `sudo biokids-admin reset-pin` (D-131). Both are audited loudly. No email
reset and no backdoor.

**D-51 [claude] Panel session: 30 minutes idle, then PIN again.** Re-auth on every
destructive action regardless of session age. The idle timer slides (D-150).

**D-52 [claude] No TLS in our code.** Localhost or Tailscale, per D-33.

**D-53 [claude] Append-only audit log**, shown in the panel with filters: every policy
change, PIN attempt, override, alert and daemon restart. Never truncated by the app;
rotated by size with the oldest archived, not deleted. *Partly superseded by D-189:*
nothing edits an entry, but entries older than `audit_days` (a year) are deleted.

**D-54 [owner] The child gets no terminal.** No terminal emulator; VT switching off via
`logind.conf` and X11 `DontVTSwitch` (shipped by D-136). Defeatable by a determined
10-year-old with a phone and Google; see `docs/SECURITY.md`.

**D-55 [claude] Hardening is opt-in and scripted, never automatic.** A `biokids-harden`
script for GRUB, recovery mode and USB boot, never run at install. *Superseded by
D-184: not built.*

**D-56 [claude] USB boot / live media: out of scope.** Firmware-level, not ours. A
documented limitation.

**D-57 [claude] The panel is unreachable from the child session.** By the child
Chromium's `URLBlocklist`, with the PIN as the real control. *Mechanism superseded by
D-171: nftables closes the port to the children.*

## Content filtering

**D-58 [owner] DNS-level filtering plus a managed Chromium extension. No MITM proxy.**
A MITM proxy means our own CA in the child's trust store, broken pinned apps, and an
interception point that is itself a risk. The extension is D-187.

**D-59 [claude] Local `dnsmasq` as the resolver, with a configurable upstream.** Local
so it works offline; Cloudflare for Families upstream; nftables redirecting ports 53 and
853. *Superseded by D-167* (children only, bundled lists, the machine's own upstream).

**D-60 [owner] Allowlist mode exists.** Essential for 4–7. Kept by D-167 as "only the
sites I choose".

**D-61 [claude] Default mode depends on age band.** Allowlist for 4–7, blocklist for
8–10. *Superseded by D-167: every child starts on the open internet, harm closed.*

**D-62 [owner] Third-party blocklists are opt-in, presented as a recommendation.**
StevenBlack's list, downloaded on demand and refreshed weekly. *Partly superseded by
D-167*: harmful categories ship bundled and always on; an opt-in extra list (ads,
tracking) remains an idea, never built.

**D-63 [claude] YouTube and search engines get special handling.**
SafeSearch forced by DNS (Google, Bing, DuckDuckGo, and YouTube strict since D-167) and
by Chromium's `ForceGoogleSafeSearch` and `ForceYouTubeRestrict: 2`. The youtube.com
block for 4–7 was not built; YouTube Kids is a catalog tile (D-164).

**D-64 [owner] A child-friendly block page.** Local, with a Zio/Sio character, one
sentence at the age band's reading level and an ask-a-parent button (D-135). *Completed
by D-187:* the extension sends a page the filter closed to it, not to Chromium's error.

**D-65 [owner] Access requests.** The block page's button files a request the parent
answers in one tap: a wall becomes a conversation. *Completed by D-173.*

**D-66 [claude] Per-application network control: Phase 4, off by default.** nftables
cgroup matching so only catalog apps reach the network. Real value, real complexity;
not MVP.

**D-67 [claude] The child cannot install software.** No sudo, no polkit rule for
PackageKit, no Flatpak; `~` is not `noexec` (that breaks too much), and app launching is
mediated by the shell.

## AI supervision layer

The screen watcher most of these entries describe (event-triggered screenshots, OCR, a
local VLM) was cut from v1 by D-180. D-71, D-75's last rule, D-76, D-81 and D-82 still
bind; the assistant is D-165.

**D-70 [owner] Event-triggered, not continuous.** No per-second screenshots.
*Superseded by D-180.*

**D-71 [owner] Fully local inference. No cloud, ever.** Non-negotiable (principle 1);
binds the assistant (D-165).

**D-72 [claude] Layered pipeline, cheapest signal first.** Lists, then OCR, then a VLM.
*Superseded by D-180.*

**D-73 [claude] Model selection, tiered by hardware.** Qwen2.5-VL, Moondream2 or
SmolVLM by VRAM. *Superseded by D-180;* the assistant's models are in D-165.

**D-74 [claude] Screenshots are processed in memory and discarded.** Flagged frames
only, kept in an evidence directory. *Superseded by D-180: no screenshots at all.*

**D-75 [claude] The parent sees flagged evidence only, behind the PIN. No live viewing.**
*Partly superseded by D-180* (no evidence). Still binding: no live view of the child's
screen, which would make this surveillance software.

**D-76 [owner] The supervision indicator is configurable, visible by default.** A small
Zio/Sio badge in the launcher; a parent may shrink it (on the child's edit screen,
D-191), but the child's first-run explanation (D-111) always runs.

**D-77 [claude] The VLM sees the whole screen, but only on a trigger.**
*Superseded by D-180.*

**D-78 [owner] Output is a 0–100 risk score** with a reason and a category.
*Superseded by D-180.*

**D-79 [owner] Age-banded thresholds.** Notify and block bands per age (4–7 at 30 and
65, 8–10 at 45 and 80). *Superseded by D-180.*

**D-80 [owner] Notify before punishing.** The middle band informs; only the top band
blocks, its timeout opt-in. *Superseded by D-180.*

**D-81 [owner] Timeout duration: 30 minutes** when enabled. Its AI trigger went with
D-180. Still binding: a timeout never consumes quota — punishment and quota are separate
ledgers, and a timeout carries over midnight while quota does not (D-150).

**D-82 [owner] `biokids-ai` is a separate optional package, default off.** `Suggests:`,
never `Depends:`; everything else works without it. It holds the assistant (D-165).

## Zio and Sio

Full spec in `docs/VOICE.md`.

**D-85 [owner] Zio is the mother voice, Sio is the father voice.** Two TTS personas,
not chatbots; the assistant is a tile of its own (D-165).

**D-86 [owner] Voice cloning is opt-in and each family records its own.** A local
recording flow, no shipped voices. *Superseded by D-174 and D-181.*

**D-87 [owner] Default voices ship: one female (Zio), one male (Sio), Turkish and English.**
From the Piper catalog. *Superseded by D-137: no shippable Turkish Piper voice exists.*

**D-88 [owner] Piper is the TTS engine.** *Superseded by D-137 (espeak-ng speaks) and
D-145 (the engine chain).*

**D-89 [claude] Cloning pipeline: Piper → OpenVoice V2 tone-color conversion.** OpenVoice
(MIT); XTTS-v2 rejected for its non-commercial CPML licence. *Superseded by D-181.*

**D-90 [owner] ElevenLabs is supported as an optional cloud backend**, with the parent's
own key, off by default. *Reversed by D-181: it breaks principle 1.*

**D-91 [owner] The parent chooses which events speak.** Welcome, the 10/5/1-minute
warnings, time up, blocked site, breaks, earned time, bedtime: each switchable, all off
valid. A per-child switch: D-148.

**D-92 [owner] One-way only.** Zio and Sio speak; the child does not talk back.
*Reversed for the assistant by D-165 (LLM) and D-166 (microphone);* Zio and Sio stay
one-way.

**D-93 [owner] A visual accompanies the voice.** A character card while speaking: two
hand-made SVG characters with a few expressions. Vector, themeable, no external assets.

**D-94 [owner] Custom art direction, playful.** Rounded geometry, high contrast, warm
palette; Zio coral/amber, Sio teal/indigo; tokens in `docs/UX.md`. No stock icon packs.

## Child experience

**D-95 [claude] Always kiosk. The launcher is the only surface.** Not "kiosk for little
ones, desktop for bigger ones": two modes are two things to maintain. What changes with
age is the launcher's *contents*.

**D-96 [owner] Two age bands drive the UI.** **4–7:** very large icon-first tiles,
minimal text, spoken labels (D-175), no clock, no file manager. **8–10:** smaller
labelled tiles, a clock, a home-only file manager, their own usage stats. The band
follows the child's age; the parent can override it.

**D-97 [claude] The parent curates the app list** from the catalog. Nothing appears
that the parent did not enable (the screen: D-164).

**D-98 [claude] No settings application for the child.** Brightness and volume are two
launcher tiles; everything else is hidden.

**D-99 [claude] Login: LightDM with big avatars.** Child accounts are passwordless —
choosing the picture is enough; the real gate is the quota. The parent has a password.
Optional autologin into one child. Implemented by D-155 and D-170.

**D-100 [owner] The child may shut down**, from a clearly marked tile with a
confirmation.

**D-101 [claude] Screen recording and screenshot tools are removed from the child
session**, and the clipboard is not shared with the parent's session.

**D-102 [owner] Offline educational content is bundled** where licensing allows (Kiwix,
Blockly games): the machine stays useful with no internet. The Turkish children's
Wikipedia it named does not exist (D-164); Kiwix packs now serve the assistant (D-178)
and, as a tile a parent opts in to, the child (D-190).

## Threat model, privacy, legal

**D-105 [claude] The adversary is a curious 4–10 year old working alone.** Not a
teenager, not someone with a second computer and a search engine. Saying so in
`README.md` is a feature: it prevents false confidence.

**D-106 [claude] If the daemon dies, the session locks.** `Restart=always` plus
`OnFailure=biokids-lockdown.service`; a crash must never silently unprotect the machine.
Hardened by D-139, D-141 and D-172.

**D-107 [claude] Unplugging the network fails closed.** Filtering is local, so no DNS
means no browsing — a property of the local resolver (D-167).

**D-108 [owner] "Nothing leaves the device" is the headline promise.** In the README,
on the site, and in the wizard.

**D-109 [owner] Zero telemetry.** No exceptions, not even opt-in, in v1.

**D-110 [owner] A KVKK/GDPR note and a plain-language privacy policy ship with the
project**, for parents: what is stored, where, for how long, how to delete it, and
"this is not legal advice". Reviewed before any public launch.

**D-111 [owner] A first-run explanation screen for the child**, in their language: what
Zio and Sio do, that a grown-up can see which sites were visited, that they can always
ask. Non-skippable on first login.

**D-112 [claude] Export and erase.** "Download my family's data" (JSON + CSV) and
"Delete everything" (the database and voice data), with passphrase confirmation.

**D-113 [claude] Disclaimers.** README and wizard: BioKids is a supervision aid, not a
guarantee; no filter catches everything; it does not replace talking to your child.

## Development workflow

**D-115 [owner] Host is Windows 11 Pro. Development target is a Debian 13 VM in VirtualBox.**
Claude Code runs on the host; the VM is where the system is installed and tested.
Snapshot before touching boot, PAM, nftables or GRUB.

**D-116 [owner] A VM is sufficient for almost everything.** Real hardware only for wifi
and GPU drivers, Secure Boot, installer partitioning and inference speed; its absence
must not block Phases 0–2. Without GPU passthrough the assistant runs on CPU (D-165).

**D-117 [claude] Python 3.13** (Debian 13 default). `ruff` for lint and format,
`mypy --strict` on core and daemon, type hints required.

**D-118 [claude] Tests are required for logic, not for looks.** `pytest` on the time
ledger, the policy engine, the category allocator and the earned-time calculator (the
risk scorer went with D-180). Templates and CSS are checked by looking at them.

**D-119 [owner] English commits, Conventional Commits format, English docs and comments.**
Public-repo grade from the first commit: README with screenshots, CONTRIBUTING.md,
issue and PR templates, CHANGELOG, semantic versioning.

**D-120 [owner] Claude Code may ask questions, and works in large blocks.** Ask when a
decision changes architecture, adds a non-permissive dependency or touches the security
model; otherwise finish a coherent unit and report.

**D-121 [owner] Do not use the `pusht` skill here.** Feature branches, pull requests
into `main`, tagged releases — not sequential version branches.

**D-122 [claude] Docs structure: a short always-read `CLAUDE.md` plus detailed
`docs/*.md`.** One giant file would be re-read every session and still skimmed. Keep
`CLAUDE.md` under about 200 lines, with a table of which doc to open for which task.

## Implementation decisions

Settled while building and testing Phase 0, mostly in areas the earlier entries left
open, and often found by running the product on the VM (`docs/VM-RUNBOOK.md`).
`[owner]` entries here came from the owner using the product.

**D-123 [claude] Two daemon sockets, and the child's is authorised by peer uid.**
The `biokids` group can grant time and power the machine off, so no child is ever in
it. Beside `/run/biokids/daemon.sock` (`root:biokids` 0660) the daemon listens on a
world-connectable `/run/biokids/shell.sock` with a short list of operations, none of
which takes a child id: it maps the peer's uid (`SO_PEERCRED`) to a child and answers
about that child only. The peer's identity is the control, not the file mode.

**D-124 [claude] "+10 dk" raises the day's allowance; it does not subtract from usage.**
A grant is its own counter, worth what the parent said whatever the child has used;
subtracting from usage fails when usage is smaller than the grant. Warnings re-arm
(D-133).

**D-125 [claude] Suspended time is derived from two clocks, not reported by logind.**
`CLOCK_BOOTTIME` includes suspend and `CLOCK_MONOTONIC` does not, so the difference of
their deltas is exactly the sleep: exact, no round trip, nothing to disagree with.

**D-126 [claude] An unexplained gap is clamped, not billed.** An interval longer than
three ticks (daemon stopped, frozen, hibernated) is recorded as a gap and clamped: the
child loses nothing, and the parent sees the gap.

**D-127 [claude] The panel degrades rather than disappearing when the daemon is down.**
Children come from the database with live counters blank, state unknown and actions
withheld, under a banner — never "no children". The launcher does the opposite and
offers nothing to start: a launcher with no limits behind it is the failure to avoid.

**D-128 [claude] The launcher is progressive enhancement, and so is the panel.** Every
form is an ordinary form and every POST redirects; JavaScript only adds live counters. A
flaky tailnet gets a stale page, and a launcher whose script fails still starts apps.

**D-129 [claude] HTMX is not used. This revises D-32, which named it.**
D-32's constraints stand. HTMX could only be vendored — a minified blob nobody can
review, in a package whose claim is that you can see what it does — and the panel's two
dynamic needs are live counters (`EventSource`, built in) and forms. So: forms that POST
and redirect, ~120 lines of hand-written JavaScript per surface, and everything works
with JavaScript off. Vendor a library only for a screen that truly needs local state,
licence recorded — never to avoid writing a form.

**D-130 [claude] Adding a child provisions their Unix account; the packaging never does.**
`biokids_core.provision` runs when a parent adds a child, never in `postinst` (no child
exists then, and a system-wide autostart would start the kiosk for the parent too);
`biokids-admin provision-child` repairs. Tested rules: `adduser --disabled-password`
with no supplementary groups, never `biokids`; idempotent, never overwriting a file a
parent edited; never fatal to the request (the reason is audited); never removing a
home — leaving BioKids must not delete a child's drawings. See D-142, D-155, D-171.

**D-131 [claude] `biokids-admin reset-pin` reads the PIN, it does not take it.**
`getpass`, twice, or `--stdin` — never an argument, which every process table (the
child's included) and shell history would show. Every audited action needs an
`audit.action.*` string, checked by `dev/check_i18n.py`.

**D-132 [claude] Earned time lands in the general allowance when there is no Games
limit to raise.** With no Games limit (none exists in Phase 0, D-44), raising it is a
no-op and earned minutes vanished. They go to the day's allowance unless a Games limit
exists, when the parent's setting decides; `earned_lands_in_general()` is one condition
and its negation, so minutes are never counted twice or not at all.

**D-133 [claude] Warnings are re-armed on every tick, not only on a grant.** The
warning set follows the current remaining time, so anything that moves the end back —
an edited quota, a wider window, a break ending, a grant — brings the warnings back.

**D-134 [claude] The child's screens get their own strings, not the panel's.** A child
never sees parent-facing text ("try restarting the computer"), and the time-up screen
says what will really happen: `shell_state` carries the configured end action.

**D-135 [claude] Every button says what happened, and the child's screens name no
parent.** Every outcome a panel action redirects with is rendered: a grant that never
reached a stopped daemon must not look like success. The child's ask-a-parent button
says "ask a grown-up", not "Anneme sor": the request goes to whoever holds the single
parent account.

**D-136 [claude] The virtual-terminal guards D-54 names are actually shipped.**
`biokids-shell` ships the logind drop-in and X11 `DontVTSwitch`, once claimed but never
shipped. `ReserveVT=1`, not none: a parent must be able to recover from a broken X
server (principle 4) via GRUB's recovery entry or ssh over the tailnet.

**D-137 [claude] espeak-ng is what speaks; Piper is the upgrade.**
No Turkish Piper voice may be redistributed (checked 2026-09-20: `tr_TR-dfki-medium` is
CC-BY-NC-SA-4.0, `fahrettin` and `fettah` were withdrawn by their contributors), and
`piper-tts` is not in Debian 13. espeak-ng is in main and speaks Turkish with no model:
robotic, but a four-year-old who hears "beş dakika kaldı" has been warned, and one shown
a card they cannot read has not. Piper is tried first where a family installs it; MBROLA
is a Suggests (D-161). Each engine caches separately, since the key names no engine.

**D-138 [claude] An end action is scoped to whoever is actually at the machine.** The
sequence does not start for a child with no session. When someone else is logged in,
`poweroff` and `logout` narrow to `lock`, audited: the child loses the machine, nobody
else does.

**D-139 [claude] Three failed ticks in a row is a failed daemon, and it says so.**
`persist()` rolls back and re-raises. One failed tick is logged; three lock every child
session (no database needed) and exit non-zero for systemd's `Restart=` and
`OnFailure=`. Alive but not enforcing is "continue unprotected". Watchdog: D-172.

**D-140 [claude] A socket server cancels its connections before it waits for them.**
Since CPython 3.12 `Server.wait_closed()` waits for every handler, and the launcher
holds one open; the other order hung every stop for 90 s, then SIGKILL and a spurious
lockdown.

**D-141 [claude] `biokids-admin lockdown` falls back to the built-in defaults.** The
backstop runs because the daemon is gone — often a typo in `config.toml` — so it uses
`Config()` when the file cannot be read. Other subcommands still refuse a broken file.

**D-142 [claude] Creating a child's Unix account lives in its own unit, not in the
panel's sandbox.** The panel's `ProtectSystem=strict`/`ProtectHome=yes` is not widened
for one feature on a process that serves HTTP. `biokids-provision@.service` runs one
fixed command, `biokids-admin provision-child %I`, via `systemctl start --wait`;
unsandboxed because writing `/etc/passwd` and `/home` is its whole job, bounded by doing
only that. The name is checked against `UNIX_USER_RE` first; without systemd the caller
provisions in-process.

**D-143 [claude] A gap between two of today's windows is a pause, not the end of the day.**
While another window is ahead today, a closed one is `Reason.WINDOW_WAITING`: the screen
locks and nothing else. Only after the last window does the end action run.

**D-144 [claude] Granting time unlocks the screen, because nothing else can.** The
child's account has no password (D-99). The unlock happens only when the grant buys
usable time now, never outside the allowed hours. With light-locker: D-155.

**D-145 [claude] The voice fallback is a chain of engines, not a choice made once.**
`render` loops over voices and engines, each with its own cache, so Piper with no model
falls through instead of going silent; `status` names the engine that spoke. The order:
the parent's recording (D-174), Piper, espeak-ng or MBROLA (D-161), the on-screen card.

**D-146 [claude] Every `?error=` the panel redirects with is rendered, in one place.**
One flash, built in `base_context` and drawn in `base.html`; an unknown slug becomes a
generic failure, never dropped. No route discards a result it computed (daemon calls,
Tailscale refusals, provisioning). `GET /login` applies `safe_next` too.

**D-147 [claude] An auto-suggested account name may be adjusted; a typed one may not.**
A suggested name that collides takes the next free variant; a typed one is refused with
a message, never pointed at another child's account and ledger.

**D-148 [claude] The per-child voice switch is honoured, and the audit day is the
parent's day.** `policy.voice_enabled` gates speech alongside `config.voice.enabled`.
The audit filter converts the parent's local day to UTC, as timestamps are naive UTC.

**D-149 [claude] The kiosk policy goes where Chromium reads it, and a launch returns
when the app starts.** Chromium on Linux reads policy only from
`/etc/chromium/policies/{managed,recommended}`; a missing policy is "not locked down",
never success. `systemd-run --scope` execs, so the launcher waits only briefly and
reports activity at start. *Who writes the file: superseded by D-151.*

**D-150 [claude] The fifteen medium and low findings.** The rules they left: a failing
end action backs off rather than re-escalating every tick; an empty lockdown is not
audited as success; the child's shutdown tile refuses while another user is logged in;
the ring includes earned minutes; penalty and break counters carry over midnight while
quota does not (a punishment is not an allowance); the session cookie slides (D-51);
the shutdown confirmation works without JavaScript; a wrong PIN at the protection-off
gate is always audited; wizard steps validate before they write.

**D-151 [claude] The daemon writes the Chromium managed policy.**
It is the only BioKids process that can (root, its sandbox names the directory), and one
file serves every child; the launcher only checks it. A failure is logged at error level
but not fatal: a daemon that would not start without Chromium is worse than a kiosk
without a policy. Contents: D-156, D-167; module `biokids_core.browser_policy` (D-171).

**D-152 [claude] The wizard reports a failed provision, and does not stop for it.**
`biokids-admin provision-child` can repair the account later, while stranding a parent
mid-setup is principle 4 in small. Visible is the requirement; blocking is not.

**D-153 [claude] What the first real build found.** Packaging rules from the first
`dpkg-buildpackage` and lintian run: no `PYBUILD_NAME` (one Python distribution split
over several binaries); `.install` paths as `usr/lib/python3*/dist-packages`, since
`dh_python3` renames after `dh_install` (both checked by `dev/check_packaging.py`); the
postinst loads debconf's `confmodule`; every shipped command has a man page, and no
`man:` reference names a missing one; no lintian override that matches nothing.

**D-154 [claude] The suite runs against what Debian 13 ships, and touches nothing.**
An installed BioKids runs on trixie's Python packages, so the `pyproject.toml` floors
are trixie's versions, every import is declared, and CI runs the suite in a
`debian:trixie` container on apt's packages as an ordinary user. FastAPI needs
`python3-python-multipart`. An autouse fixture gives every test a machine without
`systemctl` or `tailscale`; the suite once started real units on the VM.

**D-155 [owner] A child logs in by their picture, and a lock holds.**
- *Login:* a PAM service of BioKids' own, `/etc/pam.d/biokids-lightdm` (`pam_nologin`;
  `pam_exec biokids-child-login` as `sufficient`; `@include lightdm`), selected by a
  LightDM drop-in that shows the user list; no other package's file is touched. An
  account gets in without a password only if it is in `biokids-child`, in none of
  `sudo`, `adm` or `biokids`, uid ≥ 1000, with no `user` session open (which separates
  login from unlock). Chosen over per-child passwords and autologin.
- *Accounts:* only an account with no usable password and no admin group joins
  `biokids-child`; admin accounts are refused as children; a suggested name steps past
  existing non-child accounts. The provisioning unit passes `%I`.
- *Locks:* `biokids-shell` depends on `light-locker`. Lockdown reads
  `loginctl --json=short`, ignores `manager` and `background` sessions, and exits 1 if
  a child stays unlocked. light-locker never sets `LockedHint`, so a grant unlocks every
  `user` session of the child and activates it only when the seat shows a greeter.
- `biokids-daemon` ships `/etc/chromium/policies/managed`, which Debian's chromium does
  not create.

**D-156 [claude] The child's browser policy names their web tiles; every child starts
with tiles.** There is one managed policy per machine, so the allowlist is the
launcher's origin plus every web tile enabled for any child (host and path, D-164),
rewritten on every reload; it governs a parent's Chromium there too. Tile windows use
`--password-store=basic` and `--hide-crash-restore-bubble`. A new child without tiles
gets the age-appropriate installed ones.

**D-157 [claude] The panel shows Tailscale's approval page; Tailscale's own logs stay home.**
On a new tailnet `tailscale serve` waits for a one-time approval; the panel shows it as
a button — only a link of the form `https://login.tailscale.com/f/<word>?node=<id>`,
never via the query string. Tailscale's certificates put the machine's `*.ts.net` name
in the public Certificate Transparency logs: D-33's cost, written in `SECURITY.md` and
under the button. `tailscaled` runs with `--no-logs-no-support` (principle 1; set by
BioKids since D-169).

**D-158 [claude] A child's screen locks when BioKids locks it, and never on its own.**
The child's light-locker runs with `--lock-after-screensaver=0 --no-late-locking
--no-lock-on-suspend --no-lock-on-lid`; the parent's session keeps its idle lock. Only
logind's Lock (allowance over, daemon dead) locks a child: an idle lock on a
passwordless account could only be lifted by a parent. A `.desktop` file added to
provisioning reaches an existing child only when they are provisioned again.

**D-159 [claude] What T8.5 and T5.7 found on a clean machine.** Upgrade, remove, purge
and the VT guards passed. Rules left: debconf templates say `_Description:`, or
po2debconf skips them; a maintainer script names the debhelper marker exactly once,
alone on its line (`dev/check_packaging.py`); the runbook upgrades all seven packages.
The greeter as large pictures is Phase 1 (begun by D-170).

**D-160 [owner] Warnings are heard, and they are seen over the app.**
`biokids_shell/relay.py` holds one subscription to the child's events for the
launcher's lifetime and forwards SPEAK to the voice service: the daemon makes no sound,
and the child may not open its socket (D-123). At 10, 5 and 1 minutes it raises the
launcher over the app with `xdotool` (BSD, a separate process) for seven seconds —
character, sentence, ring — then gives the screen back; the owner chose this over a
corner card the app hides. The child reads what they hear (`/spoken`). Panel and
launcher bound uvicorn's graceful shutdown at 3 s.

**D-161 [owner] Zio and Sio sound like people wherever the machine allows it.**
espeak-ng, gentler: 140 words a minute, `f3` for Zio and `m3` for Sio, pitch 55 and 40.
MBROLA where a family installs it (`mbrola` in contrib, voices in non-free; AGPL-3, a
separate process): Zio `mb-tr2`, Sio `mb-tr1`. BioKids pipes espeak-ng `--pho` into
`mbrola` itself, renaming `L/`, `l/` and `&` to `l` and `e`, because espeak-ng's own
path drops the l of every "kaldı"; if MBROLA fails, espeak-ng speaks. The cache records
what made it (`.made-by`) and is emptied when that changes.

**D-162 [owner] The ring carries the minutes, for every age.** The minutes left sit
inside the ring, "dk" under them: the circle speaks to a child who cannot read, the
number to the parent and to a child who can. The clock stays 8–10 only; the number
stays dark whatever the ring's colour (AAA contrast).

**D-163 [owner] Every app has a big close button, always in the same corner.**
Chosen over a floating home button that would cover every app. A BioKids xfwm4 theme
gives a 64-pixel title bar with one coral close button (`dev/make_xfwm4_theme.py`);
devilspie2 (GPL-3, a separate process; Debian 13's xdotool cannot maximise) maximises
every ordinary app window, so it is always top right. Apps that start full screen are
started windowed by their catalog `exec`, and the child's Chromium uses the window
manager's title bar. The launcher applies the xfwm4 keys with `xfconf-query` every
session, since provisioning never overwrites a child's files.

**D-164 [owner] Safe sites a parent can add, on a screen a parent can find.**
Checked from Turkey on 2026-10-03 for ads, accounts and content: KÜRE Çocuk, Bilim
Çocuk, Meraklı Minik, Code.org (Pre-reader Express for 4–7), Blockly Games; YouTube Kids
(ads) and PBS KIDS (English) offered with `suggest = false`. No Turkish children's
Wikipedia exists. A web tile is allowed its host *and path*, or the exact `allow`
patterns its catalog entry lists, and DNS never blocks them (D-167).
**Çocuklar → Uygulamalar ve siteler** has a checkbox per app and site (volume,
brightness and shutdown always on); saving is audited and rewrites the policy. Google
Earth replaces Stellarium. A tile whose program is missing is not shown; games are found
in `/usr/games`.

**D-165 [owner] The child can ask: a local assistant, "Sor Bana".**
A local model the child asks by typing or speaking (D-166), answering for their age,
refusing what a child should not ask, and telling the parent when it refuses. This
reverses D-92's "no LLM conversation": children ask "why" all day, and a local model
behind a strict gate is a learning tool, not a companion.
- Off by default, per child (**Çocuklar → Asistan**); models download only on the
  parent's button. **The child knows:** "Sorduğun bazı soruları ailen görebilir." An
  ordinary question is answered and forgotten; only a refused one is kept, as an alert.
- Answers fit the age band (five or seven short sentences, "Bunu bilmiyorum" when
  unsure); refusals are fixed catalog sentences, never model text.
- Five steps, each able to refuse: a Turkish and English lexicon before any model;
  small talk skips the classifier; a temperature-0 classifier with a JSON enum; the
  answer; the lexicon again. No model, a crash or a timeout gives "Şu an biraz
  dinleniyorum", never an unchecked answer.
- Launcher → daemon (policy, 20 questions per 10 minutes, alerts) → `biokids-ai`, its
  own user with `PrivateNetwork=yes`, running `llama-server` on demand and stopping it
  after 10 idle minutes. Gemma 4 E4B from 12 GB of memory, E2B from 7 GB (Apache-2.0),
  pinned by SHA-256. llama.cpp and whisper.cpp (MIT) are separate processes, rebuilt in
  the BioKids repository until trixie-backports has them. Real hardware decides whether
  E4B stays the 12 GB default; the VM's CPU takes 10–30 s an answer.

**D-166 [owner] The microphone, for push-to-talk in the assistant only.**
The children it is for cannot all type, so the owner allowed the microphone on these
conditions, which are the whole exception: off by default, switched on per child; only
while the child holds the button ("Dinliyorum"), at most 15 seconds; only the
launcher's origin, by Chromium policy, and no camera for anyone; in memory only,
transcribed by `whisper-server` (MIT) on the machine, never written, logged or sent.
Recording a room, background listening, wake words and saved audio stay out of scope.

**D-167 [owner] Open internet by default; the worst of it closed by DNS.**
Every child starts on **Açık internet, zararlı siteler kapalı**; **Çocuklar → İnternet**
switches to "only the sites I choose". Pornography and the like are closed whatever the
parent chooses. Overrides D-61 and D-62 and replaces D-59's design.
- `dnsmasq-base` (GPL-2; never `dnsmasq`, whose service is an open resolver) runs as
  `biokids-dns.service` on loopback port 5354. nftables (`inet biokids`) sends the
  children's DNS to it and rejects their DoT and known DoH; Chromium's DoH is off.
  Everyone else resolves as before.
- ShadowWhisperer's lists (Unlicense: adult, gambling, malware, scam, DoH; ~291,000
  domains) ship in biokids-core, refreshed per release: offline, no new outbound call.
  SafeSearch is forced by DNS too. A site a parent enabled is never blocked (D-164).
- Fail safe: unreadable lists stop the filter, and a stopped filter answers nothing —
  no internet, never unfiltered internet. The rules load at boot before the display
  manager; removing BioKids deletes them.
- One browser policy per machine, so with two children on different settings the
  stricter applies to both. A blocked site showed Chromium's error page, not D-64's,
  until the extension (D-187).

**D-168 [owner] The child's browser speaks the family's language.** `chromium-l10n` is
a Depends of biokids-shell, and the launcher sets the child's profile to
`tr-TR,tr,en-US,en` before Chromium starts, unless someone chose other languages in it.

**D-169 [owner] Phone access is one button: BioKids installs Tailscale itself.**
Tailscale stays optional, but phone access needed a terminal; the owner chose one button
over serving the panel to the home network over plain HTTP. If Tailscale is not up,
"Telefondan erişimi aç" starts `biokids-remote-setup.service`: it writes
`--no-logs-no-support` before installing, adds Tailscale's apt repository with its key
**shipped in biokids-core** (never downloaded, fingerprint pinned by a test), installs
`tailscale`, and runs `tailscale up` while the panel shows the sign-in link (only
`login.tailscale.com/a/`) as a button and a QR code; Funnel stays refused. Progress shows
on `/settings/phone`, never on a screen with a PIN form. New outbound connection, only
after the button:
`pkgs.tailscale.com` (SECURITY.md §4).

**D-170 [owner] The login screen is BioKids', and the panel is in the menu.**
A child of four does not read "ahmet" under Debian's silhouette. `biokids-shell` ships a
greeter drop-in with the BioKids background and no host name; provisioning gives
AccountsService the child's avatar and puts their display name in GECOS
(`biokids-admin refresh-greeter` updates existing children). The parent finds "BioKids
Ebeveyn Paneli" in the menu, and until setup is complete an autostart entry opens the
wizard for whoever logs in, never in a child's session.

**D-171 [owner] The review's security findings, closed.**
From the 2026-10-06 end-to-end review, fixed before anything new:
- Root provisioning never follows a child's symlinks (`O_NOFOLLOW` at every level,
  regular single-link files only).
- The launcher accepts only a loopback Host and no cross-site change
  (`Sec-Fetch-Site`, `Origin`). The panel is closed to the children by nftables.
- The browser policy (`biokids_core.browser_policy`) also closes `file://`, typed
  `javascript:`, guest mode and certificate click-through.
- `biokids-ai` is not in `biokids`; `espeak-ng` is a Depends of `biokids-voice`, since
  the spoken warning is the exit criterion.
- A recovery code must be eight list words; the passphrase gate stops after five wrong
  tries in fifteen minutes; grants are bounded at 12 h and timeouts at 24 h.
- `biokids-admin backup-db` runs before every migration. The daemon keeps only
  `CAP_NET_ADMIN` and `CAP_CHOWN`, the panel none.

**D-172 [owner] Fail-safe that is checked, not assumed.**
From the same review: the daemon is `Type=notify` with `WatchdogSec=90s`, pinged only
after a finished tick; commands are killed after 30 s, a tick after 60 s. The DNS filter
is checked from outside each minute; if it is open, one repair, then every child at the
machine is locked. A lock is verified from logind, and a session still unlocked after
three ticks is ended. Wall time runs on BOOTTIME (`GuardedClock`): a jump counts only
once NTP confirms it. The lockdown falls back to the `biokids-child` group when the
database is unreadable.

**D-173 [owner] "Anneme sor" is answered: requests, unlock, grants of any size.**
Three kinds of request: more time (offered in the launcher during the last fifteen
minutes, before the lock), a site, an app. Asking again returns the same request; six
an hour; unanswered ones expire after twelve hours. **İstekler** answers time with +15,
+30 or +60 and the rest with İzin ver or Şimdi değil; yes to a site allows it by policy
and DNS. The child is told, by Zio or Sio when voices are on: a question that vanishes
into silence teaches a child not to ask. Today gains "Kilidi aç" and +30.

**D-174 [owner] Zio and Sio in the parent's own voice, without cloning.**
Most of what a child would hear in a cloned voice is seven sentences, so **Ayarlar →
Kendi sesiniz** lets a parent record them on their phone. The page builds a mono WAV
itself; the panel checks it (PCM, mono, 0.5–15 s, ≤ 2 MB) and keeps it in
`/var/lib/biokids-voices/`, world-readable because the voice service runs as the child,
holding nothing else and never leaving the machine. A recording plays before any
synthesiser. Purge removes the recordings, and the encryption key goes only with the
data.

**D-175 [owner] For a child who cannot read yet.** In the 4–7 band a tap says the
tile's name (throttled), and the full screens nothing else speaks say their sentence
once as they open, through the voice service's `say`. A sun rides the end of the ring
and turns yellow, orange, red: something to watch that needs no number.

**D-176 [owner] Hours are picked, not typed; some days can be different.** Two ranges
of `<input type="time">` per kind of day. Under **Gelişmiş** any weekday can differ
(`policy.schedule_json`); an unticked day follows the weekday or weekend rule, so
nothing changes for a family that never opens it.

**D-177 [owner] Mail to the parent, through their own server.**
**Ayarlar → E-posta bildirimleri** takes the parent's SMTP server, stored encrypted, the
password never shown back. Once a minute the daemon mails new alerts and requests in one
message, and on Sunday evening each child's week; nothing is marked sent until it was. A
mail says what happened to which child, never what a child typed. Sending runs in a
thread with a 20 s timeout, so a dead mail server never stops supervision.

**D-178 [owner] The assistant checks itself against an encyclopedia on the machine.**
No prompt removes a small model's confident mistakes; a local source does. The download
also fetches Kiwix's top-articles Wikipedia packs (Turkish, English; CC BY-SA 4.0;
SHA-256 checked). A question's subject is looked up with `kiwix-serve` (GPL-3, a
separate process) inside the assistant's network namespace, and up to two articles'
opening text goes into the prompt as facts to prefer; no result is not an error. The
classifier's input is quoted with `json.dumps`; the self-harm answer gives 183 and 112.

**D-179 [claude] What the live run on the VM found.** D-171 to D-178 held with a real
child session. Fixed: the lockdown polls logind for up to twenty seconds before ending a
session (light-locker takes eight to ten); the first filter check waits thirty seconds,
and a repair waits for dnsmasq to answer; the encyclopedia looks a subject up as a title
first, with Turkish endings removed, opening paragraphs only.

## Release scope for v1.0

Decided by the owner on 2026-10-07, cutting the release to what is built and tested.

**D-180 [owner] The AI screen watcher is cut from v1.** (2026-10-07)
The Phase 2b watcher — event-triggered screenshots, OCR and a local VLM
(`docs/AI-LAYER.md`) — is cut from v1 and kept only as a future idea. Its unused fields
go with it: `evidence_dir`, the policy's `ai_enabled`, `ai_block_enabled` and
`ai_timeout_min`, the AI step in the policy precedence, and the screenshot columns of
`ai_alert`. This supersedes D-70, D-72–D-75 and D-77–D-80. The assistant (D-165) stays.

**D-181 [owner] Voice cloning and the ElevenLabs backend are cut.** (2026-10-07)
OpenVoice cloning (D-86, D-89) and the optional ElevenLabs cloud backend (D-90) are
removed. The parent's own recordings (D-174) give a child their parent's voice for the
lines that matter, with no GPU and no model; ElevenLabs would send a family's sentences
off the machine, which principle 1 does not allow.

**D-182 [owner] The wizard's stub step is removed.** (2026-10-07)
One step went: the email step (step 9 of ten), which asked nothing real (D-49). The
wizard now has nine steps and a closing screen (`/wizard/10`), and mail (D-177) is set
up under **Ayarlar → E-posta bildirimleri**, where it can be tested. The extras step
stays as step 9; its assistant line is still shown disabled, and the assistant (D-165)
is switched on per child under **Çocuklar → Asistan**.

**D-183 [owner] Logs go to the journal.** (2026-10-07)
`/var/log/biokids` and its logrotate file are removed; every BioKids service logs to the
systemd journal. The audit log a parent reads lives in the database (D-53) and is not
affected.

**D-184 [owner] `biokids-harden` is not built.** (2026-10-07)
The opt-in hardening script of D-55 (GRUB password, no recovery mode, no USB boot) is
not built. `docs/SECURITY.md` keeps its honest out-of-scope list and points a parent who
wants these to the manual steps instead; anything that can lock a parent out stays
manual, documented and reversible (principle 4).

**D-185 [owner] v1.0 ships as packages only; the ISO moves to v1.1.** (2026-10-07)
v1.0 is the `.deb` packages from the APT repository (D-25, D-27). The ISO moves to v1.1
and is built with `live-build` and a preseeded Debian installer, not Calamares. This
supersedes D-16; D-17 and D-28 apply when it ships.

**D-186 [owner] The code was split for maintainability.** (2026-10-07)
The panel's routes and the daemon's supervisor were each split into modules, with no
change in behaviour.

## Built for v1.0

Settled on the owner's behalf while finishing the release, the same day.

**D-187 [claude] The browser extension is built, and packed on the machine.** (2026-10-07)
A Manifest V3 extension in plain JavaScript (`extension/manifest.json`,
`extension/background.js`) does two things. A top-level navigation the policy or the
DNS filter closed (`ERR_BLOCKED_BY_ADMINISTRATOR`, `ERR_NAME_NOT_RESOLVED`) goes to the
child's launcher's `/blocked` page with its "ask a grown-up" button (D-64, D-173). And
the host name of each page a child opens, never the path, title or content, goes to the
launcher's `POST /visit`; the daemon records it as a `usage_event` of kind `visit`, the
same host at most once per ten minutes (`VISIT_REPEAT_SECONDS`). It talks to nothing
but 127.0.0.1, and the child is told on the first-run screen (`shell.firstrun.p5`,
D-111). biokids-shell ships the source; its postinst runs `biokids-admin
pack-extension` (`biokids_core.extension_pack`), which signs a CRX3 with a key made on
the machine (`/var/lib/biokids/extension-key.pem`, 0600) and writes `biokids.crx`,
`updates.xml` and `extension-id` to `/var/lib/biokids-ext`. The daemon force-installs
it with `ExtensionInstallForcelist` (`biokids_core.browser_policy`). No key is in the
repository or the package, so nothing off the machine can sign an update. A failed pack
is not fatal: blocked pages show Chromium's error and no visits are listed.

**D-188 [claude] One launcher port per child.** (2026-10-07)
Two children logged in at once (fast user switching) shared the one launcher port. A
child's launcher now listens on `shell.port + uid % 100` (`ShellConfig.launcher_port`,
7879–7978), and the kiosk policy lists every child's launcher origin. Because a
launcher acts as its child on the daemon's socket (D-123), an nftables chain
`launchers` (`dnsfilter.render_nft`) rejects a connection to a child's port from any
other uid: a sibling, the parent, a page in either browser. The extension finds its
launcher by probing the range. Two children whose uids differ by a multiple of 100
get the same port: the chain then allows both of them, the daemon logs the collision,
and only one of the two can be logged in at a time.

**D-189 [claude] Records are kept for a set time, then deleted.** (2026-10-07)
`ARCHITECTURE.md` promised retention and nothing deleted anything: the machine was
building the very dataset principle 1 says should not exist. `[retention]` in
`/etc/biokids/config.toml`: `activity_days = 30` (usage events, the visited sites among
them, and the assistant's alerts: the most personal go first), `usage_days = 400`
(`usage_tick`, `ledger_day`), `audit_days = 365`, `requests_days = 90`. The daemon
prunes once a day (`housekeeping`, first five minutes after it starts) in one
transaction, and a failure is retried the next day. Panel sessions a day past their
idle timeout go too, and only the 20 newest boot epochs are kept.
`biokids_core.retention`. Partly supersedes D-53.

**D-190 [claude] Offline Wikipedia, for a child to read.** (2026-10-07)
A `vikipedi` web tile (8–10, `suggest = false`: the most-read articles include grown-up
subjects, so a parent ticks it on purpose) opens `http://127.0.0.1:7877/`, where
`kiwix-serve` serves the `wikipedia_*.zim` files the assistant's download already
fetches (D-178) into `/var/lib/biokids-ai/models`. `biokids-wiki.path` starts
`biokids-wiki.service` when one appears; both ship in biokids-ai. kiwix-serve (GPL-3,
kiwix-tools) is a separate process (D-30), run as `biokids-ai` on loopback with no
capabilities. The launcher does not draw a web tile on this machine whose port is
closed, so the tile appears only once there is something behind it.

**D-191 [claude] A child's details can be changed, and a child removed.** (2026-10-07)
`/children/{id}/edit` changes name, birth year, picture and language, and under
**Gelişmiş** the supervision indicator (`badge`, `small`, `hidden`; D-76). A new name or
picture is provisioned again, so the login screen shows it (D-170). Removing a child is
behind the passphrase, refused while the child is at the machine or the daemon cannot
say, and done by `biokids-unprovision@.service` (`biokids-admin unprovision-child`), for
the reason D-142 gives: the account leaves `biokids-child`, its AccountsService key
file gets `SystemAccount=true` and its shell becomes `/usr/sbin/nologin` - on the VM the
shell alone did not take it off the greeter; AccountsService's own key did, read again
when `/etc/passwd` changes, so the unit writes the key first and waits until
AccountsService reports it. The greeter already showing keeps its list until it
restarts. The launcher's autostart entry is removed, and any other session file only
while it is still byte-identical to what BioKids wrote. The account
and its home stay (D-130). If the account cannot be changed, the row stays and the
parent is told: a passwordless account in the login group with nothing supervising it
is worse than a child not removed. Otherwise the row goes, with everything recorded for
the child but the audit log. Adding the child back - even under a suggested name -
reuses the hidden account, clears the key and restores `/bin/bash`.

**D-192 [claude] A history screen.** (2026-10-07)
`/history`: fourteen days of minutes as bars against each day's quota, each a link to
its day; for that day the apps by time, the sites the extension reported (D-187), one
line per host with its first and last time, and the child's requests. Plain HTML, no
script, no chart library (D-129). It shows only what retention already keeps (D-189)
and stores nothing of its own. Part of D-37; the week is in the Sunday mail (D-177).

**D-193 [claude] Limits per kind of app can be set.** (2026-10-07)
The policy engine has enforced category limits since Phase 0 (D-44) and nothing could
set them. The time screen's **Gelişmiş** section takes minutes a day for games,
learning, web and free; empty is no limit of its own, and the day's time still applies.
With a Games limit set, earned time follows the parent's setting (D-132). When a kind is
used up, the child's blocked page says so and offers no "ask a grown-up": granted
minutes raise the day (D-124), not the category, so a yes would not let them back in.

**D-194 [claude] Four small fixes before the release.** (2026-10-07)
The panel's service worker served the stylesheet as the page when the network was
gone; it now keeps `/offline`, rendered without the session so the cached copy holds no
name and no token. The wizard's apps step always keeps volume, brightness and shutdown,
as the apps screen does (D-164). The assistant's voice box, disabled while the speech
model is missing, no longer turns the voice off when the page is saved, since a
disabled box is not sent. The launcher's volume slider reads the real level (`wpctl`,
`pactl` or `amixer`) instead of starting at 70.

**D-195 [owner] A guided first run.** (2026-10-07)
A parent installing BioKids may have just installed Linux for the first time; nothing
on the way to a working setup may ask them to know an address, log out, or read a
paragraph about networking. So: biokids-panel's postinst opens the setup wizard at
once in every desktop session already open, through the script `/etc/xdg/autostart`
runs at the next login (`/usr/lib/biokids/biokids-open-setup`), started inside that
user's own systemd manager with their display. The same script puts a "BioKids
Ebeveyn Paneli" shortcut on a parent's desktop, once; deleted, it stays deleted. The
children's Chromium policy is machine-wide, and with any child on "selected sites
only" its `*` closed the panel in the parent's own Chromium: the panel's origin is
allowed in it now, and nftables keeps every child off the port (D-171). Phone access
is four numbered steps that each say when they are done - connect this computer,
allow phone access, install Tailscale on the phone (App Store and Google Play as QR
codes; done when a phone or tablet appears among the tailnet's peers), open the panel
on the phone. The closing screen lists what is left; until the machine has restarted
once after setup, Today shows a banner with a confirmed "Şimdi yeniden başlat" (daemon
op `reboot_now`), because LightDM reads the login-by-picture setting only when it
starts (D-155). A help page answers the questions parents ask first and is open
before signing in, since "I forgot my PIN" is one of them.

