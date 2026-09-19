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

**D-26 [owner] Single monorepo:** `github.com/bilalgurkansanli/biokids`.
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

**D-32 [claude] Jinja2 + HTMX + hand-written CSS. No React, no npm, no CDN.**
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
