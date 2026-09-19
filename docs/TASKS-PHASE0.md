# TASKS-PHASE0.md

Executable task list for Phase 0. Work through these in order. Each task is a coherent
block: finish it, run its check, report, then continue.

**Phase 0 exit criterion:** on a fresh Debian 13 VM, `apt install biokids` → wizard →
the parent reaches the panel from a phone over Tailscale → sets a 20-minute quota →
the child session counts down, speaks warnings at 10/5/1 minutes in Turkish, and the
machine shuts down. Reboot resumes correctly. Moving the system clock forward three
hours changes nothing.

Filtering, AI, voice cloning, rewards, categories and the ISO are **not** in Phase 0.

---

## T0 — Environment

**T0.1** Create the Debian 13 VM in VirtualBox: 4 GB RAM, 2 vCPU, 30 GB disk, XFCE
task, Guest Additions, shared folder to the repo on the Windows host. Snapshot it as
`clean-debian-13`.

**T0.2** Scaffold the repository: the layout in `CLAUDE.md` §4, `pyproject.toml` with
ruff/mypy/pytest config, `.gitignore`, MIT `LICENSE`, a stub `README.md`, and the
GitHub issue/PR templates.

**T0.3** Write `ci.yml`: ruff, mypy, pytest. Make it pass on an empty project before
writing any real code. A green pipeline from commit one is worth the twenty minutes.

*Check:* `git push` shows a green tick.

---

## T1 — `biokids-core`

**T1.1** Config loader for `/etc/biokids/config.toml` with schema validation and
defaults. Every path and port from `CLAUDE.md` §5 is a config value, not a literal.

**T1.2** SQLAlchemy models for the schema in `ARCHITECTURE.md` §5, plus Alembic with an
initial migration. WAL mode, `foreign_keys=ON`, 0600 on the file.

**T1.3** i18n scaffold: gettext catalogs for `tr` and `en`, a `_()` helper, an extraction
script, and a test that fails if any key exists in one locale but not the other.

**T1.4** Write `PolicyEngine.evaluate()` exactly as specified in `ARCHITECTURE.md` §4.
Pure, no I/O, no clock access — `now_wall` is a parameter.

**T1.5** Tests for T1.4 covering every precedence rule and these edge cases: quota
exactly zero, a window boundary at the exact second, a penalty overlapping a window,
midnight rollover, and a child with no policy at all.

*Check:* `pytest tests/test_policy.py` passes; `mypy --strict src/biokids_core` is clean.

---

## T2 — The time ledger

This is the hardest correctness problem in the project. Do it carefully and do not move
on until the tests are genuinely convincing.

**T2.1** `BootClock`: wraps `CLOCK_BOOTTIME` and `/proc/sys/kernel/random/boot_id`.
Detects a new boot epoch and carries forward totals from the DB.

**T2.2** `TimeLedger`: four counters per child per day (`general`, `category[*]`,
`earned`, `penalty`). Tick size 10 s. Persist every 30 s and on every state change.
For Phase 0 only `general` and `penalty` need to do real work, but build all four.

**T2.3** Wall-clock jump detection: a delta greater than 600 s between wall and boot
clock deltas is logged to `audit_log` and surfaced to the parent. It never alters the
ledger.

**T2.4** Suspend handling: subtract logind-reported suspended time from the
`BOOTTIME` delta so a sleeping laptop does not consume quota.

**T2.5** Tests with a fully injected clock:
- one hour of continuous use consumes exactly one hour
- clock moved forward 3 h: consumption unchanged
- clock moved backward 1 h: consumption unchanged, jump logged
- reboot mid-session: total carries forward, at most 30 s lost
- suspend for 2 h: consumption unchanged
- session locked for 20 min: consumption unchanged
- two sessions for one user: counted once, not twice

*Check:* the whole list above passes. This suite is the backbone of the product.

---

## T3 — `biokids-daemon`

**T3.1** `SessionWatcher` on `org.freedesktop.login1` via `dbus-next` (async). Track
per child user: session id, seat, `Active`, `IdleHint`, lock state. Handle sessions that
exist before the daemon starts.

**T3.2** The IPC server on `/run/biokids/daemon.sock`: line-delimited JSON, group
`biokids`, 0660. Operations for Phase 0: `get_status`, `set_policy`, `grant_time`,
`lock_now`, `poweroff_now`, `subscribe_events`.

**T3.3** `Enforcer`: `warn`, `lock` (`loginctl lock-session`), `logout`, `poweroff`
(`systemctl poweroff`), `start_timeout`. Applications receive `SIGTERM` then `SIGKILL`
after 10 s so work can be saved.

**T3.4** The main loop: tick → update ledger → `PolicyEngine.evaluate()` → act →
publish events. The loop itself must contain no policy logic; it only wires things
together.

**T3.5** The end-of-time sequence from `ARCHITECTURE.md` §3.5: warnings at 10/5/1,
lock at zero, 120 s countdown, then the configured end action.

**T3.6** systemd unit with the full hardening block from `SECURITY.md` §2, plus
`Restart=always` and `OnFailure=biokids-lockdown.service`.

**T3.7** `biokids-lockdown.service`: locks every child session and writes a loud audit
entry. Test it by killing the daemon with `-9`.

*Check:* on the VM, with a 3-minute quota, the child session locks and powers off on
schedule, and `systemctl kill -s KILL biokids-daemon` locks the session within 5 s.

---

## T4 — `biokids-panel`

**T4.1** FastAPI app on `127.0.0.1:7878`, Jinja2 templates, `tokens.css` from
`UX.md` §1, vendored HTMX and fonts. No npm, no CDN.

**T4.2** Auth: argon2id PIN, the 5-attempt/15-minute lockout, a signed session cookie,
30-minute idle timeout, the separate passphrase gate for destructive actions, and the
8-word recovery code. Every attempt goes to `audit_log`.

**T4.3** The setup wizard, steps 1–6 and 8 from `UX.md` §3.4. Steps 7, 9 and 10
(filter, SMTP, AI) are stubs in Phase 0 that say "coming soon" rather than being absent
— the flow shape should be right from the start.

**T4.4** The Today screen exactly as drawn in `UX.md` §3.2, live via server-sent
events, with the three action buttons wired to the daemon.

**T4.5** The per-child time settings screen: weekday and weekend quota, time windows,
end action. Under seven visible controls; anything else behind Advanced.

**T4.6** The audit log view with filtering by child, action and date.

**T4.7** PWA: manifest, maskable icons, service worker caching the shell only.

*Check:* the whole flow works in a desktop browser at `127.0.0.1:7878`.

---

## T5 — `biokids-shell`

**T5.1** A FastAPI app on `127.0.0.1:7879` serving the launcher, as a systemd **user**
unit in the child session.

**T5.2** The kiosk supervisor: launch Chromium with `--kiosk --app=http://127.0.0.1:7879`,
a dedicated `--user-data-dir`, and a managed policy allowing only that origin. Restart
it if it exits. Suppress `Ctrl+W`, `Ctrl+T`, `Ctrl+N`, `F11`, `Alt+F4` and the context
menu.

**T5.3** The tile grid, both age bands, reading the catalog and the child's enabled apps.

**T5.4** App launching via `systemd-run --user --scope --unit=biokids-app-<slug>-<n>`,
with permission checked against the daemon first.

**T5.5** The time ring and the warning banners, driven by daemon events over SSE.

**T5.6** The lock, break and time-up screens.

**T5.7** XFCE session configuration: no panel, no desktop icons, no window decorations
for the kiosk window, `DontVTSwitch`, no terminal installed, settings hidden.

*Check:* logging in as the child lands directly in the launcher, and there is no way out
of it with a keyboard or a mouse.

---

## T6 — `biokids-voice` (Phase 0 subset)

**T6.1** Package Piper and one Turkish female plus one Turkish male voice. **Record
each voice's license in `assets/voices/LICENSES.md` before bundling** — if a voice is
not redistributable, download it at first run instead.

**T6.2** The voice service on a user unix socket, with the `speak` operation from
`VOICE.md` §8.

**T6.3** Line files for `welcome`, `warn_10`, `warn_5`, `warn_1`, `time_up`, with at
least two Turkish and two English variants each.

**T6.4** Caching keyed on `sha256(text + persona + profile)`, LRU capped at 200 MB.

**T6.5** The queue with priorities, PipeWire ducking to 30%, and the fallback chain
cloned → stock → banner.

**T6.6** The Zio and Sio SVG characters with a `neutral` and a `happy` expression, and
the slide-in card in the shell. The spoken line always appears as text on the card.

*Check:* setting a 12-minute quota produces spoken warnings at 10, 5 and 1 minute, each
with a character card, without interrupting itself.

---

## T7 — Remote access

**T7.1** Detect Tailscale, show its state in the panel, and run
`tailscale serve --bg 7878` when the parent enables remote access.

**T7.2** A wizard step with a QR code for the resulting URL and a clear explanation that
only devices on the family's tailnet can reach it.

**T7.3** Document the fallback when Tailscale is absent: localhost only, and explicitly
tell the parent not to port-forward. Do not offer a LAN-exposure option; someone will
use it and then expose it to the internet.

*Check:* the panel opens on a phone, over the tailnet, with a valid certificate, and
the three action buttons work.

---

## T8 — Packaging

**T8.1** `debian/` producing `biokids-core`, `biokids-daemon`, `biokids-panel`,
`biokids-shell`, `biokids-voice` and the `biokids` metapackage.

**T8.2** Maintainer scripts per `PACKAGING.md` §1, including the debconf prompt before
purging data.

**T8.3** `lintian` clean at error level.

**T8.4** The `aptly` repo and `release.yml`, publishing to GitHub Pages with the key
from Secrets.

**T8.5** A full install test on the pristine `clean-debian-13` snapshot: install,
configure, use, upgrade to a newer version, remove, purge. Verify that `remove` does
not delete `/var/lib/biokids` and that `purge` asks first.

*Check:* the two-command install from `PACKAGING.md` §2 works on a snapshot that has
never seen the source tree.

---

## T9 — Phase 0 acceptance

Run the full exit criterion end to end on a clean VM and record it as a short screen
capture. That recording is the first public artifact of the project and the first entry
for the portfolio.

Then, before starting Phase 1:

- write the real `README.md` with screenshots
- write `CONTRIBUTING.md`, including the won't-build list from `SECURITY.md` §7
- tag `v0.1.0`
- update `docs/ROADMAP.md` to mark Phase 0 complete
- open the pinned Roadmap issue

---

## Working notes

- **Snapshot the VM** before T3.6, T5.7, T7.1 and T8.5. Each can leave a session
  unbootable or unreachable.
- Keep a `dev/reset.sh` that drops the DB, clears `/var/lib/biokids`, and restarts the
  units. You will run it a hundred times.
- Keep a `dev/fakeclock.py` that drives the ledger with an injected clock so a
  "three-hour session" takes three seconds to test.
- When something in these docs turns out to be wrong — and something will — fix the doc
  in the same commit as the code. A spec that drifts from the code is worse than no spec.
