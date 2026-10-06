# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions stay in `0.x` until Phase 1 is complete (`docs/PACKAGING.md` §1). Until then a
minor bump may change the database schema, the config file format or the IPC vocabulary;
each such change is listed here with what it means for an existing installation.
`1.0.0` is the point where a stranger can install the ISO and expect to be supported
afterwards.

## [Unreleased]

Phase 0 — the BioGuard MVP — is in progress. Nothing has been released or tagged yet;
the exit criterion is in `docs/ROADMAP.md` and `v0.1.0` is tagged when it is met
(`docs/TASKS-PHASE0.md` T9). `pyproject.toml` already carries `0.1.0`: that is the
version being built towards, not one that has shipped. What follows is what has landed
so far.

### Added

- **Repository scaffold.** The layout from `CLAUDE.md` §4, the MIT licence, and
  `pyproject.toml` carrying the ruff, `mypy --strict` and pytest configuration in one
  place. GitHub issue templates (bug, feature, hardware compatibility) and a pull request
  checklist that includes the i18n, decisions and outbound-network rules.
- **CI.** One workflow on every push and pull request: `ruff check`, `ruff format
  --check`, `mypy --strict` over core and daemon, the translation catalog check, pytest
  with coverage, a dependency licence allowlist enforcing D-30, and a `.deb` build inside
  a `debian:trixie` container gated on `lintian --fail-on error`.
- **`biokids-core` configuration.** A loader for `/etc/biokids/config.toml` with schema
  validation and defaults, so every path and port from `CLAUDE.md` §5 is a value rather
  than a literal. `BIOKIDS_PREFIX` re-roots the entire runtime layout, which is what lets
  the suite and a development panel run without root.
- **Database schema.** SQLAlchemy models for `docs/ARCHITECTURE.md` §5 plus the
  boot-epoch, ledger-day and panel-session tables that section implies, with Alembic and
  an initial migration. SQLite in WAL mode, `foreign_keys=ON`, mode 0600 on the file.
- **Internationalisation.** gettext catalogs for `tr` and `en` with the Turkish written
  first, a `_()` helper, and `dev/check_i18n.py`, which fails the build when a key exists
  in one locale but not the other or when a translation is left empty.
- **Policy engine.** `PolicyEngine.evaluate()` implementing the precedence rules of
  `docs/ARCHITECTURE.md` §4 — pure, with no I/O and no clock access; the wall-clock time
  is a parameter.
- **Time ledger.** `CLOCK_BOOTTIME` accounting with boot-epoch detection and carry-over,
  suspended time derived from the BOOTTIME-minus-MONOTONIC delta rather than trusted from
  logind, an unexplained gap clamped instead of billed, and wall-clock jumps written to
  the audit log without ever adjusting the balance. Moving the system clock neither
  creates nor destroys quota.
- **Security primitives.** argon2id hashing for the PIN and the passphrase
  (`m=64MiB, t=3, p=4`), an eight-word recovery code, and encryption at rest for the SMTP
  password.
- **IPC protocol.** Line-delimited JSON over `/run/biokids/daemon.sock`, with the
  operation and event vocabulary the packages share and both a blocking and an asyncio
  client.
- **Repository layer.** The single place stored rows become the engine's frozen
  `ChildPolicy`, so a schema change breaks in one file instead of four.
- **App catalog.** Applications as data (D-20), synchronised into the database
  additively so that an upgrade which drops an application does not lose a family's
  arrangement of tiles.
- **Tests.** The `docs/TASKS-PHASE0.md` T2.5 ledger list in full — an hour of use costs
  an hour, three hours of clock movement costs nothing, a reboot carries the total
  forward, a crash costs at most one persist interval, suspend and a locked screen cost
  nothing, two sessions for one child are counted once — plus every precedence rule and
  edge case of the policy engine.
- **`biokids-daemon`.** The logind session watcher, reducing sessions to one boolean per
  child so a fast user switch cannot bill the same second twice; the enforcer, which
  locks exactly the sessions the watcher attributed to that child and never every session
  on the machine; a bounded event bus that drops a slow subscriber rather than waiting
  for it; the tick loop; and the end-of-time sequence, whose countdown is kept in the
  database so a restart mid-countdown resumes rather than starting the two minutes again.
- **`biokids-panel`.** The ten-step setup wizard, the Today screen with live counters
  over server-sent events, per-child time rules, the audit log with filters, and the PWA.
  Argon2id PIN with a lockout that doubles and survives a restart, a thirty-minute idle
  timeout enforced on read, the separate passphrase gate, and the eight-word recovery
  path. It binds to loopback and refuses anything else.
- **`biokids-shell`.** The child launcher: the tile grid for both age bands, the seven
  screens of `docs/UX.md` §2.5, applications started in their own systemd scope so
  category time comes from a cgroup rather than a window title, and a kiosk Chromium with
  a managed policy allowing exactly one origin. Plus the XFCE and window-manager
  configuration that closes what a web page cannot.
- **`biokids-voice`.** Zio and Sio: line files as data, a Piper backend with an atomic
  cache keyed on the sentence and the voice, a priority queue that never interrupts a
  line in progress, PipeWire ducking that restores the previous volume even if playback
  is killed, and the fallback chain ending at an on-screen card that always carries the
  spoken line as text.
- **Remote access.** Tailscale detection, `tailscale serve` for the panel port, a
  server-rendered QR code for the wizard, and a warning when Funnel has made the node
  reachable from the internet. There is no LAN-exposure option and there will not be one.
- **Debian packaging.** One source package producing nine binary packages, systemd units
  carrying the full hardening block of `docs/SECURITY.md` §2, maintainer scripts that are
  idempotent and never destroy a family's history on `remove`, and a release workflow
  whose signing key never leaves GitHub Secrets.
- **A second daemon socket** (D-123). The child cannot be in the `biokids` group — that
  group can grant time and power the machine off — so the launcher has a socket of its
  own on which no operation takes a child id as an argument. The daemon identifies the
  caller from the peer credentials the kernel supplies and answers about that child only.
- **An end-to-end acceptance test.** `docs/ROADMAP.md`'s Phase 0 exit criterion, run in
  one process against the real wizard, policy engine, ledger, daemon loop, launcher and
  both sockets' handlers.
- **Development tools.** `dev/fakeclock.py`, which prints the whole end-of-time sequence
  including the Turkish lines in a few seconds; `dev/seed.py`; `dev/reset.sh`;
  `dev/compile_i18n.py`, a pure-Python `.mo` compiler so the build needs no `msgfmt`;
  `dev/make_icons.py`; and `dev/screenshots.py`.
- **Project hygiene.** `CONTRIBUTING.md`, including the explicit list of features this
  project will not accept (`docs/SECURITY.md` §7); `CODE_OF_CONDUCT.md`; a root
  `SECURITY.md` disclosure policy; and this changelog.

### Fixed

Found while building, and worth recording because each was a wrong behaviour rather than
a crash:

- **"+10 dk" was worth less than ten minutes** when a child had used less than that. It
  subtracted from usage, which cannot go below zero; it now raises the day's allowance
  (D-124). It also re-arms any warning the extra time pushed back into the future.
- **Reading a child's remaining time created a database row**, so the daemon held
  SQLite's single write lock between ticks and the panel — a separate process on the
  same file — could not log anyone in. Found by running both against one database.
- **The panel held its own write transaction across its call to the daemon**, because
  resolving the session cookie touches `last_seen_at`. The two waited for each other.
- **The panel reported "no children" when the daemon was down**, which is alarming and
  untrue: only their live state was unknown (D-127).
- **`time_up` could be dropped from a full speech queue**, because priority came from the
  caller rather than from the event.
- **A killed Piper left a truncated WAV at the final cache path**, which the
  sentence-keyed cache would then serve for ever — so that line would never be heard
  again, with nothing saying why.
- **Re-recording a voice did not invalidate the audio it replaced.**

Found on the first Debian 13 VM (`docs/VM-RUNBOOK.md`), none of it visible from Windows:

- **The packages did not build** (D-153). `PYBUILD_NAME` sent the Python install to a
  package that does not exist, and the `.install` files named a path `dh_python3` only
  creates later. Lintian then found the postinst never loading debconf, so a `dpkg -i`
  install could not ask the purge question, and five commands without manual pages.
- **The first install failed in the postinst** (D-154): `security.py` used a name
  argon2-cffi only gained after the 21.1 that Debian 13 ships. CI now also runs the
  suite against trixie's own libraries. `python3-multipart` was the wrong library, and
  the test suite ran the real `systemctl` and `tailscale` on the machine running it.
- **A child could not log in** (D-155). Accounts are passwordless by design (D-99) and
  Debian's LightDM had no way to let such an account in; it now uses a PAM service of
  BioKids' own, for child accounts only.
- **Nothing ever locked** (D-155). No screen locker was installed, and the dead-daemon
  lockdown asked `loginctl` for JSON with the flag that selects a journal format, so it
  parsed nothing and locked nobody. "+10 dk" could not unlock a locked child either.
- **The kiosk ran without its browser policy** (D-155): the directory the daemon writes
  it to did not exist on Debian.
- **Every web tile opened on "This page is blocked"** (D-156): the kiosk policy is
  machine-wide and allowed only the launcher. It now names the children's web tiles. The
  tile's browser also asked the child to invent a keyring password, and offered to
  "restore pages" after every shutdown. A child added from the children screen, or whose
  parent skipped the app step, got no tiles at all.
- **Turning on phone access failed silently on every new tailnet** (D-157): Tailscale
  waits for its owner to approve Serve, and the panel dropped the page it named. The
  panel now shows it. Tailscale's client log upload is turned off in the runbook.
- **A child left idle for ten minutes was locked out until a parent came** (D-158):
  light-locker's idle lock met an account with no password. A child's session now
  locks only when BioKids locks it.
- **The purge question was asked in English on a Turkish machine, and every removal
  printed "block: not found"** (D-159): a template field without its translation mark,
  and a comment debhelper expanded. Found running T8.5 on a clean VM, which otherwise
  passed, as did the virtual-terminal guards of T5.7.
- **No warning was ever spoken, and the warning was hidden behind the child's app**
  (D-160). Nothing carried the daemon's speech to the voice service, which could not
  read the directories it looked in anyway. The launcher now relays every line and comes
  to the front for a few seconds at 10, 5 and 1 minute, showing the sentence actually
  being said. A phone with the panel open no longer delays a shutdown by 90 seconds.
- **The voice was a robot, and the mother spoke an octave below the father** (D-161).
  espeak-ng now speaks more slowly, with its female variant for Zio and its male one for
  Sio. Where a family installs the MBROLA Turkish voices, Zio and Sio speak with those
  recordings of a real woman and man instead; espeak-ng's own route to them turned the
  "l" of every "kaldı" into silence, so BioKids runs MBROLA itself. A change of voice now
  empties the cache, which would otherwise have kept playing the old one.
- **A child could get stuck in an app** (D-163). App windows had no buttons and a
  maximised window no title bar, and Stellarium starts full screen. Every app window now
  fills the screen and has one big close button, always in the top right corner; apps
  that would start full screen start windowed; web tiles use the same title bar.
- **Every BioKids machine answered DNS for its whole network** (D-167). The daemon
  depended on `dnsmasq`, whose own service listens on port 53 on every interface. It
  now depends on `dnsmasq-base`, which has no service. An upgrade cannot remove a
  package another one pulled in: run `sudo apt remove dnsmasq` once.
- **GCompris, TuxMath and Frozen Bubble did nothing when tapped** (D-164). Debian puts
  games in `/usr/games`, which is on no service's PATH; the launcher now finds them
  there and starts them by their full path. A tile whose program is not installed is
  no longer drawn at all.
- **Google Earth and YouTube Kids were blocked even when ticked** (D-164): each needs
  more than the address its tile opens. A catalog entry can now list what it needs.
- **The "opening" screen could stay up after the app had opened, or after it was
  closed.** The launcher goes back home as soon as the app has started, and when it
  comes to the front again.
- **The child's browser and YouTube Kids were in English** (D-168): Debian's Chromium
  needs `chromium-l10n` for Turkish, and the child's profile now asks websites for the
  family's language.
- **The setup wizard refused a 20-minute quota**, the exit criterion's own number:
  its fields only took multiples of 15. They take multiples of 5, like the time
  screen.
- **For the first seconds after a boot, a child's DNS was not filtered** (D-167).
  The filter's rules are now loaded before the login screen appears.
- **A page could be stale after an upgrade.** The launcher's and the panel's static
  files are revalidated on every load, and the panel's offline cache is named after
  their contents.

### Changed

- **Safe sites, and a screen to choose them** (D-164). KÜRE Çocuk, Bilim Çocuk, Meraklı
  Minik, Code.org, YouTube Kids and PBS KIDS Games are in the catalog; the last two are
  offered but never ticked by default. A web tile opens its own path, not its whole
  site. **Çocuklar → Uygulamalar ve siteler** changes a child's tiles after setup.
  Code.org opens its course for children who cannot read yet (4-7) and its Express
  course (8-10), and Blockly Games joins them. **Dünya** (Google Earth) replaces
  **Gökyüzü** (Stellarium).
- **Phone access is one button** (D-169). "Telefondan erişimi aç" installs Tailscale
  from its own signed repository if it is missing, with log upload off from the
  first start, shows its sign-in page as a button and a QR code, and then the
  panel's address. No terminal.
- **The login screen is BioKids'** (D-170): Zio and Sio instead of Debian's logo, and
  each child as their own picture and name instead of their user name beside a grey
  silhouette. The parent panel is in the applications menu, and the setup wizard
  opens by itself at the first login after installing.
- **The internet is open by default, with harmful sites always closed** (D-167),
  overriding D-61 and D-62. The children's DNS goes to a filter on the machine that
  closes about 291,000 adult, gambling, malware and scam domains and forces
  SafeSearch on Google, YouTube, Bing and DuckDuckGo; if the filter is down they have
  no internet rather than unfiltered internet. **Çocuklar → İnternet** switches a
  child to "only the sites I choose". The parent's own internet is not filtered.
- **The child can ask questions: "Sor Bana"** (D-165), a local language model behind
  a word list, a classifier and a checked answer, which refuses what a child should
  not ask and tells the parent. Off by default, switched on per child under
  **Çocuklar → Asistan**, where the models are downloaded once. With voice switched on
  too (D-166), the child can hold a button and ask aloud; the recording is transcribed
  on the machine and never saved. This reverses D-92's "no conversation" for this
  one tile; Zio and Sio stay one-way. Database migration `7c1d9e2a4b5f` adds the
  assistant's switches and the alert text.
- **The ring shows the minutes left, at every age** (D-162), in the corner and in the
  full-screen time warning.

### Added (review follow-ups)

- **D-176, hours are picked, not typed.** Two ranges per kind of day with the
  browser's time picker, and any weekday with its own minutes and hours.
- **D-177, mail to the parent** through their own SMTP server: alerts and requests
  within a minute, the week on Sunday evening, retried when offline.
- **D-178, a local encyclopedia for the assistant.** Kiwix's Wikipedia summaries,
  searched by `kiwix-serve` inside the assistant's own network namespace, given to
  the model as the facts to prefer. The self-harm answer names 183 and 112.
- **D-174, the parent's own voice** for the seven spoken lines, recorded on the phone.
- **D-175, for a child who cannot read yet**: tiles and screens read themselves in
  the 4-7 band, and a sun sets along the time ring.
- **CI** shellchecks the maintainer scripts, installs, upgrades and purges the
  packages on a clean trixie, checks the migrations against the models, and a
  release runs the suite before it publishes.

- **D-173, "Anneme sor" answered.** A requests screen in the panel (more time with
  +15/+30/+60, sites and apps with yes/not now), a "Biraz daha süre iste" button in
  the launcher's last fifteen minutes, the answer shown and spoken on the child's
  screen, "Kilidi aç" on Today, and grants of +10 and +30.
- **D-172, fail-safe that is checked.** A systemd watchdog, command and tick
  timeouts, a once-a-minute outside check of the DNS filter, verified locks, a
  wall clock the daemon guards against date changes, and a lockdown that works
  without the database.

### Security

- **D-171, the review's security findings.** Provisioning no longer follows symlinks
  in a child's home as root. The launcher refuses requests from other sites and
  rebound names. The panel is closed to the children by nftables (and no longer by
  the machine-wide browser policy, so the parent's Chromium can open it). The browser
  policy closes `file://` and certificate click-through. The recovery code and the
  passphrase gate are validated and rate-limited. `biokids-ai` leaves the `biokids`
  group. A database backup runs before every migration. The daemon and panel units
  drop to two capabilities and none.

### Known gaps

Stated here rather than discovered later:

- **No Piper voice is bundled, and none will be** (T6.1). The licences were checked as
  `docs/VOICE.md` §3 requires, and the answer was no: `tr_TR-dfki-medium`'s dataset is
  CC-BY-NC-SA, and `fahrettin` and `fettah` were withdrawn from the Piper catalogue at
  their contributors' request. `piper-tts` is not in Debian 13 either. The warnings are
  spoken by **espeak-ng** instead — Debian main, GPL-3+, Turkish, no model file and no
  download — through the MBROLA Turkish voices where a family installs them (D-161), and
  Piper is used wherever a family installs it themselves. See D-137 and
  `assets/voices/LICENSES.md`.
- **A blocked site shows Chromium's "This site can't be reached"**, not the block page
  of D-64.
- **The assistant gets an occasional fact wrong**, as a small local model does, and
  is slow on the first question after it opens (up to three minutes on the
  development VM, then 10 to 30 seconds an answer). It has not been measured on real
  hardware. It needs `llama.cpp-tools`
  and `whisper.cpp-tools` newer than Debian 13 has; until trixie-backports has them,
  the BioKids repository has to carry rebuilds.
- **The exit criterion has not been run end to end.** The packages now build, install
  and pass lintian on a Debian 13 VM, the wizard completes there and a child logs in to
  the launcher; the spoken warnings, the phone over Tailscale and the reboot and clock
  checks are still ahead. `docs/VM-RUNBOOK.md` is that work as a sequence, and
  `debian/README.source` §7 what it leaves unverified.

No telemetry, analytics, crash reporting or version ping has been added, and none will
be. The complete inventory of outbound connections is `docs/SECURITY.md` §4.

Two placeholders are outstanding before the repository can be made public: the security
contact in `SECURITY.md` and the enforcement contact in `CODE_OF_CONDUCT.md`.

[Unreleased]: https://github.com/bilalgurkansanli/BioKids/commits/main
