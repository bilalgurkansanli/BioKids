# DECISIONS.md

Every decision that shapes BioKids, with the reason. Two kinds of entry:

- **[owner]** — decided by the project owner. Do not change without asking.
- **[claude]** — decided on the owner's behalf ("sen karar ver"). Reversible, but
  read the rationale before reversing, and update this file if you do.

---

## Scope

**D-01 [owner] Target age is 4–10, not 0–13.**
An 11–13 year old is a different product and a much harder adversary (live USB, GRUB
edit, VM). Narrowing the range makes the threat model honest and the UI coherent.

**D-02 [owner] Multiple child profiles, configurable.**
One Linux user account per child. Policies are per-child, not per-machine.

**D-03 [owner] Open source (MIT), public, portfolio piece.**
MIT is chosen deliberately over GPL: it keeps the door open, and it forces us to avoid
GPL-3 libraries in-process. See D-30.

**D-04 [owner] Name: BioKids.** Package prefix `biokids-`. The control subsystem is
referred to as "BioGuard" in docs and UI, but ships as `biokids-daemon`.

**D-05 [owner] UI in Turkish and English. Code, comments, commits and docs in English.**
i18n from the first line. Turkish is the default locale on the ISO; English is the
default for the source tree.

**D-06 [owner] No deadline.** Correctness over speed. This is a learning project first.

---

## Base system

**D-10 [claude] Debian 13 "Trixie", stable.**
Testing breaks weekly and would make every bug report unreproducible. Stable gives us a
frozen Python (3.13), a frozen XFCE (4.20), and predictable security updates. When
Trixie goes oldstable we re-target in one planned sprint.

**D-11 [claude] amd64 only for v1. No ARM64 / Raspberry Pi.**
ARM doubles the build matrix and testing surface, and the AI layer expects a discrete
GPU. Revisit only after the ISO is stable. A Pi image is a good v2 headline feature.

**D-12 [claude] X11 only. Wayland is explicitly out of scope.**
This is the single most consequential technical decision in the project. Everything we
need — reading the active window, taking a screenshot of another process, global
hotkey suppression, forcing a kiosk window — is trivial on X11 and either hard or
impossible on Wayland without per-compositor portals. XFCE 4.20's Wayland support is
experimental anyway. `/etc/lightdm/lightdm.conf` pins the X11 session. Document this as
a known limitation, because it will eventually need solving.

**D-13 [claude] XFCE 4.20 as substrate, but the child never sees it.**
Comparison:

| | XFCE | LXQt | GNOME Kiosk |
|---|---|---|---|
| RAM at idle | ~500 MB | ~400 MB | ~900 MB |
| Config surface | xfconf, plain XML | Qt/INI | GSettings + JS extensions |
| Lockdown | mature, well documented | thinner docs | strong, but heavy and opinionated |
| X11 | native | native | Wayland-first |

XFCE wins on 4 GB RAM, on X11, and on how well its lockdown is documented. But the real
decision is that **we do not try to make a desktop environment child-friendly.** We
build our own fullscreen launcher and XFCE just holds the session together. Fighting a
DE's defaults is where every previous kids distro (Qimo, DoudouLinux, LinuxKidX) burned
its energy before dying.

**D-14 [claude] Child launcher is a web app in Chromium kiosk, not a native GTK app.**
`biokids-shell` serves HTML on `127.0.0.1:7879`; a dedicated Chromium instance opens it
with `--kiosk --app=` and a separate `--user-data-dir`. Reasons: the owner is a web
developer and will iterate on it ten times faster; making it cute is CSS instead of GTK
theming; it shares templates and i18n with the parent panel. Cost is ~250 MB RAM, which
fits the 4 GB target.

Escape prevention for that instance: its own managed policy with
`URLAllowlist: ["127.0.0.1:7879"]`, `URLBlocklist: ["*"]`, all keyboard shortcuts
disabled, no developer tools, no context menu.

**D-15 [owner] Minimum hardware: 4 GB RAM.** Target idle footprint under 1.2 GB with
shell + daemon + panel running. 2 GB is explicitly unsupported. Disk: 25 GB.
The AI layer needs a discrete GPU and is never installed by default.

**D-16 [owner+claude] Both live and installable ISO, but sequenced.**
Phase 3a ships live-only. Phase 3b adds Calamares. Getting a live image to boot is a
weekend; getting an installer to partition correctly on real hardware is a month.

**D-17 [claude] Secure Boot: inherit Debian's signed shim, do not sign anything ourselves.**
We ship no custom kernel and no out-of-tree modules, so Debian's signed `shim` +
`grub-efi-amd64-signed` should chain-load fine. If it doesn't, we document "disable
Secure Boot" rather than buying a signing key. Verify on real hardware before claiming
support.

**D-18 [owner] Full offline operation required.** Blocklists are bundled and refreshed
opportunistically. dnsmasq resolves locally. TTS runs locally. The VLM runs locally.
With no network, the machine is still usable and still protected.

**D-19 [claude] Split update policy.**
`unattended-upgrades` enabled for Debian **security** updates only. BioKids' own
packages never auto-update: a silently updated parental-control daemon that regresses
is worse than a stale one. The panel shows "Update available" and the parent clicks.

**D-20 [claude] Bundled application catalog.**
Core: GCompris, TuxPaint, TuxMath, Klavaro (typing), Stellarium, KolourPaint,
SuperTux, Frozen Bubble, gnome-mahjongg. For 8–10: LibreOffice Writer + Calc, Thunar
(home only), Scratch via allowlisted `scratch.mit.edu`.
Turkey-specific: **EBA (eba.gov.tr) allowlisted by default** — it is the natural target
of the reward system (D-45).
Apps are metadata in `/usr/share/biokids/catalog/`, not hardcoded. Parent picks from
the catalog; unpicked apps are not shown to the child.

**D-21 [owner] Chromium, not Firefox.** Enterprise policy via
`/etc/chromium/policies/managed/*.json` is far more capable than Firefox's
`policies.json`, and the managed-extension mechanism we need for URL reporting is
Chromium-native.

**D-22 [claude] No Steam, no emulators, no Flatpak in v1.**
Each is a sandbox escape route and a support burden. SuperTux and SuperTuxKart from
Debian repos are enough. Flatpak specifically would let a child install an unmanaged
browser; keep it out.

**D-23 [owner] Account model.** Child: normal user, no sudo, no polkit admin, locked
into the shell. Parent: separate account with sudo and a normal XFCE desktop.

**D-24 [claude] Parent can log in graphically and also manage from the panel.**
The panel is the primary interface; the parent desktop exists for real work and for
recovery.

---

## Packaging and distribution

**D-25 [owner] Phase 0 = `.deb` only. ISO comes later.** See `docs/ROADMAP.md`.

**D-26 [owner] Single monorepo:** `github.com/bilalgurkansanli/BioKids`.
One Debian *source* package producing several *binary* packages.

**D-27 [claude] We run our own APT repository, built with `aptly`, hosted on GitHub Pages.**
Free, HTTPS, no server to maintain, and it integrates with GitHub Actions. The repo
signing key lives in GitHub Secrets; the public key ships as `biokids-archive-keyring`.
Later, CNAME it to `apt.biokids.bilalgurkansanli.com` — the repo URL should be the
custom domain from day one so we never have to migrate users.

This matters more than the ISO: with an APT repo, a user can adopt BioGuard on their
existing Debian install in two commands, and we can ship updates without rebuilding an
image.

**D-28 [claude] ISO hosting: archive.org primary, GitHub Releases secondary.**
GitHub caps release assets at 2 GB per file and the full image will be close to that
line. archive.org is free, unmetered, permanent, and hosting a Linux ISO there is
entirely normal. Every release: GitHub Release holds the `.deb` files, `SHA256SUMS`,
a detached GPG signature and a link; archive.org holds the ISO. Mirror the ISO on
GitHub Releases too whenever it fits under 2 GB.

**D-29 [owner] GitHub Actions CI from the start.** See `docs/PACKAGING.md`.

**D-30 [claude] License hygiene is a hard constraint.**
MIT in, MIT-compatible dependencies only for anything we link or vendor. GPL software
invoked as a separate process (dnsmasq, Chromium, Piper's binary) is fine. This rules
out two things people would otherwise reach for: **timekpr-next** (GPL-3, see D-40)
and **XTTS-v2** (CPML, non-commercial, see D-62).

---

## Parent panel

**D-31 [owner] FastAPI + Python 3.13. SQLite with WAL.**
One language across panel, daemon, voice and AI worker.

**D-32 [claude] Jinja2 + hand-written CSS. No React, no npm, no CDN.**
*Revised by D-129: HTMX is named below but was not used. The constraints stand; the
library does not.*
The panel is forms and tables. A build step would mean shipping node_modules in a
`.deb`, and a CDN would break the offline guarantee. HTMX gives live-updating counters
with a few attributes. Alpine.js may be vendored if a screen genuinely needs local
state.

**D-33 [claude] Bind `127.0.0.1:7878` always. Remote access exclusively via `tailscale serve`.**
`tailscale serve --bg 7878` publishes the panel inside the tailnet over real HTTPS with
a valid certificate, authenticated by the tailnet itself, with no open port, no
reverse proxy and no Let's Encrypt dance. This also settles the HTTPS question: we never
terminate TLS ourselves. If Tailscale is not installed, the panel is localhost-only and
says so.

**D-34 [owner] Responsive web, installable as a PWA.** No native mobile app.
Manifest + service worker, offline shell, "Add to Home Screen".

**D-35 [claude] Panel starts at boot** as `biokids-panel.service`, `Restart=always`.

**D-36 [claude] Home screen = "Today".**
One card per child: remaining time as a ring, current status, what's on screen right
now (app name, not a screenshot), and three big buttons: **+10 min**, **Lock now**,
**Shut down now**. Everything else is one level down. A parent opening this on their
phone must understand it in three seconds.

**D-37 [owner] History and reports.** Daily and weekly usage charts, top apps, top
domains, blocked attempts, AI alerts. Rendered server-side as SVG — no chart library,
no CDN.

**D-38 [owner] Email summaries, parent-supplied SMTP.**
Daily and/or weekly digest, plus immediate alerts for high-risk events. The parent
enters host, port, user, password (encrypted at rest with a key in
`/etc/biokids/secret.key`, 0600). We run no mail service of our own — that would break
the "nothing leaves the device" promise and cost money. Ship a Gmail app-password
walkthrough in the wizard.

**D-39 [owner] No Telegram bot.** Email only.

---

## Time management

**D-40 [claude] Write our own time engine. Do not build on timekpr-next.**
This reverses an earlier recommendation, so here is the reasoning in full:

- timekpr-next is **GPL-3**. Even talking to it only over DBus is legally fine, but it
  makes the dependency awkward for an MIT project and forces a runtime dependency on a
  single-maintainer package.
- We need things it does not do: category-based limits (D-44), earned time (D-45),
  AI-triggered timeouts (D-56), and reporting into our own database. We would end up
  wrapping it and then bypassing it constantly.
- The core is roughly 500 lines against `org.freedesktop.login1`.

But timekpr has years of edge-case handling. We must handle these explicitly and there
are tests for each:

- **Clock tampering (D-41):** all accounting uses `CLOCK_BOOTTIME`, never wall clock.
  Changing the system clock cannot create or destroy quota. Wall clock is used only to
  decide *which day's* quota applies, and a jump larger than 10 minutes is logged and
  flagged to the parent.
- **Suspend/resume:** `CLOCK_BOOTTIME` includes suspend, so we subtract suspended time
  reported by logind rather than counting it as usage.
- **Session state:** only `active` logind sessions consume quota. Locked, switched-away
  and closed-lid sessions do not.
- **Fast user switching / multiple sessions:** quota is per Linux user, summed across
  their sessions, never double-counted.
- **Machine off:** time does not pass. Confirmed by the boottime ledger.
- **Crash during countdown:** the ledger is persisted every 30 s, so at most 30 s is
  lost.

**D-41 [owner] Clock tampering must not work.** Implemented as above.

**D-42 [owner] Parent chooses daily quota, time windows, or both.**
Quota = "2 hours a day". Window = "only between 16:00 and 20:00". Both = the
intersection. Separate weekday and weekend settings.

**D-43 [owner] End-of-time sequence.**
Warnings at **10, 5 and 1 minute** remaining, spoken by Zio or Sio and shown on screen.
At zero: screen locks with a friendly full-screen message and a 2-minute countdown,
then the machine shuts down cleanly. Parent-configurable to "lock only" or "log out"
instead. A visible countdown ring is always on the child's screen (D-43b).

**D-43b [owner] Remaining time always visible to the child.** Small ring in the
launcher corner, green → amber → red.

**D-44 [claude] Per-category limits, not per-app limits.**
Categories: **Oyun / Games**, **Eğitim / Learning**, **İnternet / Web**,
**Serbest / Free**. Each app in the catalog carries a default category; the parent can
reassign. The parent sets a limit per category, not per application. A screen with
four sliders is usable; a screen with thirty is not. Per-app override exists under
Advanced for the rare case.

**D-45 [owner] Earned time.**
Time spent in the **Eğitim** category earns extra time in **Oyun**, at a
parent-set ratio (default 1:1), with a daily cap (default +60 minutes). EBA is the
motivating example. The child sees earned minutes appear with a small celebration, and
Zio/Sio can be configured to announce it.
Guardrails: a session must be at least 10 minutes to count (prevents opening and
closing an app repeatedly), and idle time inside an educational app does not count
(no input for 3 minutes pauses the earn).

**D-46 [owner] Forced breaks, optional, default off.**
Default when enabled: 40 minutes of use → 5 minutes locked. The break does not consume
quota.

**D-47 [owner] Parent can grant +10 minutes and can lock or shut down instantly**,
from the panel, including remotely.

---

## Authentication and hardening

**D-48 [claude] 6-digit PIN, argon2id hashed, plus an optional passphrase for dangerous actions.**
PIN is what a parent will actually use on a phone. Rate limited: 5 failures → 15-minute
lockout, exponential after that, all attempts written to the audit log. Actions that can
disable protection entirely (turn off filtering, uninstall, wipe data) require the
longer passphrase set during the wizard, not just the PIN.

**D-49 [claude] Setup wizard on first run.**
Language, parent account, PIN and passphrase, recovery code, add children with name,
age and avatar, pick apps, set quota, choose filter mode, optional Tailscale, optional
SMTP, optional AI layer, optional voices. Under 5 minutes, every step skippable with a
sane default.

**D-50 [claude] PIN recovery: two paths.**
A 8-word recovery code shown once during setup, and `sudo biokids-admin reset-pin` from
the parent's shell. Both are logged loudly in the audit trail. There is no email reset
and no backdoor.

**D-51 [claude] Panel session: 30 minutes idle, then PIN again.** Re-auth on every
destructive action regardless of session age.

**D-52 [claude] No TLS in our code.** Localhost or Tailscale, per D-33.

**D-53 [claude] Append-only audit log**, surfaced in the panel with filters. Every
policy change, PIN attempt, override, AI alert and daemon restart. Never truncated by
the app; rotated by size with the oldest archived, not deleted.

**D-54 [owner] The child gets no terminal.** No `xfce4-terminal`, no `xterm`, TTY
switching disabled via `logind.conf` + `X11 DontVTSwitch`, `Ctrl+Alt+F*` blocked.
Documented as defeatable by a determined 10-year-old with a phone and Google; see
`docs/SECURITY.md`.

**D-55 [claude] Hardening is opt-in and scripted, never automatic.**
`sudo biokids-harden --check` reports current state; `--apply` sets a GRUB password,
disables recovery mode, disables USB boot where the firmware allows it, and prints an
undo command. Running it automatically at install time would lock people out of their
own machines, so we don't.

**D-56 [claude] USB boot / live media: out of scope.** Firmware-level, not ours.
Documented as a known limitation.

**D-57 [claude] The panel is unreachable from the child session.**
`127.0.0.1:7878` is in the child Chromium profile's `URLBlocklist`, and the PIN gate is
the real control. No additional UID checks over TCP.

---

## Content filtering

**D-58 [owner] DNS-level filtering plus a managed Chromium extension. No MITM proxy.**
A MITM proxy means installing our own CA into the child's trust store, breaking
certificate-pinned apps, and creating an interception point that is itself a risk. Not
worth it.

**D-59 [claude] Local `dnsmasq` as the resolver, with a configurable upstream.**
Local means it works offline and cannot be bypassed by changing a setting. Upstream
default: Cloudflare for Families (1.1.1.3) with plain DNS and CleanBrowsing offered as
an alternative in the wizard. `nftables` redirects all outbound port 53 and 853 to the
local resolver and drops known DNS-over-HTTPS bootstrap endpoints. Chromium policy sets
`DnsOverHttpsMode: "off"`.

**D-60 [owner] Allowlist mode exists.** Essential for 4–7.

**D-61 [claude] Default mode depends on age band.**
- **4–7: allowlist.** Only what the parent permits. A curated starter list ships
  (EBA, TRT Çocuk, GCompris online, Scratch, Wikipedia for kids).
- **8–10: blocklist.** Broad browsing with categories blocked.
The wizard explains both in one sentence each and preselects by the child's age.

**D-62 [owner] Third-party blocklists are opt-in, presented as a recommendation.**
Wizard text: "Use the recommended blocklist? (StevenBlack — advertising, tracking,
adult content)". Downloaded on demand, cached locally, refreshed weekly, never a hard
dependency.

**D-63 [claude] YouTube and search engines get special handling.**
SafeSearch forced by DNS CNAME (`forcesafesearch.google.com`,
`safe.duckduckgo.com`, `strict.bing.com`) plus Chromium `ForceGoogleSafeSearch` and
`ForceYouTubeRestrict: 2` (strict). For the 4–7 band, youtube.com is blocked outright
by default and youtubekids.com is offered instead.

**D-64 [owner] A child-friendly block page.**
Served locally. A Zio/Sio character, one sentence at a reading level appropriate to the
age band, and a big **"Anneme/Babama sor"** button.

**D-65 [owner] Access requests.**
That button files a request: site, time, screenshot of the block page. The parent gets
it in the panel and by email, and approves or denies in one tap. Approvals go straight
into the allowlist. This turns the filter from a wall into a conversation.

**D-66 [claude] Per-application network control: Phase 4, off by default.**
Implemented with nftables cgroup matching so only catalog apps reach the network.
Real value, real complexity; it does not belong in the MVP.

**D-67 [claude] The child cannot install software.** No sudo, no polkit rule for
PackageKit, no Flatpak, `~` is not mounted `noexec` (that breaks too much) but app
launching is mediated by the shell.

---

## AI supervision layer

Full spec in `docs/AI-LAYER.md`. Decisions:

**D-70 [owner] Event-triggered, not continuous.** No per-second screenshots.

**D-71 [owner] Fully local inference. No cloud, ever.** Non-negotiable, ties to
principle #1.

**D-72 [claude] Layered pipeline, cheapest signal first.**
1. URL and window title against the existing lists — instant, handles most cases.
2. Fast local OCR on the screenshot, keyword scoring — ~50 ms.
3. VLM, only when layers 1–2 are inconclusive.
Expect fewer than 30 VLM calls per hour of use.

**D-73 [claude] Model selection, tiered by hardware.**
- ≥ 8 GB VRAM: **Qwen2.5-VL-3B-Instruct**, Q4_K_M GGUF, via `llama.cpp` server.
- 4–8 GB VRAM: **Moondream2**.
- CPU only: **SmolVLM-500M**, with a warning that quality drops sharply.
`llama.cpp` is chosen over vLLM because it is a single binary with a CPU fallback and
ships GGUF quantizations we can host.
**Hardware note:** the dev machine has an **RTX 5050 (Blackwell, sm_120)**, which needs
CUDA 12.8 or newer and a recent llama.cpp build. Verify this first; if the build is
painful, start on Moondream2 and move up.

**D-74 [claude] Screenshots are processed in memory and discarded.**
Only a flagged frame is written, to `/var/lib/biokids/evidence/`, 0700, retained
7 days by default (parent-settable 0–30), auto-pruned by a timer.

**D-75 [claude] The parent sees flagged evidence only, behind the PIN. No live viewing.**
A live screen-mirror feature would make this surveillance software. It is deliberately
absent.

**D-76 [owner] The supervision indicator is configurable, visible by default.**
A small Zio/Sio badge in the launcher. The parent may shrink it but the first-run child
explanation screen (D-88) always runs.

**D-77 [claude] The VLM sees the whole screen, but only on a trigger.**
Triggers: first visit to a domain not seen before, an application outside the catalog
gaining focus, fullscreen video starting, and a manual parent request. Not on a timer.

**D-78 [owner] Output is a 0–100 risk score** with a short reason string and a category,
not a yes/no.

**D-79 [owner] Age-banded thresholds.**

| Band | Log | Notify parent | Block + notify |
|---|---|---|---|
| 4–7 | 0–29 | 30–64 | 65–100 |
| 8–10 | 0–44 | 45–79 | 80–100 |

**D-80 [owner] Notify before punishing.** The middle band never punishes; it informs.
Only the top band blocks, and the 30-minute timeout in that band is off by default and
must be enabled by the parent.

**D-81 [owner] Timeout duration: 30 minutes** when enabled. It does not consume the
child's quota; punishment and quota are separate ledgers.

**D-82 [owner] `biokids-ai` is a separate optional package, default off.**
`Suggests:`, never `Depends:`. Everything else works without it.

---

## Zio and Sio

Full spec in `docs/VOICE.md`. Decisions:

**D-85 [owner] Zio is the mother voice, Sio is the father voice.** Two TTS personas,
not chatbots.

**D-86 [owner] Voice cloning is opt-in and each family records its own.**
Shipping the author's parents' voices to strangers would be strange and a privacy
problem. The panel has a guided recording flow: 8 sentences, about 90 seconds,
processed locally.

**D-87 [owner] Default voices ship: one female (Zio), one male (Sio), Turkish and English.**
From the Piper voice catalog, redistributable licenses only. Verify each voice's
license before bundling.

**D-88 [owner] Piper is the TTS engine.** Fast, fully offline, CPU-only, good Turkish,
MIT. It is the default and the fallback.

**D-89 [claude] Cloning pipeline: Piper → OpenVoice V2 tone-color conversion.**
Piper alone cannot clone. **XTTS-v2 is rejected** despite being the obvious choice: its
CPML license forbids commercial use, which is incompatible with an MIT project people
may build on. **OpenVoice V2 is MIT** and does timbre conversion on top of any TTS
output, which fits Piper cleanly. This pipeline needs validating on Turkish early in
Phase 2 — if quality is poor, evaluate F5-TTS (MIT) before considering anything
non-permissive. XTTS-v2 may be documented as a manual user-installed option, never
bundled.

**D-90 [owner] ElevenLabs is supported as an optional cloud backend**, parent's own API
key, clearly labelled as the one feature that sends data off the device. Off by
default, and disabled entirely if the parent selects "strict offline" in the wizard.

**D-91 [owner] The parent chooses which events speak.**
Toggles for: welcome, 10/5/1-minute warnings, time up, blocked site, break start and
end, earned time, bedtime. Every one independently switchable, all off is valid.

**D-92 [owner] One-way only.** Zio and Sio speak; the child does not talk back. No
microphone capture in the child session, no LLM conversation. This keeps the feature
simple, private, and impossible to misuse.

**D-93 [owner] A visual accompanies the voice.** A small character card slides in from
the corner while speaking. Two hand-made SVG characters with a few expressions
(neutral, happy, concerned, sleepy). Vector, themeable, no external assets.

**D-94 [owner] Custom art direction, playful.**
Rounded geometry, high contrast, a warm palette. Zio in warm coral/amber, Sio in
teal/indigo. Full tokens in `docs/UX.md`. No stock icon packs for the characters.

---

## Child experience

**D-95 [claude] Always kiosk. The launcher is the only surface.**
Not "kiosk for little ones, desktop for bigger ones" — two modes means two things to
maintain and test. What changes with age is the *contents* of the launcher.

**D-96 [owner] Two age bands drive the UI.**
- **4–7:** very large tiles, icon-first, minimal text, every tile has a spoken label on
  hover/focus, no clock, no file manager.
- **8–10:** smaller tiles with labels, a clock, a restricted file manager limited to
  their home directory, and the ability to see their own usage stats.
The band is derived from the child's age but the parent can override it.

**D-97 [claude] The parent curates the app list** from the catalog. Nothing appears
that the parent did not enable.

**D-98 [claude] No settings application for the child.** Display brightness and volume
are exposed as two tiles in the launcher; everything else is hidden.

**D-99 [claude] Login: LightDM with big avatars.** Child accounts are passwordless
(clicking the avatar is enough — the real gate is the quota, not the login). The parent
account has a password. Optional autologin into a single child account.

**D-100 [owner] The child may shut down**, from a clearly marked tile with a
confirmation.

**D-101 [claude] Screen recording and screenshot tools are removed from the child
session**, and the clipboard is not shared between the child session and the parent's.

**D-102 [owner] Offline educational content is bundled** where licensing allows:
Kiwix with a Turkish Vikipedi Çocuk selection, and offline Blockly games. This is
what makes the machine useful with no internet and it is a genuine differentiator.

---

## Threat model, privacy, legal

**D-105 [claude] The adversary is a curious 4–10 year old working alone.**
Not a teenager, not someone with a second computer and a search engine, not an adult.
Stating this plainly in `README.md` is a feature: it prevents false confidence.

**D-106 [claude] If the daemon dies, the session locks.**
`Restart=always` plus an `OnFailure=biokids-lockdown.service`. The child cannot kill it
anyway (no sudo), but a crash must not silently unprotect the machine.

**D-107 [claude] Unplugging the network fails closed.** Filtering is local, so no DNS
means no browsing. Nothing to do here; it is a property of D-59 and worth stating.

**D-108 [owner] "Nothing leaves the device" is the headline promise.**
It goes in the README, on the site, and in the wizard.

**D-109 [owner] Zero telemetry.** No exceptions, not even opt-in, in v1.

**D-110 [owner] A KVKK/GDPR note and a plain-language privacy policy ship with the
project**, written for parents rather than lawyers, listing exactly what is stored,
where, for how long, and how to delete it. Add the standard "this is not legal advice"
line and get it reviewed before any public launch.

**D-111 [owner] A first-run explanation screen for the child**, age-appropriate, in the
child's own language: what Zio and Sio do, that a grown-up can see which sites were
visited, and that they can always ask for permission. Non-skippable on first login.

**D-112 [claude] Export and erase.** Panel buttons for "Download my family's data"
(JSON + CSV) and "Delete everything" (wipes the DB, evidence, and voice models, with
passphrase confirmation).

**D-113 [claude] Disclaimers.** README and wizard both state: BioKids is a supervision
aid, not a guarantee; no filter catches everything; it does not replace talking to your
child.

---

## Implementation decisions

These were settled while building Phase 0, in areas the earlier entries left
open. They are `[claude]` and reversible; read the reasoning first.

**D-123 [claude] Two daemon sockets, and the child's is authorised by peer uid.**
`ARCHITECTURE.md` §2 says the launcher asks the daemon for permission;
`SECURITY.md` §2 says the daemon's socket is `root:biokids` 0660. Both are right,
and together they leave the child unable to reach the daemon at all — which is
correct, because the `biokids` group can grant time and power the machine off,
and a child must never be in it.

So the daemon listens twice. `/run/biokids/daemon.sock` is unchanged. A second
socket, `/run/biokids/shell.sock`, is world-connectable and carries a deliberately
short list of operations, none of which takes a child id as an argument: the
daemon asks the kernel who connected (`SO_PEERCRED`), maps that uid to a child,
and answers about that child only. The open file mode is not the control; the
peer's identity is, and a process cannot lie about that.

Rejected alternative: putting the child in the `biokids` group. It is one line
and it hands a child every operation the panel has.

**D-124 [claude] "+10 dk" raises the day's allowance; it does not subtract from usage.**
Subtracting from `general_s` reads naturally and is wrong: a child who has used
two minutes of a two-hour quota would get two minutes back out of a ten-minute
grant, because usage cannot go below zero. A grant is worth what the parent said
it was worth, whatever the child has done today, so it is a separate counter that
raises the ceiling. A grant also re-arms any warning it has pushed back into the
future — otherwise the extra ten minutes arrive with no warning at the end of them.

**D-125 [claude] Suspended time is derived from two clocks, not reported by logind.**
`CLOCK_BOOTTIME` includes suspend, `CLOCK_MONOTONIC` excludes it, so the
difference between the deltas is exactly how long the machine slept. Exact, no
round trip, and nothing to disagree with. `ARCHITECTURE.md` §3.1 is updated.

**D-126 [claude] An unexplained gap is clamped, not billed.**
If the daemon was stopped, frozen, or hibernated in a way the suspend subtraction
missed, the interval since the last tick can be hours. Crediting that as screen
time would be the single worst bug the ledger could have, so an interval longer
than three ticks is recorded as a gap and clamped. The child loses nothing; the
parent sees the gap in the event log.

**D-127 [claude] The panel degrades rather than disappearing when the daemon is down.**
Running it against a stopped daemon showed "no children have been added yet",
which is both alarming and untrue: the children are in a database the panel reads
perfectly well, and only their *live* state is unknown. The cards are now drawn
from the database with the counters blank, the state shown as unknown, and the
action buttons withheld, under the existing banner. The launcher does the
opposite and shows a holding screen with nothing to start, because on the child's
side a working launcher with no limits behind it is the failure to avoid.

**D-128 [claude] The launcher is progressive enhancement, and so is the panel.**
Every form is an ordinary form and every POST redirects; JavaScript only adds the
live counters. A phone on a flaky tailnet gets a stale page rather than a broken
one, and a launcher whose script fails to load still starts applications.

**D-129 [claude] HTMX is not used. This revises D-32, which named it.**
D-32 is right about everything except the library. Its actual constraints are "no
build step, no npm, no CDN, and the panel is forms and tables", and all three
survive; what changed is that HTMX turned out not to be the way to satisfy them.

Two reasons, in order of weight:

1. **Vendoring is the only way to ship it, and vendoring means shipping a blob
   nobody in the repository can reproduce.** Offline is a hard requirement (D-18),
   so a CDN is out and the file has to live in `assets/`. A ~17 KB minified
   artefact that arrives by download, cannot be reviewed line by line, and has to
   be re-downloaded to upgrade is a strange thing to put in a parental-control
   package whose whole claim is that you can see what it does.
2. **The panel needs exactly two dynamic behaviours**, and HTMX is the wrong shape
   for one of them. The live counters are a server-sent event stream, which is
   `EventSource` — a browser built-in — and HTMX needs an extension for it. The
   other is "submit a form", which HTML has done since 1993.

So: plain forms that POST and redirect, and about 120 lines of hand-written
JavaScript per surface for the live parts. The gain is not lines saved, it is
that **everything works with JavaScript switched off**. A phone on a flaky
tailnet gets a page that is merely not live; a launcher whose script fails to
load still starts applications; and the screenshots in the README are rendered
by the server, which is a fact about the product rather than about the
screenshots.

Reverse this if a screen appears that genuinely needs local state — the voice
recorder in Phase 2a is the likely first one. Vendor Alpine.js or HTMX then, for
that screen, with the licence recorded; do not reach for it to avoid writing a
form.

---

## Development workflow

**D-115 [owner] Host is Windows 11 Pro. Development target is a Debian 13 VM in VirtualBox.**
Claude Code runs on the Windows host and works against a mounted/synced repo; the VM is
where the system is installed and tested. Always snapshot before touching boot, PAM,
nftables or GRUB.

**D-116 [owner] A VM is sufficient for almost everything.**
Real hardware is only required for: wifi and GPU drivers, Secure Boot, the installer's
partitioning, and GPU inference performance. The old desktop PC handles those when the
time comes. Do not let the absence of real hardware block Phases 0–2.
Note: the AI layer cannot be meaningfully tested in VirtualBox (no GPU passthrough).
Run `biokids-ai` on the Windows host or on the bare-metal PC during development.

**D-117 [claude] Python 3.13** (Debian 13 default). `ruff` for lint and format,
`mypy --strict` on core and daemon, type hints required.

**D-118 [claude] Tests are required for logic, not for looks.**
`pytest` on: the time ledger, the policy engine, the risk scorer, the category
allocator, the earned-time calculator. Templates and CSS are unit-tested by looking at
them.

**D-119 [owner] English commits, Conventional Commits format, English docs and comments.**
Public-repo grade from the first commit: README with screenshots, CONTRIBUTING.md,
issue and PR templates, CHANGELOG, semantic versioning.

**D-120 [owner] Claude Code may ask questions, and works in large blocks.**
Ask when a decision changes architecture, adds a non-permissive dependency, or touches
the security model. Otherwise complete a coherent unit and report.

**D-121 [owner] Do not use the `pusht` skill here.** This repo follows standard open
source practice: feature branches, pull requests into `main`, tagged releases. Not
sequential version branches.

**D-122 [claude] Docs structure: a short always-read `CLAUDE.md` plus detailed
`docs/*.md`.** A single giant file would be re-read in full every session and would
still be skimmed. Keep `CLAUDE.md` under about 200 lines and push detail into `docs/`,
with a pointer table telling Claude Code which doc to open for which task.

**D-130 [claude] Adding a child provisions their Unix account; the packaging never does.**
`create_child` wrote a row whose `unix_user` named an account that did not exist, and
nothing put the child's XFCE configuration in a home directory. Both failures look
exactly like success: the panel fills with quotas and a friendly avatar, and the machine
is unchanged. There is no avatar on the login screen, or the child logs in to an ordinary
XFCE desktop with Alt+F4 and a file manager while the Today card says they are managed.

Provisioning belongs at the moment a parent adds a child — `biokids_core.provision`,
called from the wizard's fourth step and from the children screen — and **not** in
`postinst`. Maintainer scripts run before any child exists and cannot know their names,
and `/etc/xdg/autostart` would start a kiosk launcher inside the *parent's* session too.
`biokids-admin provision-child` is the repair path for a machine that was set up before
`biokids-shell` was installed, or a home restored from a backup without its dotfiles.

Constraints, each of which is asserted in `tests/test_provision.py`:
`adduser --disabled-password` with no supplementary groups — emphatically not `biokids`,
which can grant time and power the machine off (D-123). Idempotent: an existing account
is left alone and a file a parent edited is not overwritten unless asked. Never fatal to
the request — a machine without `adduser` must still finish adding the child, with the
reason in the audit log. And nothing here ever removes a home directory: taking a child
out of BioKids must not delete their drawings.

**D-131 [claude] `biokids-admin reset-pin` reads the PIN, it does not take it.**
It was a positional argument. Every process table on the machine shows a command line —
including the child's session — and the shell writes it to history. On a family machine
the other user is precisely the person the PIN exists to stop. Now `getpass`, asked
twice, with `--stdin` for scripts. `dev/check_i18n.py` grew an AST walk over `repo.audit`
call sites at the same time: twelve audited actions had no `audit.action.*` string, and
gettext returns the key unchanged, so the one screen whose whole job is to be readable
was printing `passphrase_failed` and `access_requested` at a Turkish-speaking parent.

**D-132 [claude] Earned time lands in the general allowance when there is no Games
limit to raise.** D-45 puts earned minutes into Oyun: learning buys game time. Read
literally, `earn_spendable_general = false` means "raise the Games ceiling" — and
nothing in the product writes a Games ceiling. `CategoryLimit` is a table with no
writer, per-category limits are D-44 and not Phase 0, and `earn_spendable_general` has
no writer either. So in every configuration a family could actually produce, both
branches were unreachable: the child did their homework, the daemon congratulated them,
the panel showed the minutes, and their remaining time did not move by one second.

Raising an unlimited ceiling is a no-op, so the only place those minutes can go is the
day's allowance. This is not a departure from D-45; it is what D-45 means in a
configuration it did not consider. The parent's setting still decides when there *is* a
Games limit, and `earned_lands_in_general()` is written as one condition and its
negation so that the minutes can never be counted in both places or neither.

The earn controls stay on the child's time screen even though TASKS-PHASE0 puts rewards
outside Phase 0, because they now do what they say. A control that does nothing is the
bug this entry is about.

**D-133 [claude] Warnings are re-armed on every tick, not only on a grant.**
`rearm_warnings` was called from the grant path alone, so only "+10 dk" could bring the
warnings back. Any of half a dozen other things put the end of the day back — the quota
edited on the settings screen, a window widened, a break ending, a policy reload — and
all of them left `warned` full. A parent who raised a 20-minute day to 60 gave their
child forty more minutes that ran out in complete silence, ending in a shutdown with no
warning at all. Re-arming every tick makes the warning set a function of the current
remaining time rather than of how it got there. The ledger only marks the child dirty
when the set actually changes, so the cost is a set comparison.

**D-134 [claude] The child's screens get their own strings, not the panel's.**
Two failure screens reused `error.unknown` and `error.daemon_unreachable`, which are
written for a parent: formal address, and in one case "try restarting the computer" —
advice a six-year-old can neither follow nor should. The time-up screen had the same
shape of bug from the other direction: it announced a shutdown for all three end
actions (D-43), so a child told to save their work watched the screen merely lock, and
learned that the sentence is not true. The daemon knew which action was configured and
simply never sent it; `shell_state` now does.

**D-135 [claude] Every button says what happened, and the child's screens name no
parent.** Two findings from the same sweep: a list of catalogue strings that exist,
are translated, and are rendered by nothing. That list is worth re-running — it is how
both of these surfaced, and `dev/check_i18n.py` cannot catch them, because a string
nobody uses is not a string that is missing.

"+10 dk", "Lock" and "Turn off" redirected to `/today?granted=1`, `?locked=1`,
`?poweroff=1`, `?error=daemon` — and `today.html` read none of them. The parent pressed
the button, the page reloaded, and nothing said whether it worked. The failure case is
the one that matters: a grant that never reached a stopped daemon looked exactly like a
grant that worked, so a parent walked away believing their child had ten more minutes.
`today.grant.done` and its two siblings had been sitting translated in both catalogues,
never rendered.

The blocked screen's one button said "Anneme sor" — ask my mum. `/ask-a-parent` is
parent-neutral: BioKids has a single parent account and the request goes to whoever
holds it. A child whose account holder is their father, or who has no mother, was sent
to find someone who is not there. `shell.blocked.ask_father` existed and was never
rendered, which is a second button that was planned and never added — and two gendered
buttons doing the same thing would have been the wrong fix. The body of that screen
already said "you can ask a grown-up"; the button now agrees with it.

**D-136 [claude] The virtual-terminal guards D-54 names are actually shipped.**
D-54 disables TTY switching "via `logind.conf` + `X11 DontVTSwitch`" and T5.7 lists it
as Phase 0 work. Neither file existed. `src/biokids_shell/xfce/README.md` stated that
`biokids-daemon` installed them, so `Ctrl+Alt+F2` gave the child a login prompt while
the documentation told a parent that escape was handled — worse than not claiming it,
because the claim is what a parent relies on. `biokids-shell` now ships both.

`ReserveVT=1` rather than none, deliberately: a machine a parent cannot recover from a
broken X server is a worse outcome than the escape this closes (principle #4). The
recovery paths are GRUB's recovery entry and ssh over the tailnet, and both config
files say so in their own comments.

**D-137 [claude] espeak-ng is what speaks; Piper is the upgrade.**
T6.1 asks for "one Turkish female plus one Turkish male voice" from the Piper catalogue.
Checked against the archives on 2026-09-20, neither exists in a form BioKids may ship:
`tr_TR-dfki-medium`'s dataset is CC-BY-NC-SA-4.0, and `fahrettin` and `fettah` were
removed from `rhasspy/piper-voices` "at request of contributors" (commit `b145f2a`). A
voice whose contributor asked for it to be taken down is not one to keep a copy of.

And Piper itself is not packaged: **`piper-tts` is not in Debian 13.** The only `piper`
in trixie configures gaming mice. So `Recommends: piper-tts` named nothing, and on a
fresh machine `Synthesizer.from_config` found no binary and fell straight to silence —
every warning an on-screen card, permanently. The Phase 0 exit criterion asks for
warnings **spoken** in Turkish, so as shipped it was unreachable by any install.

espeak-ng is in Debian main, GPL-3+, speaks Turkish, carries its voices inside the
package, and needs no model file and no network. It sounds like a 1990s robot, and that
is the trade: a four-year-old who hears "beş dakika kaldı" has been warned, and one
shown a card they cannot read yet has not. Piper is still tried first and still wins
wherever a family installs it. `mbrola-tr1`/`tr2` improve espeak-ng's Turkish and are a
`Suggests`, not a `Recommends`, because they are in non-free and BioKids must install
cleanly from main alone.

Each engine caches into its own directory. The key is `sha256(text + persona + profile)`
and names no engine, so one shared directory would leave a family who installed Piper on
Tuesday hearing Monday's robot — or Monday's silence — for every line they already knew.

Revisit if a Turkish Piper voice with a redistributable dataset appears, or if
`piper-tts` enters Debian. Bundling one is still the better outcome; there is nothing to
bundle today.

**D-138 [claude] An end action is scoped to whoever is actually at the machine.**
The end-of-time sequence ran for every child on every tick with no presence test. A
logged-out child still evaluates to `QUOTA_EXHAUSTED` — the engine never consults
presence on that branch — and `_lock` quietly no-ops on an empty session list, so
control reached `apply_end_action(POWEROFF)`, which ignores session ids entirely. The
mother logs in that evening and two minutes later the machine shuts down under her.
Poweroff is the column default and the wizard pre-checks it, so this was the
out-of-the-box behaviour, and with a bedtime window it repeated every evening whether
or not the child ever touched the computer.

Two rules now. The sequence does not start for a child with no session at all: there is
no screen to lock, and the countdown should begin when they come back rather than
having expired in their absence. And when somebody else is logged in — a parent
working, a sibling drawing — `poweroff` and `logout` narrow to `lock`, because those
two reach past the child and locking is the part that was ever about this child. The
child still loses the machine; nobody else does. The narrowing is audited so a parent
can see why their chosen action was not the one taken.

**D-139 [claude] Three failed ticks in a row is a failed daemon, and it says so.**
`persist()` committed with no `try`, so one transient SQLite error left the daemon's
long-lived Session needing a rollback nothing ever issued, and every later tick died
with `PendingRollbackError` before accounting for a second. `run_forever` caught that,
logged it, and looped — forever. The process stayed alive, so systemd saw
`active (running)`: `Restart=` never fired, `OnFailure=biokids-lockdown.service` never
fired, there is no `WatchdogSec`, and the panel kept rendering the last committed
numbers. That is "continue unprotected", and `run_forever`'s own docstring already
promised the opposite.

`persist()` now rolls back and re-raises. `run_forever` counts consecutive failures:
one is a hiccup and is logged, three means nothing is being enforced, so it locks every
child session itself — D-106's answer, and it needs no database, which matters because
the database is the likeliest thing that just broke — and then raises so the unit exits
non-zero and systemd's own net catches the rest. The tests never saw any of it because
`Database.session()` is the only place in `src/` that rolls back and the daemon
deliberately does not use it: the fixtures were performing the recovery production
lacked.

**D-140 [claude] A socket server cancels its connections before it waits for them.**
Since CPython 3.12 `Server.wait_closed()` waits for every connection handler task.
`DaemonServer.stop()` cancelled its handlers *after* awaiting `wait_closed()` — which
was blocked on exactly those handlers — and `ShellServer.stop()` tracked no tasks at
all. The child's launcher holds one subscription open for as long as the kiosk is on
screen, so every restart, upgrade, reboot and press of the child's own shutdown tile
hung for the full 90 s stop timeout and was then SIGKILLed. `$SERVICE_RESULT` is then
`timeout`, not `success`, so the unit's `ExecStopPost` fired `biokids-admin lockdown`:
an upgrade locked a child out of a machine they were legitimately using, with an
alert-level audit row for a lockdown that had no reason behind it, and the ledger was
never flushed because `supervisor.stop()` never ran. Cancel, then wait. The order is
the whole fix.

**D-141 [claude] `biokids-admin lockdown` falls back to the built-in defaults.**
D-106's backstop runs precisely because the daemon is not there, and one of the
commonest ways for the daemon not to be there is a parent making a typo in
`/etc/biokids/config.toml` — a documented conffile they are invited to edit. That typo
took the backstop down with it: `main()` called `Config.load()` before any handler ran,
`ConfigError` escaped, and no session was locked and nothing was audited. Lockdown now
falls back to `Config()`; it needs almost nothing from the file, and locking against
the default database path is worth far more than not locking. Every other subcommand
still refuses a file it cannot read, because guessing is the wrong answer when nobody
is in danger.

**D-142 [claude] Creating a child's Unix account lives in its own unit, not in the
panel's sandbox.** The panel runs under `ProtectSystem=strict` and `ProtectHome=yes`
(SECURITY.md §2), so `/etc` is read-only and `/home` is an empty tmpfs: `adduser` fails
with EROFS and the child's dotfiles cannot be written. Provisioning moved into the
panel in D-130 and nobody went back to the unit, so on a real `.deb` install the parent
finished the wizard, set a quota, rebooted, and there was no avatar on the greeter at
all. That is the Phase 0 exit criterion, and it could not be met as packaged.

The fix is **not** to widen the sandbox. `ReadWritePaths=/etc /home` plus
`ProtectHome=no` on a process that also serves HTTP trades a real bound on every other
panel bug for one feature. Instead `biokids-provision@.service` runs one fixed command
with one argument — `biokids-admin provision-child %i` — and the panel reaches it with
`systemctl start --wait`. The unit is deliberately unsandboxed, because writing
`/etc/passwd` and `/home/<child>` is the whole job and a sandbox permitting those would
permit the thing we are worried about; what bounds it is that it does exactly one
thing. The instance name is checked against `UNIX_USER_RE` and `-` escaped as `\x2d`
before it is interpolated, because it becomes a unit name.

`provision_via_systemd` returns `None` when there is no systemd to ask, and the caller
then does the work in-process — which is what `biokids-admin provision-child` itself
does, and what the unit ends up running. So a developer running the panel by hand, and
the test suite, still get a child.

Whether a unit with `ProtectSystem=strict` and `SystemCallFilter=@system-service` can
reach systemd at all is the first thing to check on the VM; it is expected to work, and
`debian/README.source` §7 item 1 says what the symptom is if it does not.

**D-143 [claude] A gap between two of today's windows is a pause, not the end of the day.**
`window_open` is `any(w.contains(...))` and nothing could ask "is another window still
ahead today?", so every closed window produced `Reason.WINDOW_CLOSED` — which
`TERMINAL_LOCK` routes to the end action, poweroff by default. A parent who sets
"07:30-08:15 before school, 16:00-19:00 after school", which is the natural way two
windows get used, had the computer shut itself down at 08:17 while Zio said "Yarın
görüşürüz" and the child still had their whole daily quota.

`Reason.WINDOW_WAITING` is the new answer for a window that is still ahead. The screen
locks and nothing else happens; the child gets `window_waiting.txt` rather than
`time_up.txt`. The screens had already drifted the other way, which is the same
confusion from the other side: `window_closed.html` says "you can open it again when
your hours come", which is the *waiting* message, while the speech was `time_up`, the
*day is over* message.

**D-144 [claude] Granting time unlocks the screen, because nothing else can.**
`grant_time`'s own comment said "the lock screen comes down" and nothing brought it
down: `_clear_end_sequence` clears the daemon's bookkeeping, not logind's.
`Enforcer.unlock_sessions` existed and was called from nowhere in the product. So the
quota ran out, the screen locked, the child asked for more time, the parent pressed
"+10 dk", the panel said "10 dakika eklendi" and the card showed 10:00 — and the child
was still at the password prompt of an account that deliberately has no password
(D-99), with nothing in BioKids able to let them in.

`grant_time` is async for this reason. The unlock happens only when the grant actually
bought usable time: "+10 dk" outside the allowed hours must not open a screen the
window is there to keep shut, and those extra minutes are for tomorrow.

**D-145 [claude] The voice fallback is a chain of engines, not a choice made once.**
`from_config` picked one backend on `which("piper")`, and `render` then looped over
*voices* only. But a binary existing is not the same as it being able to speak: BioKids
ships no Piper model, so a parent who read the package description and installed piper
got `SynthError("no voice model")` for every line, for ever, while espeak-ng sat
installed by Recommends and untried. Both docstrings already described the correct
behaviour — `from_config` said "Piper, then espeak-ng, then silence" and VOICE.md §6
says "cloned → stock Piper → espeak-ng → banner" — and neither was what the code did.

The Synthesizer holds engines, plural, each with its own cache, and `render` loops over
voices and engines. `status` now reports the engine that actually spoke rather than the
one that was selected, which is how a machine silent for a week still said
`backend: "piper"`.

**D-146 [claude] Every `?error=` the panel redirects with is rendered, in one place.**
Eight findings had one cause: a route computed an outcome, put it in a URL, and no
template read it. `settings.html` rendered exactly one key (`pin`); `children.html`
rendered none. So a parent who mistyped their current passphrase, submitted a stale
form, or acted while the daemon was down got the same page back with nothing changed
and nothing said — which is indistinguishable from success.

The flash is assembled in `base_context` and rendered in `base.html`, so it covers
every page including ones not yet written. A slug containing a dot is treated as a
catalogue key already, which is how a `ValueError` from `validate_pin` reaches the
screen; anything unrecognised falls back to a generic failure rather than being
dropped, because dropping it is the bug.

Three callers separately threw away a result. `_call_daemon` returns a bool and the
time-rules route ignored it, so "Kaydedildi" appeared over a reload that never landed
and the child kept running on the old quota. `remote.enable()`'s `detail` — the only
thing that says *why* Tailscale refused — was never logged. And `provision_child()`'s
failure was discarded by both callers, so a child with no Unix account looked exactly
like a child with one, and the parent found out weeks later when there was no profile
on the greeter.

Wizard step 8 is the sharpest of the three: its entire purpose is "you can reach the
panel from your phone", and it walked the parent on to step 9 after Tailscale had
failed. They finish setup believing something untrue and discover it when they are not
at the machine.

The open redirect fixed in 3cc49a3 was only half fixed: `GET /login` never applied
`safe_next`, so an already-signed-in parent following a link was redirected off-site
immediately, and for everyone else the raw value was rendered into the form as a hidden
field and posted back.

**D-147 [claude] An auto-suggested account name may be adjusted; a typed one may not.**
`_suggest_user` keeps only the first word, so "Mehmet Ali" and "Mehmet Can" both fold to
`mehmet`, and `create_child` did `existing or Child(...)` and wrote the second child's
details over the first one's row. The older brother vanished from the panel, the panel
said a child had been added, and the two shared one Linux account, one daily quota and
one ledger — whoever logged in second was enforced against the other's time.

A name BioKids invented is BioKids' to adjust, so a collision takes the next free
variant. A name the parent typed is theirs, and quietly pointing it at a different
child's account is not a correction we are entitled to make: that one is refused with a
message.

**D-148 [claude] The per-child voice switch is honoured, and the audit day is the
parent's day.** Two settings written and never read. The wizard's "Sesler" checkbox
wrote `policy.voice_enabled` and `load_policy` had no field to copy it into, so a parent
who unticked it because the baby was asleep got the warnings spoken anyway — and there
is nowhere else in the panel to turn the voices off. The only gate on speech was
`config.voice.enabled`, a machine-level flag in `/etc/biokids/config.toml` that nothing
in the product ever writes.

And the audit screen's date filter built local-midnight boundaries and compared them
against timestamps stored as naive UTC. In Istanbul (UTC+3, the project default) every
row between 00:00 and 03:00 fell out of its own day — exactly the window a parent asking
"what happened last night" means. The unfiltered list showed the 01:30 row under today
and filtering for today hid it.

**D-149 [claude] The kiosk policy goes where Chromium reads it, and a launch returns
when the app starts.** `write_policy` wrote to `<user-data-dir>/policies/managed/` and
its docstring asserted Chromium reads that. It does not: the Linux policy loader reads
only `/etc/chromium/policies/{managed,recommended}`, and `--user-data-dir` adds no
policy source. CLAUDE.md §3 and ARCHITECTURE.md already named the fixed path; the code
was the only thing that disagreed. So every lockdown T5.2 promises was inert —
`chrome://policy` empty, `URLBlocklist: ["*"]` unapplied, DevTools available, downloads
unrestricted — with only a keydown handler left, whose Ctrl list does not include
Ctrl+Shift+I.

The policy depends on nothing but `host:port`, which is identical for every child, so
the package ships it and the launcher verifies it rather than writing it. When it is not
in force `write_policy` returns None rather than a path, because a caller that treats a
returned path as "the kiosk is locked down" is the original bug restated.

`systemd-run --scope` does not fork and return: it registers its own PID into the scope
and execs the command, so `communicate()` waited for the whole life of the application.
`report_activity` reached the daemon only after the child closed the app, and the launch
result was the app's own exit status — so when the daemon stopped the scope at quota
time, the launcher reported that the launch had failed. A refusal happens before the
exec and is therefore immediate, so a short bounded wait tells the two apart.

**D-150 [claude] The fifteen medium and low findings.** Grouped because they share one
property: each is small enough to live with and wrong enough that a parent who noticed
would not trust the rest.

*The daemon.* The end-action backoff was dead code — `already_done` is only true once
the action has succeeded, so an action that never worked was retried every tick and
re-escalated to lockdown every thirty seconds for the rest of the day. A lockdown with
an empty session list was audited at alert severity as though the machine had been
secured. The child's shutdown tile checked `self.children`, so it refused when the other
user was a sibling and allowed it when the other user was a parent — mum switches user,
her son taps "Kapat", and the machine goes down with her unsaved work. The shell was
sent the bare quota while `remaining_seconds` included earned minutes, so the ring a
four-year-old is meant to read sat at full for the length of the bonus. And penalty and
break counters live on the calendar-day row, so a fifteen-minute timeout given at 23:55
ended at midnight, silently; they now carry over, while quota does not, because a new
day is a new allowance and a punishment is not an allowance.

*The panel.* The session cookie was minted once with an absolute `Max-Age`, so D-51's
idle timeout threw out a parent who had been working for thirty minutes. The shutdown
confirmation existed only in an `onsubmit` handler, so with JavaScript off a mis-tap on
"Kapat" — which sits beside "Kilitle" in the same row of three — powered the machine off
under a child with no chance to cancel. A wrong PIN at the turn-protection-off gate was
not audited on an install with no passphrase, which is the one install where a child who
watched the PIN being typed can keep guessing. The lockout page was a disabled form with
no explanation. Wizard step 6 wrote the quotas before parsing the window, and `get_db`
commits on the way out, so a parent who typed "16.00-20.00" got the error page with
their quotas already saved and their end-action choice reverted to the form default. The
completion screen was unreachable: step 10 redirects to /wizard/11 and the gate that
stops a configured machine being re-set-up bounced it to the login page, so ten steps
ended at a bare PIN prompt. Yesterday's audit rows rendered identically to today's. And
the settings page reused the login sentence, which counts down attempts that form does
not have, leaving a hole where the number belonged.

*The shell.* `CHARACTER_DIR` is two levels above the package — `assets/` in a checkout,
`/usr/lib/python3` on an installed machine — so `_load_characters()` returned `{}` in
silence and every real install had a first-run screen introducing "ben Zio" next to
nothing. The packaging ships them now, and a missing character is logged.

**D-151 [claude] The daemon writes the Chromium managed policy.**
Found by verifying `docs/VM-RUNBOOK.md` against the repository: the runbook said the
package ships the policy file, and nothing shipped it. D-149 had moved the *reader* to
`/etc/chromium/policies/managed/` — the only directory Chromium on Linux reads — and
asserted somebody else would put the file there.

The architecture had already decided who: `PathsConfig.chromium_policy_dir` exists,
`biokids-daemon.service` carries `-/etc/chromium/policies/managed` in its
`ReadWritePaths` with a comment explaining why the `-` prefix is needed, and
`biokids-daemon.postrm` removes `*biokids*` from that directory on purge. Every part of
the plan was in place except the line that writes the file. So `chrome://policy` was
empty on every machine and the kiosk was an ordinary browser with an ordinary
Ctrl+Shift+I.

The daemon is the only BioKids process that can write it — root, and the unit whose
sandbox names the directory — and the policy depends on nothing but the launcher's host
and port, so one file covers every child. The launcher, running as the child, only
checks that it is there. A failure is logged at error level and is not fatal: a daemon
that refuses to start because Chromium is absent would be worse than a kiosk without a
policy, and `biokids-shell` is what pulls Chromium in.

**D-152 [claude] The wizard reports a failed provision, and does not stop for it.**
D-146 fixed the children screen and left the wizard's step 4 discarding the same result
— and the wizard is where the first child is always added, so the case that mattered
most was the one still silent. A parent finished setup and found out at the greeter.

It reports through the flash and continues, rather than holding the parent on step 4.
On a machine where `adduser` is absent the database row is still correct and
`biokids-admin provision-child` still repairs it, so refusing to move on would strand a
parent halfway through a ten-step setup with no way forward — principle #4 in its
smaller form. Visible is the requirement; blocking is not.

**D-153 [claude] What the first real build found.**
The first `dpkg-buildpackage` ever run, on a Debian 13.7 VM (runbook §1), failed in
`dh_install`, and lintian then found more. None of it was visible from the Windows host.

*pybuild's destination.* `export PYBUILD_NAME=biokids` in `debian/rules` tells pybuild
there is one binary package, `python3-biokids`, and pybuild then replaces a destination
of `debian/tmp` with `debian/python3-biokids` — so the eight `.install` files found
nothing. Removed; a source package that splits one Python distribution across several
binaries must not set it. And pybuild installs into the *versioned*
`python3.13/dist-packages`, which `dh_python3` renames only after `dh_install` has run,
so the `.install` files now say `usr/lib/python3*/dist-packages`.
`dev/check_packaging.py` checks both.

*debconf.* The postinst did not load `confmodule`. Under apt with apt-utils nothing
shows, because apt preconfigures; under `dpkg -i` the config script never ran, the purge
question was never registered, and a purge could not ask it. The data was kept — the
postrm fails safe — but the one question the package asks was silently never asked. The
template also said "answer yes", which means nothing to a frontend showing a checkbox;
reworded in English and Turkish.

*Manual pages.* Five commands had none, and `biokids-provision@.service` pointed
`Documentation=` at a `biokids-admin(8)` nobody had written. They exist now, in
`debian/man/`. `biokids-admin(8)` matters beyond lintian: it is the forgotten-PIN and
dead-daemon recovery path, and a man page is readable with the network unplugged
(principles #4 and #5). The checker now refuses a `man:` reference to a page that is not
shipped.

*Overrides.* Four lintian overrides no longer matched anything — lintian 2.122 does not
raise `empty-binary-package` for a metapackage or check user units against
`WantedBy=` — and were removed, because an override that matches nothing hides the
next real warning of the same name.

**D-154 [claude] The suite runs against what Debian 13 ships, and touches nothing.**
The first install of the D-153 packages failed in the postinst: `biokids_core.security`
imported `argon2.exceptions.InvalidHashError`, which arrived in argon2-cffi 21.2, and
trixie ships 21.1. The development venv had 25.1 from PyPI, as did CI, and
`pyproject.toml` asked for `>=23.1` - a floor the .deb could never meet, because an
installed BioKids runs on the distribution's packages, not on these. The package was
left unconfigured, which is the loud failure the postinst promises; but it was a failure
no test could have caught, because no test ran on trixie's libraries.

So three changes. The import uses `InvalidHash`, which is an alias in every later release.
The floors in `pyproject.toml` are now exactly trixie's versions, and `cryptography`,
imported by `security.py` and never declared, is listed. And CI has a job that runs the
suite in a `debian:trixie` container against apt's packages, as an ordinary user. Run
that way on the VM, with this one import fixed, the only failures were in the tests
themselves.

*`python3-multipart` is the wrong package.* In trixie that name is defnull's `multipart`
1.2.1; FastAPI needs Kludex's `python-multipart`, which Debian packages as
`python3-python-multipart` - and which Provides `python3-multipart`, so apt happened to
pick it on the VM. With the real package winning the resolution instead, the panel's
forms would not parse. `debian/control` now names the right one.

*The tests reached the real machine.* Run under `strace -f -e execve` on the VM, the
suite executed `systemctl start --wait biokids-provision@elif.service` - and `kerem`,
`mehmet` - from the panel tests, and `tailscale status --json` from the remote-access
ones. As an ordinary user systemd refused; as root on a machine with BioKids installed,
it would have created those accounts. On the Windows host neither program exists, which
is why nobody saw it. An autouse fixture in `tests/conftest.py` now gives every test a
machine with neither, and a test that wants one fakes it explicitly. Separately,
`test_provision` faked `shutil.which` to mean "no adduser", but `_find` then looks in
`/usr/sbin` itself and found the real one; and four tests there chowned to a made-up uid,
which is EPERM for an ordinary user and a stranger's file for root. CI's new job runs as
an ordinary user so that the next such test fails instead of succeeding.

**D-155 [owner] A child logs in by their picture, and a lock holds.**
Found by driving the runbook's §3-§4 on the VM. Each of these was invisible from Windows,
and together they meant a child could not log in at all and, had they been able to, the
backstop for a dead daemon would have locked nothing.

*Logging in.* D-99 promised that choosing the child's picture is enough, and nothing
implemented it. Provisioning creates the account with `--disabled-password`, Debian's
LightDM has no passwordless rule, and it hides the user list besides: the greeter asked
for a user name and then a password the account does not have. The owner chose a PAM
service of BioKids' own over a per-child password or single-child autologin.
`biokids-shell` ships `/etc/pam.d/biokids-lightdm` - `pam_nologin`, then
`pam_exec /usr/lib/biokids/biokids-child-login` as `sufficient`, then `@include lightdm`
unchanged - and `/usr/share/lightdm/lightdm.conf.d/50-biokids.conf`, which selects that
service and shows the user list. No other package's file is touched. The helper lets an
account in without a password only if it is in `biokids-child`, in none of `sudo`, `adm`
or `biokids`, has uid >= 1000, and has no `user`-class session open. That last condition
is what separates logging in from unlocking: light-locker unlocks through the same
greeter. Provisioning adds an account to `biokids-child` only when it has no usable
password already (`passwd --status`) and no administrative group, so a parent who types
their own user name as the child's never makes their own account passwordless. Checked
with `pamtester` and at the real greeter: Elif gets no password field; the parent, root
and unknown names are asked for a password as before; a locked Elif gets a password field
and an empty one is refused. LightDM reads its configuration only at start, so the
wizard's closing screen now tells the parent to restart once - replacing a promised
two-minute trial run that nothing offered, under a counter that read "Step 11 of 10".

*Locking.* No screen locker was installed, so `loginctl lock-session` - the LOCK end
action, and the whole of the D-106 lockdown - changed nothing on screen. `biokids-shell`
now depends on `light-locker`, which XFCE starts from `/etc/xdg/autostart`.

*The lockdown read nothing.* `biokids-admin lockdown` asked for `loginctl list-sessions
--output=json`. For loginctl `--output=` is the journal format of `session-status`; the
command printed its ordinary table, the JSON parse failed, and the lockdown logged
"locked 0 session(s)" and exited 0. On every machine, since it was written: the tests fed
it JSON. It now asks for `--json=short`, falls back to the table on a systemd without it,
leaves `manager` and `background` sessions out of the count, and exits 1 when a child
session stays unlocked, so the unit fails where the journal shows it.

*Unlocking.* light-locker never sets `LockedHint`, and it unlocks behind the greeter:
`loginctl unlock-session` alone left the seat on a password prompt. The grant path looked
only at `LockedHint`, so "+10 dk" unlocked nobody. A grant now unlocks every `user`
session of the child - a no-op on one that is not locked - and activates it when, and only
when, the seat is showing a greeter, so a parent working in their own session keeps their
screen. The daemon's re-lock on every tick is left as it is: with no `LockedHint` it
repeats `lock-session` against a locked screen, which the VM showed to be harmless.

*The browser policy.* Debian's `chromium` does not create `/etc/chromium/policies/managed`,
so the `-` entry in the daemon's `ReadWritePaths=` was skipped, `/etc` stayed read-only
and the D-151 policy was never written. The first thing the child saw was Chromium
offering to translate the page, with its `⋮` menu. `biokids-daemon` ships the directory.

*Whose account.* Checking that a parent's account is never made passwordless showed that
provisioning still wrote the kiosk's session files into it - autostart included - and
nothing stopped the panel creating a child whose `unix_user` was the parent's own, whose
sessions the daemon would then ration and power off. An account in `sudo`, `adm` or
`biokids` is now refused as a child's, by provisioning and by the child form alike, with
its own message. A name BioKids *suggests* also steps past any existing account that is
not already a BioKids child's - a child called Bilal on a machine whose parent logs in as
`bilal` becomes `bilal2` - while one that is, left over from a purge, is reused, drawings
and all.

*The hyphenated child.* The runbook adds `elif-nur` for exactly this reason, and it found
it: `biokids-provision@.service` passed `%i`, the *escaped* instance, so adduser was asked
for `elif\x2dnur`, refused it, and the second child had no account. `%I` is the unescaped
one. The panel said so, as D-152 intended - which is the only reason it was seen at once.

**D-156 [claude] The child's browser policy names their web tiles; every child starts with tiles.**
Found by tapping the first web tile on the VM, once D-155 let a child log in at all.

*"This page is blocked."* Chromium on Linux has one managed-policy source for the whole
machine, `/etc/chromium/policies/managed` (D-149), so the kiosk policy of D-151 - block
everything, allow the launcher's origin - also governed the window a web tile opens. TRT
Çocuk, a tile the parent had enabled, opened on Chromium's "Your organization doesn't
allow you to view this site". The allowlist is now the launcher's origin plus the host of
every web tile enabled for any child, `www.` dropped so that subdomains are covered, and
the daemon rewrites the file on every `reload` as well as at start. Everything else stays
blocked. The same file governs a parent's own Chromium on that machine, which was already
true and is written down here: on a family machine the parent uses the panel from their
phone, or another browser.

*A keyring for a six-year-old.* The tile's Chromium kept passwords in GNOME's keyring,
which on an account with no password asked the child to choose one for a "Default
Keyring". It now runs with `--password-store=basic`, as the kiosk already did. After
the VM was powered off, the same window also opened with "Restore pages? Chromium didn't
shut down correctly" - which every end-of-day shutdown would produce. Both it and the
kiosk now pass `--hide-crash-restore-bubble`, the spelling current Chromium reads.

*An empty launcher.* The wizard's "Şimdilik geç" on the app step is a link straight to
step 6, and the children screen never chose apps at all, so both left a child with no
tiles - not even volume or shutdown, which D-49's "every step is skippable with a sane
default" had promised. A child with no tiles now gets the age-appropriate, installed ones
when they are created. Apps installed afterwards still reach an existing child only
through the wizard: the panel has no apps screen yet, a gap for Phase 1.

**D-157 [claude] The panel shows Tailscale's approval page; Tailscale's own logs stay home.**
Found in runbook §3.3, the first time the product met a real, brand-new tailnet.

*Serve starts switched off.* On a new tailnet `tailscale serve --bg 7878` prints "Serve
is not enabled on your tailnet. To enable, visit: https://login.tailscale.com/f/serve?..."
and then waits for somebody to open it. The panel gave it fifteen seconds, threw away what
it had printed, and told the parent "Telefondan erişim ayarı değiştirilemedi" - a dead end
for every family on day one. `run_command` now keeps a timed-out command's output,
`TailscaleRemote.enable` recognises the approval page, and the settings screen and the
wizard's step 8 show it as a button with one sentence of explanation. Only a link of
exactly the form `https://login.tailscale.com/f/<word>?node=<id>` is ever shown, and it
travels through the settings table rather than the redirect's query string, so the panel
cannot be made to present somebody else's link as a setup step. On the VM approving that
page was not enough: `serve` kept refusing until *HTTPS Certificates* was enabled on the
tailnet's DNS page, which the runbook now says.

*HTTPS has a public side.* Tailscale's certificates are ordinary publicly trusted ones, so
enabling them writes the machine's `*.ts.net` name into the Certificate Transparency logs.
D-33 chose real HTTPS for good reasons and that stands; the cost is now written down, in
`SECURITY.md` and in the panel's sentence under the approval button. The name is public;
the panel, its traffic and who may reach it are not.

*Tailscale's client logs.* Out of the box `tailscaled` uploads its own diagnostic logs to
`log.tailscale.com`, which principle #1 does not allow on a BioKids machine. The runbook
starts it with `--no-logs-no-support` and `SECURITY.md` lists the destination as turned
off. BioKids does not manage `/etc/default/tailscaled` itself: the parent installs
Tailscale, and a package editing another package's configuration is its own problem. The
cost of the flag is Tailscale's support, which a family that needs it can turn back on.

**D-158 [claude] A child's screen locks when BioKids locks it, and never on its own.**
Found from the phone, of all places: the Today card said Elif's session was not open,
and on the VM she was at a lock screen nobody had asked for. light-locker's default is to
lock five seconds after the X screensaver starts, and X starts it after ten idle minutes.
With D-155 that is a trap: the account has no password, and the login-by-picture rule
refuses a child whose session is already open, so a child who walked away for ten minutes
came back to a lock only a parent could lift - by granting time they did not need.

The child's session now starts light-locker with `--lock-after-screensaver=0
--no-late-locking --no-lock-on-suspend --no-lock-on-lid`, through a
`~/.config/autostart/light-locker.desktop` that provisioning puts beside the launcher's
and that overrides the system entry of the same name for that account only. The parent's
own session keeps locking when idle; the child's screen may still blank, and any key
brings it back. What locks a child is logind's Lock, which is what BioKids sends when the
allowance ends or the daemon dies. Checked on the VM both ways: forcing the screensaver
left Elif in her session, and `loginctl lock-session` still took the seat to the lock
screen. Provisioning now copies every `.desktop` it ships, not only the launcher's. A
file added this way reaches an existing child only when they are provisioned again,
which nothing does on upgrade yet.

**D-159 [claude] What T8.5 and T5.7 found on a clean machine.**
Runbook sections 5 and 6, run on the `clean-debian-13` snapshot with the D-158 packages.
Everything the runbook asks of them passed: an upgrade with the child logged in took 19
seconds, locked nobody and kept both the ledger and an edited `config.toml`; remove kept
the data; purge asked, and obeyed "hayır" and "evet"; a reinstall asked again; an
unattended purge kept the data; and in the child's session Ctrl+Alt+F2 to F6 and
Ctrl+Alt+Backspace did nothing, with no getty on tty2-6. Four things were wrong on the way.

*The purge question was never Turkish.* `debian/biokids-core.templates` said
`Description:`; po2debconf translates only fields written `_Description:`, so
`debian/po/tr.po` never reached the package and a Turkish machine asked in English. Fixed,
and checked on the VM: "Bu ailenin BioKids verileri silinsin mi? [evet/hayır]".

*"block: not found" on every removal.* debhelper expands the first debhelper marker it
finds, and both prerm scripts mentioned it in a comment above the real one. The generated
code was spliced into the comment and the comment's tail ran as a command. Harmless only
because it came before `set -e`. The comments no longer name the marker, and
`dev/check_packaging.py` refuses a maintainer script where it is not exactly once, alone
on its line.

*The runbook's upgrade removed a package.* It upgraded the six core packages, but §2.1 had
also installed `biokids-desktop`, which pins `biokids (= ${binary:Version})`; apt resolved
that by removing it, silently under `-y`. The runbook now upgrades all seven. An upgrade
from the archive brings all of them at once, so the product was not affected.

*The greeter is not yet a set of pictures.* On a fresh machine lightdm-gtk-greeter opens on
"Diğer..." with a user-name field, and a child has to find their name in a drop-down.
D-155 made choosing it enough; D-99's large pictures need a child-friendly greeter, which
is Phase 1 work.

`dev/answer_debconf.py` answers the purge question once it appears, so section 5.4 can
be run over ssh; debconf ignores anything typed before it asks.

**D-160 [owner] Warnings are heard, and they are seen over the app.**
The first run of the exit criterion on a fresh VM (runbook section 7): the 10-minute
warning appeared as a thin strip and a card mostly hidden behind Tux Paint, nothing was
said, and the owner's shutdown from the phone then hung for ninety seconds. Three faults,
each invisible to a test suite that runs in one process as one user.

*Nothing ever spoke.* The daemon publishes SPEAK and never makes a sound; the voice
service runs in the child's session, and the child may not open the daemon's socket
(D-123). Something in the session had to carry each line across, and nothing did - the
page drew a card from the event and that was all. `biokids_shell/relay.py` now holds one
subscription to the child's event stream for the life of the launcher and forwards SPEAK
to the voice service. One subscription, not one per browser tab, which would have spoken
once per tab and not at all with none open.

*And the voice service could not have spoken anyway.* It runs as the child, and looked
for cloned voices and stock models under `/var/lib/biokids`, which is 0700 root. Python
3.13's `is_dir()` raises `PermissionError` there instead of answering, so every line
crashed the worker before espeak-ng was tried. Unreadable is now answered as absent and
logged once; espeak-ng speaks. Before voice cloning ships, those directories have to move
somewhere a child's session may read.

*The warning was behind the app.* It was drawn inside the launcher, and the launcher is
behind whatever the child opens. The owner chose a short full-screen pause over a corner
card: at ten, five and one minute the relay raises the launcher with xdotool (a new
Depends of biokids-shell, BSD-licensed, run as a separate process) for seven seconds -
the speaking character, the sentence, the ring - and then gives the screen back to the
app that had it. The launcher is found by the WM_CLASS the kiosk now sets with `--class`.
The voice service's reply carries the sentence it actually chose from the line files and
the character it chose; the relay keeps it and the page asks for it at `/spoken`, so what
a child reads is what they hear, in the face of whoever is saying it (VOICE.md section 7).
Before, the card showed a catalog sentence while the voice said a different one.

*The shutdown hang.* The Today screen holds an event stream open, typically from the
parent's phone, and uvicorn's graceful shutdown waited for it without limit: "Job
biokids-panel.service/stop running" until systemd killed it at ninety seconds. Both the
panel and the launcher now bound it at three seconds. Checked on the VM: the panel stopped
in one second and the machine was off seven seconds after the end action.

The second run, on the same VM: warnings at 13:45 and 13:49 brought the launcher in front
of Tux Paint and were spoken, the quota ran out at 13:50 and the machine powered off at
13:52, all heard by the owner. The voice is espeak-ng, and it sounds like it.

**D-161 [owner] Zio and Sio sound like people wherever the machine allows it.**
After the second run of section 7 the owner heard the warnings and called the voice
robotic. Of the four ways offered, they chose the two that need no new dependency: a
gentler espeak-ng, and the MBROLA voices where a family installs them. A bug was found on
the way.

*espeak-ng, gentler.* 140 words a minute instead of 150 (espeak-ng's default is 175), and
its female variant `f3` for Zio and male `m3` for Sio. The pitches had been the wrong way
round: Zio, the mother (D-85), at 35 and Sio, the father, at 75 - the opposite of what
VOICE.md section 3 said. Now 55 and 40.

*MBROLA where a family installs it.* `espeak-ng --voices=tr` lists `mb-tr2` as female and
`mb-tr1` as male, so Zio speaks with tr2 and Sio with tr1. The voices are in non-free and
the `mbrola` program in contrib - not non-free, as D-137 and the package description had
it - so a family enables both components and installs `mbrola-tr1 mbrola-tr2`. Nothing in
BioKids depends on them; they stay a Suggests. mbrola is AGPL-3 and runs as a separate
process, never linked (D-30). Without them, nothing changes but the tuning above.

*BioKids drives MBROLA itself.* Letting espeak-ng do it (`-v mb-tr2`) loses sounds:
espeak-ng 1.52's Turkish produces three phonemes - `L/`, the l after a back vowel, `l/`
and `&` - that its own `tr1_phtrans` table does not translate, and MBROLA speaks each
unknown diphone as silence. That is the l of every "kaldı", in every warning there is.
Upstream's table has the same gap. So espeak-ng is asked only for the phonemes (`--pho`),
the three are renamed to the `l` and `e` the tr1/tr2 diphone sets do have, and `mbrola` is
run on the result, writing a WAV whose header knows its length. Checked on the VM: all
nineteen Turkish lines, not one unknown diphone. The tr2 recording is a third as loud as
espeak-ng; mbrola's `-v` evens the two out (2.6 and 1.5). If MBROLA fails, the same engine
speaks with espeak-ng's own voice and the log says why.

*The cache remembers what made it.* The key names the sentence and the voice, never how
they were synthesised, so a family who installed MBROLA would have gone on hearing the
robot for every line already heard once - which, a week in, is every line. espeak-ng's
cache directory now holds a `.made-by` line - the tuning number and the MBROLA voices in
use - and is emptied when that changes, including on the first start after this upgrade.
Which voices are in use is decided once, when the service starts, so a family hears the
change from the child's next login.

**D-162 [owner] The ring carries the minutes, for every age.**
After using the launcher as a child, the owner asked for the minutes left to be written
inside the ring in the top right corner. UX.md section 2.2 had kept the 4-7 band to the
circle alone, on the grounds that a number a child cannot read is noise. The owner's
view: the circle still says everything to the child who cannot read, and the number
says it at a glance to the parent standing behind them and to the child who can. So the
number is shown at every age, with "dk" under it, and the clock stays 8-10 only. The
number stays dark whatever colour the ring turns: amber on the launcher's off-white
would fail the AAA contrast the launcher keeps. The full-screen time warning (D-160)
shows the same number in its ring, and a screen reader hears "Kalan süre: 42 dk".

**D-163 [owner] Every app has a big close button, always in the same corner.**
The owner, using the launcher as a child, opened "Gökyüzü" (Stellarium) and could
barely get out. The window manager was configured with no window buttons at all
(`button_layout = "|"`) and with maximised windows losing their title bar, and
Stellarium starts full screen with its menus hidden. Nothing on the screen said how
to leave. The owner chose, over a floating home button that would cover part of every
app, a child-sized title bar:

* A BioKids xfwm4 theme with a 64-pixel title bar and one button on it: a 56-pixel
  coral square with a white cross, 64 by 64 to click (UX.md section 2.4's minimum for
  4-7). White on `--danger` is 4:1, which a control needs; `--bio-coral` would be 2.9:1.
  `dev/make_xfwm4_theme.py` draws it; the PNGs are committed.
* Every ordinary app window is maximised by devilspie2 with a one-line rule, so the
  button is always in the top right corner of the screen. Dialogs, menus and the
  launcher are left alone. devilspie2 is a new Depends of biokids-shell: Debian main,
  GPL-3, run by the launcher as a separate process (D-30). xdotool could not do it:
  Debian 13 has the 2016 release, which has no `windowstate`.
* Apps that start full screen are started windowed through their catalog `exec`
  (Stellarium `--full-screen no`, Tux Paint and TuxMath `--windowed`, GCompris and
  SuperTux `--window`, Frozen Bubble `--no-fullscreen`). Catalog sync updates `exec`
  of existing rows, so this reaches installed machines.
* The child's Chromium is told to use the window manager's title bar
  (`browser.custom_chrome_frame: false`), so a web tile has the same button in the
  same place. Written only while that browser is not running.
* The launcher applies the window manager keys live through `xfconf-query` at the
  start of every session (new Depends: `xfconf`). Provisioning never overwrites a file
  already in a child's home, so changing `xfwm4.xml` alone would have reached new
  children only, and an open session would have written its old settings back at
  logout. The same keys stay in `xfwm4.xml` for a child's first login; a test keeps the
  two in step.

A game that switches itself to full screen from its own menu still hides the bar; the
time warning still brings the launcher forward over it. The launcher's kiosk window has
no title bar and is not affected.

**D-164 [owner] Safe sites a parent can add, on a screen a parent can find.**
The owner asked for a children's encyclopaedia, a safe games site and YouTube Kids.
Each candidate was checked from Turkey on 2026-10-03 for ads, accounts and what it
actually offers:

* **KÜRE Çocuk** (`kureansiklopedi.com/tr/cocuk`), the children's section of the
  T3 Foundation's encyclopaedia: Turkish, no ads seen, no login. There is no Turkish
  children's Wikipedia: Vikidia has no Turkish edition and Turkish Wikijunior has 120
  pages. The `vikipedi-cocuk` tile stays what it always was, general Wikipedia at 8+.
* **Bilim Çocuk** and **Meraklı Minik**, TÜBİTAK's two children's magazines: no ads,
  issues older than four months free.
* **Kodlama** (`studio.code.org`): Turkish interface, no advertising by policy, most
  courses need no account.
* **YouTube Kids** (`www.youtubekids.com`): works signed out with a content level a
  parent picks, but shows short ads without Premium. In the catalog with
  `suggest = false`: offered, never ticked for a parent.
* **PBS KIDS Games** (`pbskids.org/games`): ad-free, every host under `pbskids.org`,
  English only; also `suggest = false`.

No Turkish site offers ad-free games that play in a browser. TRT Çocuk's game pages
only link to app stores, so its tile no longer promises games, and Minika carries ad
networks. GCompris remains the best Turkish games tile, and it works offline.

A web tile is allowed its URL's host *and path*, not the whole host: KÜRE Çocuk opens
the children's section, not the encyclopaedia written for adults. Chromium's URL filter
matches the path as a prefix and governs pages and frames only, so a site's images and
scripts need no entries. Hosts without a path are unchanged.

And the screen: **Çocuklar → Uygulamalar ve siteler**, one checkbox per app and site
with what it is, the ages it suits and the address it opens. Until now only the wizard
asked, so a site added to the catalog later could never reach a child already set up.
Volume, brightness and the shutdown tile cannot be unticked. Saving is audited as
`apps_changed` and reloads the daemon, which rewrites the browser policy.

*Amended 2026-10-04 (owner).* Two tiles did not open in allowlist mode and the owner
saw both: Google Earth draws its globe in a frame under `earth.google.com/static/`,
which a tile allowed only `earth.google.com/web` could not load, and YouTube Kids
checks a silent sign-in through `accounts.google.com` before it shows anything. A
catalog entry may now carry an `allow` list, the exact patterns its tile needs; an
entry without one is allowed its host as before. KÜRE Çocuk keeps its path
(`kureansiklopedi.com/tr/cocuk`); Earth, YouTube Kids, Code.org and PBS KIDS are
allowed their hosts. The same list is what the DNS filter never blocks (D-167).

Also at the owner's request:

* **Dünya** (Google Earth in the browser) replaces **Gökyüzü** (Stellarium), which
  the owner found hard to use and hard to leave. Earth needs WebGL: on a VirtualBox
  machine without 3D acceleration it says "WebGL is not supported"; on real hardware
  it runs. Stellarium is no longer recommended by biokids-desktop.
* **Simpler coding.** The Kodlama tile now opens Code.org's *Pre-reader Express*
  (ages 4-7, no reading needed) in Turkish, and a second tile its *Express* course
  (8-10). **Blok Oyunları** (Blockly Games, 6-10) is Google's open-source set of
  coding puzzles: Turkish, no ads, no account.
* A tile whose program is not installed is not drawn, and the panel does not offer
  it. Debian puts games in `/usr/games`, which no service's PATH contains, so the
  launcher looks there and starts a game by its full path. GCompris, TuxMath and
  Frozen Bubble were drawn on the owner's machine and did nothing when tapped,
  because the launcher had looked for them by name on that PATH.

**D-165 [owner] The child can ask: a local assistant, "Sor Bana".**
The owner asked for a local language model a child can ask questions of, by typing
or by speaking, that answers for the child's age, refuses what a child should not
be asking ("porno nedir" gets "Buna cevap veremiyorum") and tells the parent when it
refuses. This reverses D-92's "no LLM conversation" and ROADMAP's deferral of a
chat interface, by the owner's decision: children ask "why" all day, and a model on
the machine with a strict gate in front of it is a learning tool, not a companion.
Zio and Sio stay one-way; the assistant is a tile of its own.

What the child and the parent get:

* **Off by default**, switched on per child in **Çocuklar → Asistan**. The models
  download only when the parent presses the button there (D-165 adds that one-time
  download to SECURITY.md section 4 under the AI package row it already had).
* **The child knows** (principle 2). The assistant screen says, under the box:
  "Sorduğun bazı soruları ailen görebilir." The owner chose this over a silent log
  and over showing the parent everything: an ordinary question is answered and
  forgotten; only a refused one is kept, as an alert with the child's words and a
  category, on the Today screen and in the audit log.
* **Answers for the age band**: five short sentences for 4-7, seven for 8-10, with
  the real reason first and no invented facts; "Bunu bilmiyorum, bir büyüğüne
  sorabilirsin" when the model is not sure. Refusals are fixed sentences from the
  catalog, never model text: a kind one for self-harm that sends the child to a
  grown-up, one for personal information, one for questions a family answers
  (religion, death, politics), one for everything else.

How it is built:

* The launcher (child) asks the daemon over the shell socket. The daemon checks the
  child's policy, keeps a rate limit (20 questions in 10 minutes), records alerts,
  and passes the question to `biokids-ai`: its own system user, `PrivateNetwork=yes`,
  no home, no devices. That service runs `llama-server` from llama.cpp on a Unix
  socket, starts it on the first question of a session (the assistant screen asks
  for it as it opens) and stops it after 10 idle minutes, so the memory is back for
  the child's games.
* **Five steps, each able to refuse.** (1) A lexicon of Turkish and English words and
  phrases, matched after Turkish-aware lowercasing, leetspeak and repeated-letter
  folding, refuses before any model sees the question. (2) Greetings and small talk
  skip the classifier, which had filed "selam canım" under family matters. (3) A
  classifier call at temperature 0 whose output is a JSON enum of categories. (4)
  The answer, written with a system prompt for the age band, the child's name and
  one worked example per language and band. (5) The lexicon again, on the answer.
* **Fail safe** (principle 3): no model, a crash or a timeout gives the child "Şu an
  biraz dinleniyorum" and answers nothing unchecked.
* **Models**, pinned by revision and SHA-256, fetched from Hugging Face once:
  Gemma 4 E4B (Apache-2.0) where the machine has 12 GB of memory, Gemma 4 E2B
  (Apache-2.0) from 7 GB. Qwen 3.5 2B was tried and dropped: it stated wrong facts.
  E2B still does so more often than E4B, which the owner noticed against a cloud
  model's answer; the panel says which is in use and what that means.
* **Packages.** llama.cpp and whisper.cpp are separate processes (MIT). Debian 13
  does not have the versions with Gemma 4 support; until trixie-backports does, the
  BioKids repository is to carry rebuilds of sid's `llama.cpp-tools` and
  `whisper.cpp-tools` against trixie-backports' `libggml0`. `biokids-ai` stays a
  Suggests (D-82): nothing else needs it.

On the development VM (VirtualBox, no GPU, four threads; AI-LAYER.md section 12 on
threads) an answer takes 10 to 30 seconds with E4B once it has warmed up. In the
owner's comparison with a cloud model the remaining gap is facts, not style: E4B
still slips now and then (the Moon "going round the Sun"), which no prompt removes
from a 4-billion-parameter model. Real hardware has not been measured yet; that
measurement decides whether E4B stays the default on 12 GB machines.

**D-166 [owner] The microphone, for push-to-talk in the assistant only.**
The owner asked for the assistant to be usable by speaking, because the children it
is for cannot all type. SECURITY.md section 7 and CONTRIBUTING.md listed microphone
capture as out of scope, so the owner was asked, and agreed to these conditions,
which are the whole of the exception:

* Off by default; switched on per child, separately from the assistant itself.
* Only while the child holds the button on the assistant screen, which says
  "Dinliyorum" while it listens, and for at most 15 seconds.
* Only the launcher can use it. Chromium's managed policy sets
  `AudioCaptureAllowed: false` with `AudioCaptureAllowedUrls` naming the launcher's
  origin alone, and `VideoCaptureAllowed: false`: no website can open the
  microphone, and nothing can open a camera. A page in the child's browser cannot
  post audio in the child's name either: the launcher's assistant routes accept
  only `audio/wav` or `application/json` from its own origin.
* The recording exists in memory only: a 16 kHz WAV built in the page, sent to the
  daemon, passed to `biokids-ai` and transcribed by `whisper-server` (Whisper small
  from 3 GB, large-v3-turbo from 12 GB, MIT) on the machine. It is never written to
  disk, never logged and never sent anywhere. The text then goes through exactly
  the same steps as a typed question.

Recording a room, listening in the background, wake words and saving audio stay
out of scope; SECURITY.md and CONTRIBUTING.md say so next to this exception.

**D-167 [owner] Open internet by default; the worst of it closed by DNS.**
The owner wants a child's internet open unless a parent chooses otherwise, with
pornography and the like closed whatever the parent chooses, and "only the sites I
choose" there for a parent who wants it. That overrides D-61, which defaulted 4-7 to
the allowlist. Every new child now starts on **Açık internet, zararlı siteler
kapalı**, and **Çocuklar → İnternet** switches between the two.

* **A DNS filter for the children only.** `dnsmasq` from `dnsmasq-base` (GPL-2,
  a separate process, D-30) runs as `biokids-dns.service` and listens on
  `127.0.0.1` and `::1`, port 5354, nowhere else. An nftables table, `inet biokids`,
  redirects DNS from the children's uids to it and rejects their DNS-over-TLS and
  their HTTPS to the known DNS-over-HTTPS resolvers; Chromium's own DoH is off by
  policy. The parent, the system and every other user resolve as before.
* **The lists**: ShadowWhisperer's BlockLists (Unlicense), adult, gambling,
  malware and scam, and known DoH hosts, about 291,000 domains, shipped gzipped in
  biokids-core and refreshed at release time by `dev/update_blocklists.py`. They
  arrive by APT with BioKids, so there is no new outbound call. This replaces
  D-62's opt-in, downloaded StevenBlack list for these categories: the owner wants
  them closed whatever a parent chooses, and offline (principle 5). D-62's weekly
  refresh is not built.
* **SafeSearch by DNS** as well as by policy: Google answers as
  `forcesafesearch.google.com`, YouTube as `restrict.youtube.com` (strict), Bing as
  `strict.bing.com`, DuckDuckGo as `safe.duckduckgo.com`.
* **A site a parent enabled is never blocked**: its `allow` patterns (D-164) are
  sent upstream unfiltered.
* **Fail safe** (principle 3): the redirect is loaded first and on its own. If the
  lists cannot be read the filter is stopped, and a stopped or crashed filter
  answers nothing: the children's accounts then have no internet, never unfiltered
  internet. The Internet screen says whether the protection is running.
* **The browser.** "Only the sites I choose" is the allowlist policy as before.
  "Open" drops the `*` wildcard and keeps the panel's addresses and Chromium's own
  settings pages blocked. Chromium reads one managed policy per machine, so with
  two children on different settings the stricter one applies to both; the screen
  says so.
* **Removing BioKids** deletes the nftables table, so no child is left sending DNS
  to a filter that is gone (principle 4).
* **From the first second of a boot.** nftables rules do not survive a reboot,
  and the daemon loads them again only once it is up: on the third section 7 VM
  that was sixteen seconds after LightDM had started, and a child's DNS was
  unfiltered in between. `biokids-dns.service` now loads the rules the daemon
  last wrote (`ExecStartPre=+nft -f`) and is ordered before the display manager.

A security finding on the way: `biokids-daemon` depended on the `dnsmasq` package,
whose own service answers DNS on port 53 on every interface, so every BioKids
machine was an open resolver on its network. The dependency is now `dnsmasq-base`,
which ships no service. An upgrade cannot remove a package another one pulled in;
the release notes tell families to run `sudo apt remove dnsmasq`.

Not done: a blocked site shows Chromium's own "this site can't be reached" rather
than the block page of D-64.

**D-168 [owner] The child's browser speaks the family's language.**
YouTube Kids set itself up in English on the owner's machine. Debian's Chromium is
English without `chromium-l10n`, and a profile made then asks websites for English
for good. `chromium-l10n` is a new Depends of biokids-shell, and the launcher sets
the child's profile to ask for the family's language (`tr-TR,tr,en-US,en`) before
the browser starts, unless someone chose other languages in it.

**D-169 [owner] Phone access is one button: BioKids installs Tailscale itself.**
The owner, setting up the third section 7 VM, asked whether a family has to install
Tailscale and type 127.0.0.1 to use BioKids, and to make it easier. Tailscale is
not required: everything works on the machine itself, and Tailscale is only how the
panel reaches a phone (D-33). But reaching it meant a terminal: adding Tailscale's
apt repository, editing `/etc/default/tailscaled` so the client does not upload its
logs (D-157), `tailscale up`. Asked to choose, the owner kept Tailscale and took
the one-button version over opening the panel to the home network over plain HTTP,
which `remote.py` refuses to ship for the reasons it gives.

"Telefondan erişimi aç", in the wizard's step 8 and in the settings, now works from
any state. When Tailscale is up it publishes the panel as before. When it is not
installed, signed out or stopped, the daemon starts `biokids-remote-setup.service`,
which runs `biokids-admin remote-setup` (`biokids_core/tailscale_setup.py`):

* Writes `--no-logs-no-support` into `/etc/default/tailscaled` before the package
  is installed, so the client never uploads a log, not even at its first start.
* Adds `https://pkgs.tailscale.com/stable/<debian|ubuntu> <codename> main`, signed
  by Tailscale's repository key **shipped in biokids-core**, never downloaded, and
  pinned: a test computes the key's fingerprint from the file
  (`2596A99EAAB33821893C0A79458CA832957F5868`, Tailscale Inc.'s package signing
  key, fetched from pkgs.tailscale.com on 2026-10-06). Then `apt-get install
  tailscale`, with the parent's defaults file kept.
* `tailscale up`, waiting up to thirty minutes. Meanwhile the panel reads the
  sign-in page from `tailscale status --json` (only a `login.tailscale.com/a/`
  address is ever shown) and offers it as a button and as a QR code, so a parent
  can sign in on their phone.
* Publishes the panel with the same `remote.enable()` as before: Funnel is still
  refused, and the Serve approval page still shown (D-157).

The settings send the parent to a page of its own, `/settings/phone`, which
refreshes itself every five seconds while the setup runs; the settings screen,
with a PIN form on it, never does. The wizard's step 8 follows it the same way.

This adds an outbound connection: `pkgs.tailscale.com`, the package and its updates
through apt, only on a machine whose parent pressed the button. SECURITY.md section
4 lists it. If Tailscale ever rotates that key, the setup fails with apt's error
and BioKids needs an update; the runbook's manual steps still work.

**D-170 [owner] The login screen is BioKids', and the panel is in the menu.**
The owner saw a child's login screen with Debian's background and logo, the child
listed as `ahmet` in lower case, and Debian's grey silhouette for a picture. A child
of four does not read "ahmet". Three things were wrong, and one more was hard for a
parent:

* **The look.** `biokids-shell` ships
  `/usr/share/lightdm/lightdm-gtk-greeter.conf.d/50-biokids.conf` over Debian's
  `01_debian.conf`: a background with the BioKids mark and Zio and Sio, happy, on the
  launcher's warm off-white (`assets/greeter/background.png`, drawn by
  `dev/make_greeter_art.py`), no user backgrounds, and no host name in the top bar.
* **The picture.** Provisioning never set one, and the `.face` a new account gets
  from /etc/skel is Debian's silhouette, in a 0700 home the greeter cannot read
  anyway. It now gives AccountsService the child's panel avatar, drawn as a
  256-pixel picture with Noto Color Emoji on a pastel disc
  (`biokids_core/data/avatars/`); AccountsService copies it where LightDM can read it.
* **The name.** The unit that provisions a child knew only the user name, so the
  account's real name was the user name. It now reads the child's row, and the
  display name goes into GECOS (commas and colons removed). Children provisioned
  before this get both from `biokids-admin refresh-greeter`, run by biokids-core's
  postinst; without AccountsService a child keeps the silhouette and logs in all the
  same.
* **The parent's way in.** `biokids-panel` puts "BioKids Ebeveyn Paneli" in the
  applications menu, with the BioKids icon, so nobody needs to know the panel lives
  at 127.0.0.1:7878. And until setup is complete, an autostart entry opens the
  wizard in the browser of whoever logs in: the parent who just installed BioKids
  is taken to it rather than told about it. It does nothing in a child's session,
  and nothing once `/` stops leading to the wizard.

**D-171 [owner] The review's security findings, closed.**
An end-to-end review on 2026-10-06 found these, and the owner asked for all of them
to be fixed before anything new:

* **Root no longer follows a child's symlinks.** Provisioning writes into the
  child's home as root, and used `mkdir`, `copyfile` and `chown`, all of which
  follow links: `~/.config` pointing at `/etc` would have had root chown `/etc`
  to the child. Every level is now opened with `O_NOFOLLOW` relative to the one
  above, given to the child through its descriptor, and the file is refused unless
  it is a regular file with one name (`provision._write_into_home`).
* **The launcher answers only its own page.** Its form routes (power off, ask a
  parent, launch, volume) took a POST from any website the child opened. Every
  request must now carry a loopback Host (no DNS rebinding), and every change must
  not be cross-site by `Sec-Fetch-Site` and `Origin`.
* **The panel is closed to the children by nftables**, by port and the whole
  tailnet, instead of by three spellings in Chromium's policy. The policy no longer
  names it, so the parent's own Chromium opens it from the menu (D-170).
  `stop-dns-rebind` in the filter, and more DoH resolver addresses.
* **The browser policy** moved to `biokids_core.browser_policy`: the daemon writes
  it and imported it from `biokids_shell`, which `biokids-daemon` does not depend
  on, so the daemon failed to start without the launcher installed. It now also
  closes `file://`, typed `javascript:`, more of Chromium's own pages, guest and
  add-person, and clicking through a certificate warning.
* **Dependencies.** `biokids-shell` depends on the Python libraries it imports;
  `espeak-ng` is a Depends of `biokids-voice`, since the spoken warning is the
  exit criterion and `--no-install-recommends` used to silence it.
* **`biokids-ai` is not in the `biokids` group**, which can grant time and power
  the machine off; the assistant never opens the daemon's socket. Upgrades remove
  it from the group.
* **The recovery code must look like one.** The wizard hashed whatever its hidden
  field sent, so an empty code could be stored and then reset the PIN. A code is
  now eight words from the list or it is refused, at both ends.
* **The passphrase gate stops after five wrong tries in fifteen minutes**, counted
  from the audit log; the PIN fallback goes through the same function. Signing out
  needs the CSRF token, the alert page's `next` goes through `safe_next`, and the
  power-off confirmation no longer puts a child's name inside JavaScript.
* **The daemon bounds a grant (12 h) and a timeout (24 h)**; a negative grant used
  to take time away.
* **A database copy before every migration**: `biokids-admin backup-db`, run by the
  postinst, SQLite's online backup, three kept, in `/var/lib/biokids/backups`.
* **systemd**: the daemon keeps only `CAP_NET_ADMIN` and `CAP_CHOWN`, the panel no
  capability at all; both get the remaining `Protect*`/`Restrict*` settings, and
  neither may write `/etc/dnsmasq.d`, `/etc/nftables.d` or `/etc/biokids`, which
  nothing wrote.


**D-172 [owner] Fail-safe that is checked, not assumed.**
From the same review (D-171). The daemon is `Type=notify` with `WatchdogSec=90s` and pings only after a tick that finished; every command it runs is killed after 30 s and a tick after 60 s; a failed tick rolls the shared Session back. The DNS filter is checked from the outside once a minute (nft table present, NXDOMAIN for a canary): open means one repair, then every child at the machine is locked until it closes; not answering is reported. A lock is verified from logind (still on screen and unlocked; light-locker never sets LockedHint, D-155) and after three ticks the session is ended. The wall time the daemon uses runs on BOOTTIME (`GuardedClock`): a jump is ignored until NTP confirms it, so moving the date no longer resets the quota. The lockdown falls back to the `biokids-child` group when the database cannot be read and ends sessions that did not lock. The ledger carries fractions of a second and re-reads its day rows after a failed commit. Closing and `manager` sessions no longer count as a child at the machine.

**D-173 [owner] "Anneme sor" is answered: requests, unlock, grants of any size.**
A child could file a request since D-65 and nothing could answer it: the panel
counted them in a badge on the audit screen and offered no way to say yes. Now:

* **Three kinds**, a column on `access_request` (migration `3e8b51c0d2a7`):
  `time` (more minutes today), `site`, `app`. The launcher asks for time with a
  "Biraz daha süre iste" pill in its footer during the last fifteen minutes -
  before the lock, because after it there is nothing to press - and the block
  page asks for whatever stopped the child: out of minutes is time, an app not on
  their screen is that app, anything else the page.
* **One question is one request.** Asking again while it waits returns the same
  request; six in an hour is the limit, and the seventh is told so kindly.
  Unanswered requests expire after twelve hours.
* **İstekler**, a screen of its own, linked from Today and badged on its nav item:
  more time is answered with +15, +30 or +60 minutes, everything else with İzin ver
  or Şimdi değil. Yes to time is a grant (which also lets a locked child back in);
  yes to a site allows its host for the children by policy and by DNS (a
  `filter_rule` row, source `request`); yes to an app puts it on their screen.
* **The child is told**, on screen and, when the voices are on, by Zio or Sio
  (`request_approved`, `request_denied` lines). A question that disappears into
  silence teaches a child not to ask.
* **Today** gains "Kilidi aç", which lifts a lock without adding time - "Kilitle"
  had no way back but "+10 dk" - and grants of +10 and +30.

A site cannot yet be asked for from the browser itself: blocked names fail in
Chromium's own error page, and the block page that would carry the button (D-64)
needs the extension. The request kind and the approval path are ready for it.


**D-174 [owner] Zio and Sio in the parent's own voice, without cloning.**
Phase 2a's voice cloning is ninety seconds of audio, a GPU and a month; most of what a child would hear in it is seven sentences. **Ayarlar → Kendi sesiniz** lets a parent say those themselves, on their phone: welcome, 10/5/1 minutes left, time is up, and yes / not now to a request. The page records a mono 16-bit WAV itself (no MediaRecorder, whose formats differ by phone and which `pw-play` cannot read), the panel checks it (PCM, mono, 8-48 kHz, 0.5-15 s, 2 MB) and keeps it in `/var/lib/biokids-voices/<locale>/<event>.wav` with the sentence that was said. That directory is world-readable and outside `/var/lib/biokids`, because the voice service that plays it runs as the child; nothing else of the family's is in it and nothing in it leaves the machine. The voice service plays a recording before any synthesiser and shows the parent's sentence on the card, with Zio or Sio as the parent chose. A line nobody recorded is spoken as before. Purging with the data removes the recordings; the encryption key now goes only with the data as well, so a database kept across a reinstall can still read what it encrypted.

**D-175 [owner] For a child who cannot read yet.**
UX.md section 2.2 promised spoken labels for 4-7 and `data-speak` was rendered with nothing reading it. In the young band a tap now says the tile's name (throttled, so drumming on tiles is names, not a pile-up), and the full screens nothing else speaks - blocked, asked, first run, shutdown - say their sentence once as they open, through the voice service's `say` (a launcher route that, like the assistant's, takes JSON from its own page only). The ring keeps its minutes for every age (D-162); for 4-7 a sun rides the end of it and goes from yellow to orange to red as the time runs down: something to watch that needs no number.

**D-176 [owner] Hours are picked, not typed; some days can be different.**
The time screen asked a parent to type `16:00-20:00, 07:30-08:15` and refused anything else. Each kind of day now has two ranges of `<input type="time">`, the phone's own clock picker; half a range is refused with a sentence that says so. Under **Gelişmiş**, any day of the week can be ticked as different, with its own minutes and hours (`policy.schedule_json`, migration `9a4f2c7e1b36`): swimming on Wednesday is thirty minutes between six and seven. A day that names only its minutes keeps the usual hours, and an unticked day follows the weekday or weekend rule, so nothing changes for a family that never opens it.

**D-177 [owner] Mail to the parent, through their own server.**
"The parent is notified" meant an audit row nobody read until evening. **Ayarlar → E-posta bildirimleri** takes the parent's own SMTP server (SECURITY.md section 4 has listed it, opt-in, from the start); the settings are stored encrypted in `parent.smtp_json_encrypted` with the key in `/etc/biokids/secret.key`, and the password is never shown back. Once a minute the daemon mails, in one message, every new alert-severity audit row (a filter that failed, a lock that did not take, the daemon giving up), every request a child filed and every assistant alert; turning mail on starts from the present rather than sending the history. On Sunday from 19:00 it mails each child's time per day for the week. A mail that cannot be sent is retried with a growing pause and nothing is marked sent until it was. A message says what happened and to which child, never what a child typed: the panel holds the details. The send runs in a thread with a 20-second timeout, outside the tick's failure count, so a mail server that is down never stops supervision.

**D-178 [owner] The assistant checks itself against an encyclopedia on the machine.**
The gap the owner saw between Gemma 4 E4B and a cloud model was facts, not style
(D-165), and no prompt removes a small model's confident mistakes. A source does,
and it has to be local (principle 5, and the owner's "it must be a local LLM").
The same download button now also fetches the opening paragraphs of the most-read
Turkish and English Wikipedia articles as Kiwix packs them -
`wikipedia_tr_top_mini_2026-07.zim` (138 MB) and `wikipedia_en_top_mini_2026-09.zim`
(297 MB), CC BY-SA 4.0, pinned by the SHA-256 Kiwix publishes and checked after the
download. Before the model answers a question the classifier let through, its
subject is searched with `kiwix-serve` (kiwix-tools, GPL-3, in Debian; a separate
process, never linked) running inside the assistant's own network namespace on
127.0.0.1, started with the first question and stopped when idle like the model.
Up to two articles' opening text goes into the answer prompt as the facts to
prefer over memory, after the word list has screened it. Nothing found, a slow
lookup or a missing kiwix-serve is not an error: the model answers as before.
SECURITY.md section 4 lists the new download; `biokids-ai` Recommends kiwix-tools.

Two smaller fixes from the review: the classifier's input is quoted with
`json.dumps`, so a question containing a quote mark can no longer close the
quotation and append a verdict of its own; and the self-harm answer gives a child
with no grown-up nearby a number to call (183, and 112 for an emergency).

