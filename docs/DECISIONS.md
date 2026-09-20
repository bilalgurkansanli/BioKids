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
