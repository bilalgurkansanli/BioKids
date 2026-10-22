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
D-89, D-90 → D-181 · D-92 → D-165, D-166 · D-149 → D-151 · D-163 → D-197.
Completed by a later entry: D-38 → D-177 · D-44 → D-193 · D-64 → D-187 ·
D-65 → D-173 · D-137 → D-196.

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
D-163, D-164 and D-203.

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
*Completed by D-196:* the panel downloads Piper and that voice on the parent's button.

**D-138 [claude] An end action is scoped to whoever is actually at the machine.** The
sequence does not start for a child with no session. When someone else is logged in,
`poweroff` and `logout` narrow to `lock`, audited: the child loses the machine, nobody
else does. *Amended by D-209:* only a login on a seat is someone at the machine; an ssh
session is not.

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
session, since provisioning never overwrites a child's files. *Partly superseded by
D-197:* the close button is the app bar's; the title bar keeps it only as the
fallback.

**D-164 [owner] Safe sites a parent can add, on a screen a parent can find.**
Checked from Turkey on 2026-10-03 for ads, accounts and content: KÜRE Çocuk, Bilim
Çocuk, Meraklı Minik, Code.org (Pre-reader Express for 4–7), Blockly Games; YouTube Kids
(ads) and PBS KIDS (English) offered with `suggest = false`. No Turkish children's
Wikipedia exists. A web tile is allowed its host *and path*, or the exact `allow`
patterns its catalog entry lists, and DNS never blocks them (D-167).
**Çocuklar → Uygulamalar ve siteler** has a checkbox per app and site (volume,
brightness and shutdown always on); saving is audited and rewrites the policy. Google
Earth replaces Stellarium. A tile whose program is missing is not shown; games are found
in `/usr/games`. *Partly superseded by D-203:* Dünya is Marble; Google Earth stays,
offered for 8+.

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
before signing in, since "I forgot my PIN" is one of them. *Extended by D-224:*
step-by-step guides.

**D-196 [claude] The natural voice, downloaded with one button.** (2026-10-08)
The owner found Zio and Sio "very robotic", and espeak-ng is. Piper sounds like a
person, and BioKids still may not ship it (D-137), so the family fetches it from its
authors. **Ayarlar → Zio ve Sio'nun sesi** (`/voices`) says which voice speaks now,
"robot sesi" or "doğal ses", and offers **Doğal sesi indir (yaklaşık 90 MB)**, with one
sentence on the licence and a reminder that the parent's own recordings (D-174) are
more natural still. The button audits `voice_download`; the daemon op `voice_setup`
starts `biokids-voice-setup.service` (oneshot, never enabled, root with no
capabilities, `ProtectSystem=strict`, writable only `/var/lib/biokids-voices`), which
runs `biokids-admin voice-setup` (`biokids_core.natural_voice`):
- Piper 2023.11.14-2, `piper_linux_x86_64.tar.gz` from the archived MIT-licensed
  `github.com/rhasspy/piper`, unpacked into `paths.piper_dir`
  (`/var/lib/biokids-voices/piper/`) through tarfile's `data` filter, only under the
  archive's `piper/`; and `tr_TR-dfki-medium` (`.onnx`, `.onnx.json`, its model card)
  from `huggingface.co/rhasspy/piper-voices` at commit `c10ece1a`, into
  `models/piper/`, hard-linked as `tr-sio.onnx` and `tr-zio.onnx` with a `.onnx.json`
  beside each, the names `VoiceBank` looks for. Every file is pinned by SHA-256 and
  size, written beside its place, checked, then renamed; a mismatch is deleted and the
  unit fails. No other network, at any other time.
- Piper goes in first and the voice last: audio is cached by sentence and voice, never
  engine, and the voice's key changes when its model appears, so the first line under
  the new key is Piper's, not espeak-ng's. The voice service looks for Piper in
  `piper_dir` before PATH, on every line, and a Piper that appears while a child is at
  the machine joins the chain at the next sentence (`Synthesizer._wake`).
- The panel follows the unit on `/voice-natural`, which refreshes itself and reads
  the percentage from the files on disk; `/voices` never refreshes, since a recording
  may be under way. States: absent, running, failed (with the button again),
  installed.
- The only Turkish voice in the catalogue is a man's (DFKI's "OT" speaker; dataset CC
  BY-NC-SA 4.0: a family may use it, BioKids may not ship it). Sio speaks it as it is.
  Zio, the mother (D-85), speaks the same model lifted by 1.2 (`ZIO_LIFT`): Piper
  speaks 1.2 times slower, and the WAV's sample rate is raised 1.2 times, so pitch and
  formants rise about three semitones at an unchanged pace, with every sample as Piper
  made it. Chosen over sox's `pitch` effect (a phasey warble, and a new Recommends)
  and over leaving Zio on MBROLA `tr2` or espeak-ng (a woman, but a robot again beside
  a natural Sio): to a child, a lighter, clearly different person beats a female
  robot. She does not sound like a woman; a family who wants that records their own
  voice, as the card says. A voice of Zio's own at `tr-zio.onnx`, any other file, is
  not lifted. Piper speaks with `--length_scale 1.1`, for D-161's reason.
- Purging biokids-core with the family's data removes it with the recordings;
  `biokids-admin voice-setup remove` removes it alone. Turkish only: a child set to
  English still hears espeak-ng. Two rows in `docs/SECURITY.md` §4. Completes D-137.
  *Amended by D-223:* the wizard's box, ticked for a Turkish family, starts it too, and
  it is tried again until it is there. *Amended by D-225:* an English voice for the
  English corner comes with it.

**D-197 [owner] The app bar: home, the app's own volume, and a close that works.** (2026-10-08)
The owner asked for a "send to background" button and the app's volume beside every
app's close button, and found that Frozen Bubble 2 would not close: its window has a
fixed size, so devilspie2 cannot maximise it (D-163) - it floated mid-screen with the
close button at its own corner and the launcher clickable around it - and a title
bar's button only asks, so an app that ignores WM_DELETE_WINDOW could not be closed by
a child at all. A title bar cannot hold a volume control. So the launcher, in kiosk
mode, runs and restarts `python3 -m biokids_shell.appbar` (`windows.AppBar`): four
64-pixel buttons at the top right of the screen the active app is on, shown while an
app window is active and never while the launcher is (WM_CLASS `biokids-kiosk`), which
raises itself for the warnings and the end of the day.
- 🏠 minimises the app, its dialogs with it, and activates the launcher. The app keeps
  running; its tile then runs the bar once with `--bring-back <unit>`, which shows the
  topmost window of that scope again instead of starting a second copy. A scope with
  no window is left 30 s to open one, then stopped and the app started afresh. Web
  tiles open a window each time: all of them are one Chromium, in the scope of
  whichever tile started it. *Partly superseded by D-208:* the running bar is asked
  over a socket, and a bar that cannot look never leads to a second copy.
- 🔉 and 🔊 step the app's own streams by ten percent, 0 to 100, never the machine's,
  so Zio and Sio stay audible: `pactl -f json list sink-inputs` (its text form when
  the JSON cannot be read; `pulseaudio-utils` talks to Debian 13's pipewire-pulse), the
  streams whose `application.process.id` is in the window's `biokids-app-*.scope`
  (`_NET_WM_PID`, `/proc/<pid>/cgroup`) - by scope, since Chromium plays from a helper
  process. Five rising bars show the level; both buttons dim when the app has no
  stream.
- ✕ asks the window to close (`_NET_CLOSE_WINDOW`), and stops its scope with
  `systemctl --user stop` (TERM, the ten-second grace, KILL) if it is still there 3 s
  later, or at once on a second tap. An app that answered with a window of its own
  ("save your drawing?") is left to the child for a minute. Only a `biokids-app-*.scope`
  under the child's own `user@<uid>.service` is ever stopped (SECURITY.md §2).
GTK 3 and libwnck 3 through PyGObject: LGPL libraries from Debian's own packages,
loaded at run time in that process only, never vendored or changed. The LGPL lets a
program under any licence load a library that stays replaceable, which a Debian
package is, so D-30 holds. A popup (override-redirect) window: one the window manager
knows can be clicked into focus - and the "active app" would then be the bar - would be
maximised by devilspie2, and is covered by an app gone full screen. The popup never
takes the focus and is raised again whenever the stacking changes. While the bar is up,
xfwm4's `button_layout` is `|`: one way out, not two. The launcher puts `|C` back the
moment the bar's process ends, and every session starts with `|C`, so a child is never
without a close button - without GTK (exit 3) the title bar keeps it for the session.
The 64-pixel title bar stays: over a maximised app the bar sits in its strip and covers
the title, not the app, which is what made D-163 turn down a floating button; it names
the app; and it holds the fallback button in the same corner. xfwm4 now opens every
window in the middle of the screen (`placement_ratio` 100). A click on the launcher
around a small window still brings the launcher forward; the app's tile brings the app
back. Dimming the launcher while an app is open was left out: the page could not tell
which app to return to.

**D-198 [claude] "Sor Bana" only when the assistant can answer.** (2026-10-08)
The owner pressed "Sor Bana" and nothing answered. Two causes. The tile showed whenever
a parent switched the assistant on, also where biokids-ai is not installed - it cannot
be on a stock Debian 13, which has no llama.cpp - or no model was downloaded: a tile
that can only say "I'm resting" is a broken toy. The daemon now asks the assistant
service every five minutes what it can do (`Supervisor.refresh_assistant`; its socket
exists only where biokids-ai is installed), and the launcher shows the tile only when
the service says a chat model is ready, the microphone only when speech recognition is
too; the panel's assistant page says in plain words why the tile is hidden. And where
it was installed, every question came back "I'm resting" because D-171 had taken
CAP_DAC_OVERRIDE from the daemon and the assistant's socket is 0660 `biokids-ai`: the
daemon unit now has `SupplementaryGroups=biokids-ai` (biokids-core creates the group on
every machine), the narrow way in rather than the capability back. On the development
VM the model then answered too slowly to be useful (about one token a second with the
VM in the background); real hardware is the measure (D-165).
Amended 2026-10-11: a service that is not ready is asked again every 20 seconds,
not five minutes. At boot the daemon asked before biokids-ai had opened its socket,
and a child who logged in straight away found no "Sor Bana" until the next check.

**D-199 [owner] The launcher's sound at the top right; no spreadsheet.** (2026-10-08)
The volume moves from a slider in the launcher's footer to the header's top right,
where a child looks for it: two big buttons, 🔉 and 🔊, ten percent a tap, and a bar
between them that starts at the sound's real level. A four-year-old taps; dragging a
thin slider is a grown-up's gesture. Brightness stays in the footer. "Tablo"
(LibreOffice Calc) leaves the catalogue and biokids-desktop's Suggests: a spreadsheet
is of no use to a child of four to ten; "Yazı" (Writer) stays.

**D-200 [owner] No boot menu in front of a child.** (2026-10-08)
The owner asked why Debian's GRUB menu appears at every start. biokids-desktop now
hides it: `/usr/share/biokids/grub/biokids.cfg` (`GRUB_TIMEOUT_STYLE=hidden`,
`GRUB_TIMEOUT=1`), linked into `/etc/default/grub.d/90-biokids.cfg` by the postinst,
which runs `update-grub`; the postrm removes the link and runs it again, so
`apt remove` brings the menu back. A link rather than a conffile, because a conffile
would outlive `apt remove`. Not a lock (principle 4): Esc during that second shows the
menu and its recovery mode, and after a failed boot Debian's own 30-second menu still
appears. Only where `/boot/grub` and `update-grub` exist; another bootloader is left
alone. biokids (the packages on a parent's existing desktop) does not touch the boot.

**D-201 [owner] The app bar keeps up with a dragged window; sound by dragging; open apps and the time in view.** (2026-10-08)
The owner's second pass over D-197 on the VM. Dragging an app by its title bar left
the bar trailing behind it - "the close button and the sound controls arrive late, it
looks very bad" - because the bar moved only on its 500-ms tick; over a Chromium
window that the drag had taken out of maximisation it then sat about a title bar's
height too low and well left of the window's corner.
- The bar follows the active window's libwnck `geometry-changed` and `state-changed`:
  hidden at the first change, shown again on the window's title bar 250 ms after the
  last (`appbar.Follower`). A window that was maximised and was dragged with a button
  or finger down (the pointer's button mask) is maximised again once it is let go: a
  child never needs a floating window, and the close button stays in the corner
  (D-163). A fixed-size window (Frozen Bubble) or a full-screen one is only followed.
  Without a button down nothing is maximised, so an app that sizes itself is not
  fought (*amended by D-205:* except a new window in its first seconds); and a window still changing after 1.5 s without one (10 s with one) gets the
  bar back, following it, so the way out is never gone for long.
- Where the bar goes is the window's frame as the X server has it at that moment: the
  contents' position and size (a foreign GdkWindow: XTranslateCoordinates,
  XGetGeometry) grown by `_NET_FRAME_EXTENTS`, or shrunk by `_GTK_FRAME_EXTENTS` for a
  window that draws its own title bar; libwnck's numbers only where these cannot be
  read. Always that window's own title bar, kept on the monitor - no longer the
  monitor's corner for a window merely as wide as the screen. Why libwnck's frame was
  wrong over the dragged Chromium window could not be reproduced without an X session;
  where the two differ the bar says so at debug level (VM-RUNBOOK §8.9).
- The bar draws the title bar's two-pixel bottom line and its buttons' borders are
  transparent, so it sits flush in the 64-pixel title bar instead of a pale box
  breaking the line; each 56-pixel face sits where the title bar's close button sat.
- The five level glyphs become a slider between 🔉 and 🔊 (a Gtk.Scale, 0-100, a
  40-pixel knob, under an event box that takes the finger over the bar's whole
  height). It sets the app's own streams as before; while a finger drags, at most every
  80 ms and only the latest level while one is being set. The buttons still step by ten.
- Between 🏠 and 🔉: "⏳ 12 dk", the minutes left as the launcher's own `GET /state`
  (on this child's launcher port, loopback only, no proxy) reports them every 15 s -
  never worked out in the bar - orange under five minutes and red under one, as the
  launcher's banners; under it the clock, for 8-10 only, as on the launcher (UX.md
  §2.2). Showing it to 4-7 too is a one-line change if the owner wants it. *The
  owner asked for it the same day: the clock shows for every child (7c6b1e7).*
- The launcher's own sound bar (D-199) can be dragged or tapped anywhere along its
  64-pixel height (pointer events, `touch-action: none`) and is a slider to a screen
  reader and the arrow keys; it posts at most every 120 ms and once where the finger
  lifts.
- The launcher's footer lists the apps sent to the background as "Açık", and their
  tiles carry a dot: `GET /running` returns the app tiles whose `biokids-app-*.scope`
  one `systemctl --user is-active` says is running (a closed one is forgotten), polled
  on load, every 5 s, and whenever the launcher comes back in front. Each button is
  drawn from its own tile and goes through `/launch/<slug>`, the second tap that
  brings the same window back (D-197). Web tiles are not listed: each opens a window.


**D-202 [owner] A forbidden search is never sent.** (2026-10-08)
On the VM a child typed an adult word into Google: SafeSearch filtered the results, but
the search went through and a results page came back. The owner wants such searches
not to happen at all, in Wikipedia and the offline Vikipedi too. The extension (D-187)
now installs `declarativeNetRequest` rules that refuse, before it is sent, any page,
frame, script or background request whose address carries a listed word; the tab then
shows the launcher's "Bunu arayamayız. İstersen bir büyüğüne sorabilirsin."
- **The list** is `biokids_core/data/search_words.toml`, 89 terms: pornography and
  explicit sexual searches in Turkish and English, the vulgar sexual words children
  pick up, and first-person and method searches about self-harm. Left out on purpose,
  and written down in the file: anatomy and sex education, sexual orientation,
  violence, drugs and gambling, "intihar" on its own, and words a child searches
  innocently ("am", "meme", "göt", "dick", "naked"). A term is a prefix, a whole word,
  an exact word ("seks" must not become "seeks") or a spelled-out prefix ("p.o.r.n.o").
- **Matching** is compiled into the patterns, because Chromium matches an address, not
  the typed text: any case; a Turkish letter also as its ASCII neighbour and its
  capital ("sikis", "SİKİŞ"); look-alike digits; held-down letters; any spaces or
  punctuation between a phrase's words. A dotted "i" never matches a dotless "ı", so
  "sık sık", "sıkışık" and "kaşık" stay searchable; "seksen", "seksek", "saksofon",
  "milföy" and "fahiş fiyat" have tests of their own. Known false positive: capitals
  with an ASCII "I" ("SIKIŞIK" reads as "sikiş...").
- **Where a word counts.** A term of five letters or fewer only in a search box's value
  (`q`, `query`, `search`, `search_query`, `text`, `pattern`, `term`) or a title after
  `/wiki/` or `/content/` (kiwix-serve 3): random tokens in addresses spell short words
  now and then. A longer term or a phrase after any `=` or `/`.
- **Chromium's limits.** Each regex gets 2 KB of RE2 memory, about a hundred
  instructions. `search_filter.re2_cost` estimates them and terms are packed up to 104,
  measured against Chromium 154's own `isRegexSupported` (Edge 154): 804 patterns built
  at 104 and 116 from the list and 400 heavy random parent words were all accepted. The
  default list is 79 patterns; at most 990 are installed.
- **Block, not redirect.** A redirect needs a host permission on every site searched;
  a block needs none, so the extension's only host is still 127.0.0.1. The refused
  navigation fails with `ERR_BLOCKED_BY_CLIENT`, which only our extension can cause
  (every other is blocked), and the extension sends the tab to
  `/blocked?reason=search.blocked`: why, never what. Rule 1 always lets the launcher's
  own origin through. A page that searches without loading one (YouTube posts the words
  in a body and then changes the address) is checked on `onHistoryStateUpdated`.
- **Delivery.** `GET /search-rules` on the launcher serves the patterns, built from the
  list and the parent's words, which the daemon adds to the shell state. The extension
  asks on install, at start, every five minutes and when its launcher answers again;
  it swaps the whole set in one `updateDynamicRules` call, and dynamic rules outlive a
  browser restart, so a launcher that does not answer leaves the last set in force. A
  set the launcher built without the parent's words (`complete: false`) never replaces
  a complete one. No rules is logged as an error, never silently.
- **The parent's words** are one list for the family, under **Çocuklar → İnternet →
  Gelişmiş**, where a parent looks, with a sentence saying it covers every child and
  that a list already exists: the browser and its policy are one per machine (D-167),
  and a word a parent does not want one child of four to ten to search is rarely one
  they want another to. Stored as the `search_words` setting, audited as
  `search_words_changed`; a word under four letters closes only itself, a longer one
  the words that begin with it.
- **"Büyüğüme sor"** files a request of the new kind `search`, which carries nothing:
  the parent reads "Elif kapalı bir şeyi aramak istedi" and answers "Tamam,
  konuşacağız"; there is nothing to allow, and the child hears the kind "not now".
- **The address bar** searches Google by policy (`DefaultSearchProvider*`), where
  SafeSearch is forced and the rules see the search; its suggestions are off
  (`SearchSuggestEnabled`), because the browser fetches them itself, where no extension
  can see them; and nothing is preloaded (`NetworkPredictionOptions: 2`). The policy is
  the machine's, so the parent's Chromium gets these too.
Not closed: a site whose search box posts a form, the words inside YouTube's in-page
search request (the results page is still closed once the address changes), and a
spelled-out word in a suggestion request written with `%20`. Nothing about a search is
recorded or reported: no new outbound connection, no new record. *Amended by D-222:* a
refused search's words are kept for the parent, and the block page says so.

Amended the same day, after the VM: the first search in a freshly started browser went through, in the seconds before the launcher's rules arrived. The default words are now also a static ruleset, `rules/defaults.json`, generated by `biokids-admin pack-extension` on the machine (`extension_pack.default_rules_json`) and declared in the manifest, so they hold from the browser's first page; an allow rule for the launchers' ports 7879-7978, never 7877, keeps the block page reachable and Vikipedi filtered. Extension 1.2.0.

**D-203 [owner] Something new every day, more to learn with, and a Dünya that works.** (2026-10-08)
The owner: a child who sees the same things every time gets bored. Three changes.
- **Bugünün sürprizi.** A strip under the launcher's header with one card a day: a
  fact (Biliyor muydun?), a riddle (Bilmece) whose answer opens with a tap, an animal
  (Günün hayvanı), a word (Bugünün kelimesi) or a task (Bugünün görevi), which may be a
  button to one of the child's own tiles. 178 cards in twelve weekly themes - space,
  animals, seasons, ocean, numbers, dinosaurs, plants, the body, the poles, insects,
  Türkiye, birds - in `biokids_core/data/daily/daily.toml`: Turkish written first,
  only settled facts (approximate numbers say so), nothing frightening, the child
  addressed as sen, a task done in a room or with a grown-up. The loader refuses a card
  in one language, a riddle without an answer or with a picture that gives it away,
  a task for anything but a catalogue app. Chosen, never generated - the assistant is
  D-165's, behind its own gate - and offline: the week (Monday to Sunday) picks the
  theme, the same for siblings; inside it each kind is spread evenly through the deal;
  the day of the week walks the deal, seven cards further on each time the theme comes
  round (every twelve weeks), and the child's id moves the start, so siblings differ.
  No card repeats within twelve weeks. A task is dealt only to a child whose tile it
  names can start. 4-7 get one sentence, which Zio reads on a tap (D-175's
  `data-speak`); 8-10 a sentence more. Drawn by the server, so it reads with scripts
  off; ✕ puts it away until tomorrow (the date, in the child's own browser storage,
  nothing else). Measured at 1280 x 800: the young band's two rows of 220-pixel tiles
  and the older band's three rows of 150 still fit under it, which is why an 8-10 card
  is held to two lines (a test).
- **More to learn with.** From trixie main, each offline, with a Turkish interface,
  starting in a window: Marble (Dünya, 6-10), KTuberling (Patates Kafa, 4-6), Blinken
  (Hafıza, 5-7), KGeography (Haritalar, 8-10, with Türkiye's provinces) and KBruch
  (Kesirler, 9-10); Palapeli (Yapboz), KTurtle (Kaplumbağa) and GNOME Chess (Satranç,
  with its own engine) are offered but not ticked, so the default screens do not
  grow. Recommends of biokids-desktop; separate programs under the GPL and LGPL, so
  D-30 holds. Not taken: gbrainy and gtans (not in trixie), Tux Typing (no Turkish
  words; Klavye exists), KHangman and Kanagram (no Turkish word lists offline),
  KLettres (no Turkish letters), Stellarium (D-164 dropped it).
- **Dünya is Marble.** Google Earth on the web needs WebGL; on a machine without
  graphics acceleration (the VM, an old laptop) it says only "WebGL is not supported".
  Marble draws its globe itself, with QPainter, from maps its package installs, so it
  works offline and anywhere. `google-earth` stays as "Google Earth", 8+, offered but
  never ticked. A slug is never reused, so Marble is `marble`; the catalogue's new
  `inherits = ["google-earth"]` makes `sync_catalog`, the first time Marble's row is
  added, give it to every child who had Google Earth enabled, in its place and with its
  category, in the audit log as a change by the system. It happens once per database:
  a parent who unticks it is not overruled by the next upgrade. Nobody loses a tile;
  the old one stays where it was.

**D-204 [claude] The block page in the child's browser works, end to end.** (2026-10-11)
On the VM the owner searched for a listed word and pressed "Büyüğüme sor": a blank page.
Four faults, each found by doing it in a real Chromium, which the tests' client never did:
- **The launcher refused its own forms.** Every launcher page said `<meta name="referrer"
  content="no-referrer">`, and under that policy Chromium sends `Origin: null` with a form
  a page posts; the same-origin guard (D-171) answered 403. "Büyüğüme sor", "Biraz daha
  süre iste", "Evet, kapat" and the first-run screen were all affected; the `fetch` calls
  (launching, the sound, the assistant) send their origin whatever the policy, which is
  why it went unseen. The pages now say `same-origin`, so other sites still learn nothing
  of the launcher's address and a form carries its origin. The guard also accepts
  `Origin: null` when Sec-Fetch-Site, which no page can set, says same-origin (a page left
  open across the upgrade); a sandboxed frame on a website says cross-site and is refused.
- **"Geri dön" opened the launcher inside a browser tab.** In the browser (a closed site
  or a refused search, which the extension sends there) it now goes to the child's start
  page, `/start`, and so does the button after asking. Not the browser's Back: the entry
  before is the error page of the address just closed, which closes it again.
- **A refused search could stay on Chromium's "blocked by an extension" page.** The
  extension finds its launcher by asking every launcher port for `/healthz` within 1.5 s,
  and the launcher's `/healthz` waited for the daemon first; on a busy machine starting
  the browser it missed, backed off for 15 s, and left Chromium's page. `/healthz` now
  answers at once (the daemon gets half a second), the probe waits 5 s, and a block page
  waiting for its launcher asks again after 1, 3 and 6 s, past the back-off. Extension
  1.2.1.
- **A new profile invited the child to sign in to Chromium** although sign-in was off;
  the policy also sets `SigninInterceptionEnabled` and `PromotionsEnabled` to false.

**D-205 [claude] The app bar fits the window it sits on; a new window fills the screen.** (2026-10-11)
The owner on the VM: "on some small things this bar stays far too big". It was 544 pixels
wide whatever was under it: over Palapeli, open in a window a little wider than that, it
covered nearly the whole title bar; over Frozen Bubble's fixed 640 x 480, most of it.
- **Three sizes** (`appbar.bar_tier`), chosen each time the bar is placed from the width
  of the window under it, in CSS pixels and only the part on the monitor: the whole bar
  (544 px) from 1000 pixels; a compact one (284 px) from 520 - buttons 48 wide and the
  title bar's 64 tall with 44-pixel faces, the minutes in a smaller type without the
  clock, 🔉 and 🔊 without the slider; below that 🏠 and ✕ alone (96 px). Each threshold
  is the narrowest window over which that bar takes no more than 55%, so every app that
  fills a screen 1024 or more wide keeps every control and Frozen Bubble gets the compact
  bar. The compact targets are UX.md §2.4's one exception to 64 x 64 for 4-7.
- **Only on a change, and nothing feeds back.** The tier is a CSS class on the bar; a
  change hides what the tier does not show, has GTK measure again, asks the window for
  1 x 1 (a GTK 3 window does not shrink by itself) and places the bar by its new width.
  The tier depends on the app's window alone, so the bar's own size cannot change it. The
  minutes' box shows only while it holds something - not while the launcher cannot say
  the minutes (the daemon restarting during an upgrade) - so the compact bar has no gap.
- **A new window fills the screen** (`appbar.Filler`). devilspie2 maximises every
  ordinary window as it maps (D-163), and KDE's apps then put back the size they saved,
  as Palapeli did. The first time a window is in front it is maximised if it is an
  ordinary window that can be and is not. When libwnck reports it leaving maximisation
  with no finger down, or from 1.5 s on if nothing is reported, it is looked at again
  and maximised once more, until 6 s after it was first seen: at most twice, then
  never. Never a dialog, a fixed-size window, a window under a finger, or one open
  before the bar started; and the bar stays off a window it asked to maximise until the
  window settles, so it is never shown on the small window first.

**D-206 [claude] Insights: the week in plain words.** (2026-10-11)
The owner asked for analysis in the parent panel. A parent who is not technical
(principle 6) wants to know whether anything changed, where the time went and whether to
do something about it, not a dashboard. **Analiz** (`/insights`), in the menu and a tap
from **Geçmiş**, shows one child for **Bu hafta** (Monday to Sunday, the default),
**Geçen hafta** or **Son 30 gün**: at most five highlights first, then the numbers as
tiles and bars the server draws - a bar per day against its quota, where the time went by
kind of app, the apps with "daha çok", "daha az", "aynı" or "yeni", the hours of the day,
the requests and the sites. No script, no chart library (D-129). Today gets a "Bu hafta"
card with three highlights per child, and the Sunday mail (D-177) ends each child's week
with the same three; it never names a site.
- **Rules, not a model.** `biokids_core.insights.highlights` is a list of weighted rules,
  each a catalogue key with parameters: an assistant alert, a refused search the child
  asked about, a change in the daily average, the time running out on two days or more,
  use late at night, requests that expired unanswered, where most of the time went,
  minutes earned, weekends far from weekdays, many asks for time, the busiest hours, a
  week the time never ran out. A highlight says what happened, never what it says about
  the child; a test refuses judgement words. Some carry one gentle suggestion and a link
  to the setting that acts on it - a games limit, earned time, the allowed hours, the
  alert mail - and say nothing once it is on.
- **Honest arithmetic.** Today counts in totals, never in an average or a comparison; a
  period still under way is compared with the same days one period back. Days before
  BioKids knew the child are left out, not counted as zeros. A comparison needs ten
  minutes a day on average over at least three days on the earlier side (or as many as
  have finished), and a rise beyond +200% is said in words ("iki katından fazla") - a
  child added last Friday is not "up 900%". "Yeni" is decided against the whole period
  before. Late use is counted in nights, 00:00-06:00 belonging to the evening before. A
  request still waiting is "bekliyor", never "cevapsız": only one that expired unanswered
  is. Weekdays and weekends are set side by side only from two finished days on
  each side: on the VM a Sunday made "the weekend" one Saturday the machine was off.
  The day's time ran out on the days the daemon wrote `quota_exhausted`.
- **When in the day.** Nothing recorded it, so: `usage_hour(child_id, day, hour,
  seconds)`, migration `077ca4545f23`, filled by the ledger at the one place it adds to
  `general_s` (split at the hour, never across midnight), after the ledger's commit in a
  transaction of its own: a failure costs the chart, never a tick (D-139), and nothing
  that enforces reads it. It is the most personal of the aggregates, so it is kept for
  `activity_days` (30) with the sites, not `usage_days` (D-189).
- Everything else is computed when read, one indexed query per table. The intro says
  plainly what leaves the machine: nothing, or, with the weekly mail on, the week's
  minutes and highlights to the parent. Extends D-37 and D-192.

**D-207 [claude] A parent sees which apps an upgrade brought.** (2026-10-11)
D-203 added eight apps, and a child who already existed got none of them but Marble
(through `inherits`): a family's tiles are the ones its parent ticked (D-164), and nothing
told the parent the new ones were there.
- `sync_catalog` writes each slug it adds, with the time, to the setting
  `catalog_new_apps` (JSON; no schema change), for thirty days - but nothing on the first
  sync into an empty table, a fresh install, where every tile would be new. A time in the
  future (a wrong clock during the upgrade) is brought back to the present.
- **Çocuklar → Uygulamalar** puts "Yeni" beside each such tile, and one line names those
  that suit the child (what a new child of that age would be given), can start on the
  machine and are not ticked: "Bu çocuğun yaşına uygun 2 yeni uygulama henüz ekranında
  yok: Patates Kafa, Hafıza. Eklemek için işaretleyin." **Çocuklar** shows it under the
  child as a link to that screen.
- A parent's save of that child's apps after the apps arrived answers the line (the
  newest `apps_changed` row by a parent); the hand-down's `system` rows, a save from
  before the upgrade and another child's do not. The "Yeni" stays its thirty days.
  Nothing is ticked for the parent: the child's screen changes only by D-203's
  hand-down. A machine that already had these apps shows nothing new (VM-RUNBOOK §8.12).

**D-208 [claude] Opening an app is answered at once, on its tile, and never twice.** (2026-10-11)
The owner on the VM: whatever the app, "Yapboz açılıyor" stayed up "for a very, very long
time". That session's journal had palapeli-3, -4 and -5 running at once. Measured: the
POST that starts an app took 1.59 s, all but 0.09 s of it a fixed wait; Palapeli's window
came 6.9 s later; a second tap on an open app took 1.46 s, because `bring_back` started a
new Python that loaded GTK and libwnck every time, and on a busy machine it ran out of
its 5 s and `_open_again` took "could not look" for "not running" and started another.
- **No screen in between.** A tap on a tile, an "Açık" button or the day's card posts
  `/launch/<slug>` from the home page. The tile gets a teal ring breathing on its edge and
  "Açılıyor…" where its name was; the rest of the screen stays usable and a second tap
  on it does nothing. It ends when the launcher loses the focus (the app's window took
  it), on a refusal (the calm Blocked screen), or after 25 s; an answer for a ring that
  has already ended shows nothing. Without JavaScript the old screen remains, with only
  the icon, the name and an "Aç" button.
- **Never two copies.** Taps on one tile are taken one at a time, and an app whose scope
  is running is never started again because the bar could not look: only a scope systemd
  says has ended is forgotten. D-197's rule stays - no window 30 s after that very unit
  started, and it is stopped and started afresh.
- **The bar is asked, not a new Python.** The running bar listens on
  `$XDG_RUNTIME_DIR/biokids-appbar.sock` (0600; a peer of another uid is refused): one line
  in, `bring-back <scope> <time>`, the helper's exit code out, served on the GTK main loop.
  A request more than 10 s old by the launcher's clock is dropped, so a frozen bar never
  raises a window long after the tap. The old helper runs only where no bar listens.
- **Answered sooner.** A launch is answered once `/proc/<pid>/exe` shows systemd-run has
  become the app (every 50 ms; 1.5 s is the upper bound), and `report_activity` runs
  after the answer. The launcher keeps watching for the rest of the 1.5 s: an app that
  exits non-zero then is forgotten, and the page, asking `/running` two seconds after a
  yes, gives its tile the calm Blocked screen, as the old wait did.

**D-209 [claude] "Kapat" switches the computer off: answered at once, done after.** (2026-10-11)
The owner: "Bilgisayarı kapat deyince kapansın." The child's "Evet, kapat" asked the
daemon, which stopped the child's applications (each given `app_term_grace_seconds`,
ten by default), wrote the ledger and called `systemctl poweroff`, all before it
answered; the launcher waits five seconds. With an app open the answer came too late and
the launcher showed the home screen as if nothing had been pressed. While anyone else was
logged in - an ssh login included - the refusal looked the same and the child's session
stayed open. The panel's "Kapat" did the same inside its request.
- **Answered first, done after.** The daemon asks logind (at most two seconds), decides,
  audits and answers with what it will do (`ShutdownOutcome`: `poweroff` or `logout`). A
  task the supervisor holds then stops the apps, writes the ledger and, no sooner than six
  seconds after the answer, calls `systemctl poweroff`. The parent's poweroff and restart
  (D-195) work the same way. A second "Kapat" while one is under way starts nothing; a
  restart asked for while the computer is switching off (or the reverse) is not done and
  says so, rather than answering "restarting" and powering off.
- **The child is told.** "Bilgisayar kapanıyor…" and "Yarın görüşürüz.", said aloud for
  4-7 (D-175). Should the machine stay up after all, the page goes back to the launcher
  after a minute: never a child in front of "switching off" with nothing to press.
- **Someone else at the machine: the child's session ends instead.** D-100's protection
  stays - a parent who switched user, or a sibling, never loses their session - but
  "Kapat" closes what the child has open: their apps stop, then `loginctl terminate-user`
  for the child alone, after ten seconds so the longer sentence ("Bilgisayarda başka biri
  de var, o yüzden bilgisayar açık kalıyor. Senin oturumunu kapatıyorum.") is heard. Nobody
  is named. Only a login on a seat counts as someone at the machine: an ssh session is a
  grown-up at another one, and counting it kept the computer on. Who is logged in is asked
  again just before the machine goes.
- **No silent failure (principle 3).** A poweroff that fails ends the child's session, or
  locks it; a parent's that fails locks every child's screen; each writes an alert the
  mail carries. A launcher that cannot reach the daemon says "Bilgisayar kapanamadı. Bir
  büyüğüne söyle." with a way back. A daemon stopped during the wait cancels what is left:
  an upgrade must not end in a poweroff.
- The audit log's `logout` read "Panelden çıkıldı"; only the daemon writes it, for a
  child's session, so it reads "Çocuğun oturumu kapatıldı".

**D-210 [owner] Rows that look full: a game, a site, and centred rows.** (2026-10-11)
The owner, at a six-year-old's launcher on 1600 x 900: the home grid ended in a row of four
under rows of six, and the browser's start page one short. "Böyle boş kalmış gibi
gözüküyor, kötü gözüküyor." One more thing on each, they asked.
- **Elmaslar** (KDiamond, games, 4-10): swap two diamonds, three of a colour vanish - in
  trixie main, offline, in Turkish, in a window of its own, nothing to read. **Müzik
  Atölyesi** (Chrome Music Lab, learning, 4-10): fourteen music experiments, free, no ads,
  no account; its menu is English but nothing needs reading. Song Maker opens in a frame
  from gweb-song-maker.appspot.com, which its `allow` lists for "only the sites I choose";
  scripts and fonts are not pages and the URL filter leaves them alone (D-167). The
  experiments that listen cannot: the microphone is the launcher's alone. Both are ticked
  for a new child; an existing one gets them when a parent ticks them (D-207).
- **Not Ri-li**, the toy train first proposed: no command-line options, full screen
  unless its own settings file says otherwise (D-163), no Turkish, a flag picker on its
  first start, and in Debian's build a question about a human-rights article between levels.
- **Tux Paint's stamps.** biokids-desktop recommends tuxpaint-stamps-default (GPL-2 data
  of a separate program, D-30): thousands of animals, foods, plants and vehicles instead of
  six of Tux. Among them are a few military ones and Halloween's graves; a parent who minds
  removes the package.
- **Every row is centred**, on the home grid and the start page alike: a wrapping flexbox
  of fixed-size tiles instead of a grid that holds the last row to the left. Home keeps six
  220-pixel tiles a row at 1600; the start page's young tiles are 200 pixels, seven a row.

**D-211 [owner] Çıkartma albümüm: a sticker for each day spent learning.** (2026-10-21)
Reward learning, never with screen time. A day earns its sticker with at least 15 minutes
in apps of the learning category - the seconds `usage_tick` already files, with each
tile's category as the parent set it; games never count; the day is the ledger's local
day. Nothing is stored: `biokids_core.album` reads `usage_tick` for past days and the
ledger for today, and the shell state carries `album`. 64 emoji in eight sets; the date
picks one (`ordinal × 9 mod 64`), so none repeats within 64 days. ⭐ and the count beside
the child's name open `/album`: this week as seven circles, a sentence a parent can read
aloud, the stickers, and the child's own learning tiles. Zio celebrates a new sticker
once a day (the date is kept in the child's home). Analiz mentions two or more stickers.

**D-212 [owner] Resimlerim: the pictures a child made, on their own launcher.** (2026-10-21)
A built-in tile (`drawings`, 4-10, suggested) that is a page of the launcher, which runs
as the child: Tux Paint's `~/.tuxpaint/saved/` (not its thumbnails) and the Pictures
folder (`XDG_PICTURES_DIR`, `~/Resimler`, `~/Pictures`), top level, newest 500. A picture
is a folder key and a plain file name; never a link, owned by the child (checked on the
open file), PNG/JPEG/GIF/WebP/BMP by name and first bytes, never SVG; another site's
fetch is refused. A grid of big thumbnails and a full-screen viewer with 84-pixel ‹ › ✕.
Nothing about the pictures reaches the daemon, the database or the panel.

**D-213 [owner] Three more weeks of surprises: online safety, feelings, jobs.** (2026-10-21)
Bugünün sürprizi (D-203) gets **İnternette güvenlik**, **Duygularımız** and **Meslekler**,
fifteen cards each in the others' mix (223 in all). Online safety says what to do and whom
to tell, never what could go wrong; a test keeps words of danger out of it. The rotation
is fifteen weeks; the week of an upgrade may change theme once.

**D-214 [owner] Good night: an evening's ending is bedtime.** (2026-10-21)
When the day ends at night - its last allowed window closes, or its time runs out, at
19:00 or later (or before 05:00) - the last ten minutes and the end screen are in a calm
night tone; when and how the day ends do not change (a test compares every command the
daemon runs at 16:00 and at 19:50). `policy.bedtime_in` decides it, counting only what
ends a day: never a gap between windows (D-143), a limit per kind of app or a break.
- The engine asks for `bedtime_10/5/1` and `bedtime` instead of `warn_*` and `time_up`,
  Zio sleepy; a machine without the night lines, or a parent's own recording of the day
  line (D-174), is used instead. The warnings are drawn in the night's blue, the end
  screen says "İyi geceler, {name}." and "Yarın görüşürüz.".
- The screen dims in four steps to 60% of the child's own level, only while their
  session is on screen, and the level is put back when the evening moves away, when the
  launcher stops, or at the next session (kept in `~/.local/state/biokids/`).

**D-215 [owner] Special days: today, and holidays.** (2026-10-21)
On **Bugün**, "🎉 Bugün özel bir gün mü?" gives today only +1 sa, +2 sa or **Süre sınırı
yok** (no daily quota; the allowed hours still apply). **Süre kuralları → Özel
günler** lists them and adds a range ("Tatil", up to 100 days) that follows the weekend's
rules or a quota of its own. Each is shown, audited and removable.
- Table `day_override` (migration `5d2f8a61c3e4`), read where the day's quota and
  windows are picked, so warnings, the end of the day, Today and history follow. Extra
  minutes add up, "no limit" and "weekend rules" hold if any says so, the later own
  quota wins; granted and earned minutes still add, category limits still apply.
- Never the past: a range starts today at the earliest; cancelling one under way cuts it
  to yesterday, so history keeps the quota those days had. The daemon's new `day_changed`
  lets a child whose day just got longer back in, as "+10 dk" does (D-144), never from
  the filter's lock (D-172); a reload no longer forgets that lock.

**D-216 [owner] "Sor Bana" answers a sentence at a time.** (2026-10-21)
On the VM the model writes about three tokens a second, and a child looked at
"Düşünüyorum…" for twenty to forty seconds. What the assistant allows does not change:
the word list and the classifier run before anything is shown, refusals are catalogue
sentences, alerts and rate limits are as before.
- llama-server streams; `biokids_ai.sentences` splits Turkish sentences (not after an
  abbreviation, an initial, a number or a Roman numeral; long ones at a comma). Each is
  screened with everything shown before it. The first one stopped ends the answer with
  "Bu konuda sana bu kadarını anlatabilirim. Gerisini ailenden bir büyüğe
  sorabilirsin." (self-harm keeps its own sentence with 183 and 112), and the parent is
  told as before. A model that dies, or the answer's 180 seconds running out, ends it
  with "Şu an biraz dinleniyorum".
- Polling, not a streamed reply: `ask` returns once the question is accepted, `more`
  returns new sentences; the page asks every 500 ms through the daemon's
  `ASSISTANT_MORE`, which answers only about the calling child's answer. A launcher quiet
  for 15 seconds has gone; the service stops an answer nobody read for 60.
- Zio reads one sentence at a time (`/assistant/say` answers when the line finished), in
  order and never more than one in the voice service's queue, so a time warning is never
  pushed out.

**D-217 [owner] Text size, high contrast and a reading font, for each parent and each child.** (2026-10-21)
**Ayarlar → Görünüm** for the signed-in parent (on their own row, so it follows them to the
phone): Normal, Büyük, Çok büyük (100/125/150%) and high contrast. **Çocuklar → Düzenle →
Ekran** for each child: the same and OpenDyslexic, sent in the launcher's state and
written on `<html>` of every launcher page. Tokens only: `--font-scale`, a high-contrast
palette measured by a test (words 7:1, buttons 4.5:1, lines 3:1). Two broken hooks in
`tokens.css` were fixed (a self-referencing font stack, a `@font-face` for a file never
shipped). The font is Debian's `fonts-opendyslexic`, a Recommends of biokids-desktop;
without it the usual font returns and Ekran says so.

**D-218 [owner] A second parent, with their own name, PIN and recovery code.** (2026-10-21)
**Ayarlar → Ebeveynler** adds a parent (name, PIN twice, a recovery code shown once) or
removes one, both behind the passphrase; at most four; never the last. With two or more,
sign-in asks "Kim giriş yapıyor?" and then that parent's PIN. Each parent's own: PIN,
lockout, recovery code, sessions, panel look. The family's: the passphrase (one secret
turns protection off), mail and requests. Kayıtlar names who did what, İstekler who
answered. A removed parent keeps their row and name (`removed_at`) with every secret
cleared. `biokids-admin reset-pin --parent NAME`. Found on the way: a second parent could
have replaced the family's passphrase without knowing it, and a wrong current passphrase
on Ayarlar → Parola was never counted; both fixed. Migration `b7e41d2c9a63`.

**D-219 [owner] The first release is 1.5.0.** (2026-10-21)
The owner asked that the version show the work it took, not "1.0.0". 1.0 was the release
candidate built as 0.1.0-2 (D-185); five rounds of the owner's tests followed (D-195 to
D-218), so the first published packages are 1.5.0, Debian revision 1.5.0-1, tag
`v1.5.0`. The ISO comes after, under its own number. Debian orders 1.5.0-1 after
0.1.0-2, so an upgrade is an ordinary upgrade.

**D-220 [owner] A forgotten family passphrase can be set again.** (2026-10-21)
The passphrase guards switching protection off (D-48), and a family that forgot it had no
way back - more likely with two parents (D-218), when the one who knew it removes
themselves. `sudo biokids-admin reset-passphrase` asks for a new one twice (never on the
command line; `--stdin` for scripts), at least ten characters, and sets it on the row
that holds the family's; it is audited as `passphrase_changed` by `cli`. Root on the
machine is the proof, as for `reset-pin`. The gate's five-in-fifteen-minutes count now
starts at the last change, so guesses at the old passphrase do not lock out the new one.
Also, by the owner: the evening of D-214 starts at 19:00, and D-215's taps are +1 sa,
+2 sa and no limit - Today's own "+30 dk" was one too many beside them.

**D-221 [owner] The parent's own account on the login screen has their name and a house.** (2026-10-21)
After the wizard the greeter showed each child by name and picture (D-170) and the parent
as their bare user name, "bilal", beside Debian's grey silhouette: the one account a
family sees as "the computer's", not as a person. When a child is provisioned, and from
`biokids-admin refresh-greeter`, the one account on the machine that is a person's
(uid 1000-59999, a login shell) and not a child's gets the name the parent gave the
wizard as its GECOS name, if it has none or only its user name, and a house picture
(`parent.png`, 🏡) if AccountsService has no picture for it. A house is not among the
children's animals, so the grown-up's login never looks like a child's. With two or more
such accounts nothing says which parent ran the wizard, and nothing is changed. The user
name and the password are never touched, and a name or photo the parent set themselves
is kept. It runs in biokids-provision@, root and outside the panel's sandbox, like the
children's names.

**D-222 [owner] A refused search reaches the parent, by its words.** (2026-10-22)
D-202 refused a listed search and kept nothing; "Büyüğüme sor" filed a request that named
nothing. The owner wants the parent told - "çocuğunuz porno kelimesini arattı, dikkatli
olmanızda fayda var" - in Analiz. So, and no further:
- **The extension** (1.3.0) sends the address of a page or frame its rules refused, never
  a suggestion fetched while typing, to the child's own launcher, `POST /search-refused`
  `{"url"}`, after it has shown the block page; the same for YouTube's in-page search.
- **The launcher** accepts it only from the extension's own origin, checks the address
  against its own copy of the same expressions (`search_filter.refused_search`), so
  nothing else can become something a parent reads, and passes on only the words
  searched for - the search box's value, or a wiki title, or the last part of the path,
  decoded and at most 100 characters (`searched_words`) - and the site's name, over the
  new shell op `report_search`.
- **The daemon** keeps them as an `ai_alert` with `app = "search"` beside the
  assistant's (D-165): pruned with the child's other activity (D-189), at most 20 a day
  per child, the same words again within ten minutes counted once. Its audit row,
  `search_refused`, names the site and never the words: the audit log outlives them.
- **The parent** sees a banner on Today ("Ahmet uygun olmayan bir şey aramaya çalıştı
  (2 kez)"), a highlight in Analiz (weight 95, after the assistant's), and there an
  "Engellenen aramalar" card with each search's words and time, a few sentences of advice
  - curiosity is natural; talk calmly, without scolding - and "Gördüm", which clears the
  banner. The alert mail says that one happened and where to read it; the words never go
  in a mail.
- **The child knows** (principle 2): the block page, read aloud by Zio, now says "Bu
  aramayı bir büyüğün de görecek. Merak ettiğin bir şey varsa ona sorabilirsin."
Every other search stays unrecorded; `docs/SECURITY.md` §5 and §7 say so.

**D-223 [owner] The natural voice by default.** (2026-10-22)
The owner: espeak-ng "sounds like a robot" to a child, and 90 MB is not much. The voice
still may not ship in a package (D-137), so the family still fetches it from its authors
(D-196) - now from the wizard: step 9 has "Zio ve Sio doğal, insan gibi bir sesle
konuşsun (yaklaşık 90 MB indirilir)", ticked for a Turkish family and not for an English
one (the voice is Turkish), with where it comes from and its licence in one sentence.
Finishing the wizard with it ticked sets the setting `natural_voice = wanted`, audits
`voice_download` (`via: wizard`) and starts the download; the button on /voices sets the
same. While it is wanted and not on the machine, the daemon starts
`biokids-voice-setup.service` again every 30 minutes, the first time two minutes after it
starts (`Supervisor.fetch_voice`), so a computer set up without a network gets the voice
the first half hour it has one; it audits nothing, or an offline machine would fill the
log. /voices says it will be tried again instead of "İndirme tamamlanamadı".
`biokids-admin voice-setup remove` clears the setting, or the voice would come back.

**D-224 [owner] Step-by-step guides.** (2026-10-22)
The owner: some parents will not manage the mail settings, and the help needs more
detail. **Yardım** now starts with "Adım adım rehberler": e-posta, telefondan erişim,
çocuk ekleme, süreler, internet ve aramalar, Zio ve Sio'nun sesi, unutulan PIN ya da
parola. Each is `/guides/<slug>`, open before signing in like /help: numbered steps in
the screens' own words, the commands to type where there are any, "Sorun mu var?"
questions, and once signed in a button to the page it is about. The mail guide walks
through Gmail's 2-Step Verification and app password, gives the servers of Yandex and
iCloud, and says plainly that personal Outlook and Hotmail accounts can no longer send
this way (Microsoft turned it off; they can still receive). The mail card links to it.
A failed test mail now says which kind of failure (`mail.failure_kind`): the user name
or password refused ("Gmail normal şifrenizi kabul etmez…"), or the server not reached
(port and security must match), and the spaces Google shows inside an app password are
taken out when the server is Gmail's (`mail.tidy_password`). Every guide's catalogue
entries are checked in both languages by a test.

**D-225 [owner] The English corner.** (2026-10-22)
The owner saw a Chinese child speaking fluent English learned from a computer and asked
for the same. What the research and the families who manage it agree on:
- **A lot of English the child understands, every day.** Chinese families call it
  磨耳朵, "grinding the ears": songs, picture books and stories heard over and over,
  pictures making the words clear, chosen so most is understood; phonics and reading
  only once there is a spoken vocabulary to read
  ([sina](https://www.sina.cn/news/detail/5296623414674020.html),
  [banyu](https://www.banyu.com/blog/english/148636/),
  [biteabc](https://m.biteabc.com/zxwj/9944.html)).
- **Being answered, not watching.** Young children learn little language from passive
  video ("video deficit") and much more when what is on the screen responds to them; a
  conversational agent that asks and listens matches a person on vocabulary and
  comprehension in storybook studies
  ([PNAS](https://www.pnas.org/doi/10.1073/pnas.1611621115),
  [Xu et al.](https://pmc.ncbi.nlm.nih.gov/articles/PMC9299009/),
  [BJET 2025](https://bera-journals.onlinelibrary.wiley.com/doi/10.1111/bjet.13615)).
  The Chinese apps that grew out of this (Zebra English, for 2-8) are short animated
  lessons the child answers aloud, with instant, encouraging pronunciation feedback.
- **Words met again just before they are forgotten.** Spaced retrieval beats massed
  practice for children's word learning
  ([ASHA 2026](https://pubs.asha.org/doi/10.1044/2026_JSLHR-25-01039),
  [pooled study](https://pmc.ncbi.nlm.nih.gov/articles/PMC8740735)).

So BioKids gets **İngilizce**, a built-in tile (`english`, learning, suggested for 4-10)
that opens a page of the launcher:
- **Content** (`biokids_core/data/english/units.toml`, written for BioKids, MIT): twelve
  units a child knows - animals, colours, numbers, family, food, body, clothes, home,
  nature, actions, feelings, transport - 98 words with an emoji picture each, 36 short
  lines ("Can I have water, please?") and a six-picture story with two questions per
  unit.
- **A lesson a day** (`biokids_core.english.plan_lesson`): due words found again first
  (hear it, tap one of three pictures), then up to four new words of the current unit
  (seen, heard twice, said back, then found), up to eight a day; once a unit's words are
  met its lines are heard and said back, then its story is read aloud and asked about.
  Every word, line and story sits in a Leitner box (1, 2, 4, 7, 14, 30 days); right the
  first time goes up a box, wrong goes back to the first, and nothing else happens: the
  word is said again and the child tries again. "Biraz daha oyna" afterwards finds met
  words again without moving a box.
- **Spoken, always**: instructions in the child's language by Zio, English by the
  English voice, each line answered only once said (`/english/say` waits like the
  assistant's, D-216). Praise is English too ("Well done!"), so the corner's most heard
  phrase is one worth knowing. The young band sees no Turkish under the word.
- **Saying it back** where the parent let the child speak to the assistant (D-166): the
  same held button, the same 16 kHz WAV dropped once heard, now with `language: en`;
  `english.heard_matches` is generous ("a cat", "kat"). Without the microphone the child
  presses "Söyledim" after saying it - listening and repeating still works.
- **Its minutes are learning minutes**: the page reports itself every 45 seconds while in
  front, under the tile's category, so it earns time (D-45) and stickers (D-211).
- **The voice**: Piper's `en_US-ljspeech-medium` (LJ Speech, public domain), pinned by
  SHA-256 like the Turkish voice and downloaded with it (D-196, D-223): the box and the
  button now say about 153 MB. Zio speaks it as `en-zio.onnx`; Sio keeps espeak-ng's man
  in English. A family that has only the Turkish voice is offered "İngilizce sesi indir
  (yaklaşık 64 MB)", and a wanted voice is fetched again until both are there.
- **Progress** in a new table, `english_item` (child, item, box, due, first day, right,
  wrong; migration `e3a9c75d1f08`), kept as long as the child is: what they have learned,
  not a log. The launcher reads and writes it through two shell ops, `english_state` and
  `english_answer`, which accept only the corner's own ids.
- **For the parent**, Analiz has an İngilizce card - words met and known well, new words
  and days this period, the unit now, the known words, a tip (use the words at home) and,
  without the English voice, a pointer to it - and a highlight (weight 47) for new words.
- **Two English websites** as web tiles a parent may tick, not suggested: British
  Council's LearnEnglish Kids and Pratham Books' StoryWeaver (CC BY 4.0, levelled
  read-along books).
Not now: a conversation in English with the assistant, the next step the research points
to; phonics and reading, for when a child has the spoken words to read.
