# ROADMAP.md

Releases are ordered by risk, not by excitement. Each has an exit criterion that can
be demonstrated. Do not start a release's work before the previous one meets its
criterion.

There is no deadline. The effort estimates assume roughly one focused day per week.

Two releases, decided by the owner on 2026-10-07 (D-185): **the packages**, installed
from the project's APT repository on an existing Debian 13, published as **1.5.0**
(D-219: 1.0 was the release candidate, and five rounds of tests followed); then **the
ISO**. Both are released together as **2.0.0**, after the owner installed the ISO
beside Windows on a real computer (D-249 to D-255). What did not make v1 is listed
under *Cut from v1*, with its decision.

---

## v1.5.0 — the packages  *(was the v1.0 release candidate, 0.1.0-2)*

**Goal:** a family installs BioKids on stock Debian 13 with apt, from the project's own
repository (D-27), sets a daily limit, and the child's session enforces it with spoken
warnings, manageable from a phone.

### What is built

Every item below is written, tested, and in the `1.5.0-1` packages.

- **Time.** `CLOCK_BOOTTIME` accounting that a changed clock cannot cheat (D-40,
  D-172); daily quotas, time windows and per-weekday schedules (D-176); forced breaks
  and earned time; limits per kind of app (D-193); the end sequence of lock, countdown
  and shutdown, scoped to whoever is at the machine (D-138); warnings at 10, 5 and 1
  minute, spoken by espeak-ng, MBROLA or Piper and shown over the child's app (D-137,
  D-160, D-161), in the parent's own recorded voice where they made one (D-174).
- **The child.** A full-screen launcher for two age bands, a time ring with the minutes
  (D-162), login by picture at a BioKids login screen (D-155, D-170), read-aloud for
  children who cannot read yet (D-175), a big close button on every app (D-163), the
  "Sor Bana" assistant when a parent switches it on (D-165, D-166), and offline
  Wikipedia as a tile a parent opts in to (D-190).
- **Filtering.** The children's DNS through dnsmasq and nftables, harmful categories
  always closed, the open internet or only chosen sites (D-167); a machine-wide
  Chromium policy (D-171); the BioKids extension, which turns a closed site into the
  launcher's block page and reports host names for the history (D-187).
- **Asking.** "Büyüğüme sor" for more time, a site or an app, answered in one tap
  (D-173).
- **The parent.** The setup wizard (D-182); Today, Children, History (D-192), the audit
  log and Settings; editing and removing a child (D-191); mail through the parent's own
  server (D-177); phone access over Tailscale with one button (D-169).
- **The machine.** A daemon that locks the children when it, the filter or a lock fails
  (D-139, D-172); one launcher port per child, closed to everyone else (D-188); records
  deleted on a schedule (D-189); clean upgrade, remove and purge (T8.5); CI that
  installs, upgrades and purges on a clean trixie.

**Exit criterion.** On a fresh Debian 13 VM: `apt install biokids` → wizard → phone
reaches the panel over Tailscale → set a 20-minute quota → log in as the child → the
ring counts down → warnings at 10/5/1 spoken in Turkish → the machine shuts down →
the parent's audit log shows every event. Reboot; the counter resumes correctly.
Change the system clock forward 3 hours; nothing changes.

### Where it stands

`tests/test_acceptance.py` runs the criterion above end to end in one process. On a
Debian 13 VM, `docs/VM-RUNBOOK.md` §0–§6 have passed: the packages build, pass lintian
and install, the wizard completes, a child logs in by their picture, the panel opens on
a phone over Tailscale, upgrade, remove and purge behave, and the virtual-terminal
guards hold.

**What is left before v2.0.0 is tagged:**

1. `docs/VM-RUNBOOK.md` §7 — the criterion on a fresh VM, recorded — and §8, the
   checks for what the release adds.
2. The archive key. `biokids-archive-keyring` cannot be built until
   `debian/biokids-archive-keyring.asc` is committed (`debian/README.source`), and its
   fingerprint belongs in the README. Until then the release workflow cannot publish
   the APT repository.

The contact address is set (`SECURITY.md`, `CODE_OF_CONDUCT.md`), and the version is
2.0.0 everywhere `release.yml` compares it: `debian/changelog` (2.0.0-1),
`pyproject.toml` and `biokids_core.__version__`; the tag is `v2.0.0`.

The voice question is closed (D-137): no Turkish Piper voice may be redistributed and
`piper-tts` is not in Debian 13, so espeak-ng speaks and Piper is used wherever a family
installs it - which the panel now does with one button, downloading Piper and the
Turkish voice from their authors (D-196).

---

## Next — the ISO

**Goal:** a stranger downloads the ISO, writes it to a USB stick, installs it on an old
laptop, and has a working supervised machine in under 20 minutes without reading
documentation.

- built with `live-build` under `iso/`: package lists, branding, Plymouth theme, the
  BioKids packages from our own APT repository (`docs/PACKAGING.md` §3)
- installed by Debian's own installer, **preseeded** — not Calamares (D-185)
- the setup wizard on first boot
- boots in VirtualBox (BIOS, then UEFI) and from a USB stick on at least two real
  machines
- Secure Boot through Debian's signed shim, verified on real hardware before it is
  claimed (D-17)
- hosted on archive.org, mirrored on GitHub Releases when under 2 GB (D-28)

Started with D-249: the image carries the .debs itself, so it does not wait for the
APT repository; every `lb build` is 30–60 minutes (`iso/README.md`).

*Estimate: 10–15 sessions, most of it on real hardware.*

---

## Cut from v1

Decided by the owner on 2026-10-07. Not "later in v1": out, each with its reason.

| Cut | Decision |
|---|---|
| The AI screen watcher: event-triggered screenshots, OCR, a local VLM, risk scores. The assistant stays. | D-180 |
| Voice cloning (OpenVoice) and the ElevenLabs cloud backend. The parent's own recordings replace both. | D-181 |
| The wizard's email step, which asked nothing. Mail is set up under Settings. | D-182 |
| `/var/log/biokids` and its logrotate file. Everything logs to the journal. | D-183 |
| `biokids-harden` (GRUB password, recovery mode, USB boot). `docs/SECURITY.md` §6 says how to do it by hand. | D-184 |

---

## Later

Ideas that are still worth having, in no order. None is promised.

- per-application network control via nftables cgroups (D-66)
- per-app category overrides under Advanced (D-44)
- an opt-in extra blocklist for ads and tracking (D-62)
- launcher ports that cannot collide: two children whose uids differ by a multiple of
  100 share one today, and only one of them can be logged in at a time (D-188)
- a short demo video for the website (D-256)
- translation infrastructure so other languages can be contributed
- the screen watcher, only if it can be built without breaking principles 1 and 2
  (`docs/AI-LAYER.md`)

---

## Explicitly deferred

Do not start these. They are recorded so they stop being distractions.

| Idea | Why later |
|---|---|
| ARM64 / Raspberry Pi image | doubles the test matrix (D-11) |
| Wayland support | rewrites focus tracking, window raising and the kiosk (D-12). v2 |
| Android companion app | the PWA covers it |
| Cloud sync / family accounts | directly contradicts the privacy promise |
| ~~A chat interface for the child~~ | built as the assistant at the owner's request (D-165); Zio and Sio stay one-way |
| Flatpak / Steam support | sandbox escape routes |
| School or multi-device management | different product |
| Ages 11–13 | different threat model; would require honesty about what we can't do |

---

## Milestones worth announcing publicly

1. v2.0.0 on the APT repository — a post with the screen recording of
   `docs/VM-RUNBOOK.md` §7.5
2. The first ISO — a Reddit r/linux post and a DistroWatch submission

Do not submit to DistroWatch before the ISO installs on real hardware. They do not list
unfinished projects and a rejection is hard to undo.
