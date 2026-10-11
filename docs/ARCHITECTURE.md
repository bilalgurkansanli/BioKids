# ARCHITECTURE.md

Read `CLAUDE.md` first. This file describes how the pieces fit together and how the
time and policy engines actually work.

---

## 1. Component map

```
                    ┌──────────────────────────────────────┐
   parent's phone ──┤  tailscale serve  →  127.0.0.1:7878  │
                    └──────────────┬───────────────────────┘
                                   │
                    ┌──────────────▼───────────────┐
                    │  biokids-panel  (FastAPI)    │   root
                    │  Jinja2 + forms, PWA         │
                    └──────────────┬───────────────┘
                                   │ unix socket /run/biokids/daemon.sock
                    ┌──────────────▼───────────────┐
                    │  biokids-daemon              │   root
                    │  ┌────────────────────────┐  │
                    │  │ TimeLedger (BOOTTIME)  │  │
                    │  │ PolicyEngine           │  │
                    │  │ SessionWatcher (logind)│  │
                    │  │ Enforcer (lock/halt)   │  │
                    │  │ FilterManager          │  │
                    │  │ EventBus               │  │
                    │  └────────────────────────┘  │
                    └───┬───────┬──────────┬───────┘
                        │       │          │
        ┌───────────────▼─┐ ┌───▼────────┐ │  ┌──────────────────┐
        │ biokids-shell   │ │ biokids-   │ └──┤ biokids-ai       │
        │ child launcher  │ │ voice      │    │ (optional)       │
        │ :7879+uid%100   │ │ Zio & Sio  │    │ the assistant    │
        └───────▲─────────┘ └────────────┘    └──────────────────┘
                │ POST /visit (host name), GET /blocked
        ┌───────┴──────────────────────────┐
        │ Chromium (child profile)         │
        │  managed policy + BioKids ext    │
        └──────────────────────────────────┘

   system level:  dnsmasq (127.0.0.1:5354, children only)   nftables   lightdm   systemd-logind
                  kiwix-serve (127.0.0.1:7877, optional, biokids-wiki)
   storage:       /var/lib/biokids/biokids.db  (SQLite WAL, 0600 root)
```

---

## 2. Process responsibilities

### `biokids-daemon` (root, system service)

The only component allowed to change system state. Everything else asks it.

- **SessionWatcher** subscribes to `org.freedesktop.login1` signals and tracks, per
  child user: session id, seat, `Active`, `IdleHint`, and lock state.
- **TimeLedger** accumulates usage. See §3.
- **PolicyEngine** owns the rules and answers the only question that matters:
  *given this child, this moment, and this activity, what should happen?* Pure
  function over `(policy, ledger_state, now)`. Fully unit-tested, no I/O.
- **Enforcer** performs actions: warn, lock, log out, shut down, start a timeout,
  kill a process outside the catalog.
- **FilterManager** renders `dnsmasq` config and Chromium policy from the DB, reloads
  services, and applies per-child allow/blocklists.
- **EventBus** fans out events to the panel (SSE), the notifier, and
  each child's launcher over `/run/biokids/shell.sock`. The voice service hears them
  through the launcher: it runs as the child, who may not open the daemon's own
  socket (D-123), so `biokids_shell.relay` carries every SPEAK across (D-160).

IPC: a line-delimited JSON protocol over a Unix socket. Not DBus — a Unix socket is
easier to test, easier to secure, and has no session-bus lifetime problems.

There are **two** sockets, because the panel and the child need very different
things (D-123):

| Socket | Access | What it can do |
|---|---|---|
| `/run/biokids/daemon.sock` | `root:biokids`, 0660 | everything: policy, grants, lock, poweroff |
| `/run/biokids/shell.sock` | world-connectable, authorised by peer uid | reads scoped to the calling child, plus launch, activity, ask-a-parent, shutdown request |

The child is not in the `biokids` group and must never be — that group can grant
time and power the machine off. The launcher therefore has a socket of its own on
which **no operation takes a child id as an argument**: the daemon reads the peer
credentials the kernel attached at connect time (`SO_PEERCRED`, which a process
cannot forge about itself), maps that uid to a child, and answers about that
child only. A uid that is not a managed child gets nothing.

### `biokids-panel` (root, system service)

FastAPI on `127.0.0.1:7878`. Reads the DB directly for queries; every mutation goes
through the daemon socket so the daemon stays the single writer of live policy.
Server-sent events for live counters. Routes live in `biokids_panel/routes/`, one
module per area (D-186).

**Adding, changing and removing a child.** The panel's sandbox cannot write `/etc` or
`/home`, so the account work runs in two template units, each one fixed
`biokids-admin` command started with `systemctl start --wait`:
`biokids-provision@<user>.service` when a child is added, or when the edit screen
(`/children/{id}/edit`, D-191) changes the name or picture the login screen shows, and
`biokids-unprovision@<user>.service` when a child is removed. Removal takes the account
out of `biokids-child`, sets `SystemAccount=true` in its AccountsService key file and
gives it `/usr/sbin/nologin`, so it leaves the login screen the next time the greeter
starts, and removes the launcher's autostart entry and any other session file still
byte-identical to what BioKids wrote; the account and its home stay. It needs the passphrase, is refused while the child is at the
machine, and keeps the database row if the account could not be changed. Otherwise the
row is deleted and everything recorded for the child with it (`ON DELETE CASCADE`);
the audit log keeps the removal.

**History** (`/history`, D-192) reads what retention already keeps (§5): fourteen days
of `ledger_day` minutes as bars, and for one day the `usage_tick` seconds per app, the
`visit` events as one line per host, and the day's `access_request` rows. Plain HTML,
no script.

**Insights** (`/insights`, D-206) is the same kind of reader over a week or a month.
`biokids_core.insights` loads, per child and period, the `ledger_day` and `usage_tick`
rows (with the period before, for comparing), the `usage_hour` rows (with the morning
after, so a late night is counted with its evening), the `visit` events, the
requests, the assistant's alerts and the daemon's `quota_exhausted` audit rows, one
indexed query each; a pure `summarize` turns them into numbers and a pure, rule-based
`highlights` into at most five sentences as catalogue keys with parameters. Today's
"Bu hafta" card and the Sunday mail (`notifier.weekly_summary`) call the same
functions. Nothing is stored; no model is involved.

### `biokids-shell` (child user, user service)

A small FastAPI app serving the launcher UI, plus a supervisor that starts the kiosk
Chromium. Launching an app is a POST to the shell, which asks the daemon for permission
and then spawns the process in a tracked cgroup so usage can be attributed to a
category.

**One port per child** (D-188). Each child's launcher listens on
`127.0.0.1:(shell.port + uid % 100)`, 7879–7978 by default (`ShellConfig.launcher_port`),
so two children logged in at once by fast user switching do not fight over one port.
The daemon writes every child's launcher origin into the kiosk policy, and its
nftables table carries a `launchers` chain that rejects a connection to a child's port
from any other uid (`docs/SECURITY.md` §5): a launcher answers as its child on
`shell.sock`, so only that child's own processes may reach it.

### `biokids-voice` (child user, user service)

Receives `speak(event, persona, priority)` over a unix socket. Plays a parent's own
recording of the line when there is one (D-174), otherwise renders it with Piper or
espeak-ng, caches by hash, plays through PipeWire, and tells the shell to show the
character card. See `docs/VOICE.md`.

### `biokids-ai` (dedicated user, optional)

Answers the child's questions with a local model, checked against a local
encyclopedia (D-165, D-178); the daemon decides who may ask and records alerts. It
never captures the screen: the screen watcher is not in v1 (D-180). See
`docs/AI-LAYER.md`.

### Chromium extension (`extension/`)

Force-installed via managed policy from a CRX packed and signed on the machine
(D-187). When a page is blocked it sends the tab to the launcher's block page, where a
child can ask a parent; and it reports the host name - never the path, title or
content - of each page a child opens to the child's own launcher, for the history
screen. It finds its launcher by probing the launcher ports, of which nftables lets
it reach only its own (D-188).

The visit path, end to end:

```
Chromium (webNavigation, main frame committed)
  → extension: same host within 10 min? drop  (chrome.storage.session)
  → POST http://127.0.0.1:<launcher>/visit  {"host": "www.example.com"}
  → launcher: a host name and nothing else, or 400
  → shell.sock REPORT_VISIT, authorised by peer uid (D-123)
  → daemon record_visit: same host within VISIT_REPEAT_SECONDS (600)? drop
  → usage_event(kind="visit", payload {"host": …})
```

Packing happens on the machine: biokids-shell ships the source in
`/usr/share/biokids/extension`, and its postinst runs `biokids-admin pack-extension`
(`biokids_core.extension_pack`), which signs a CRX3 with
`/var/lib/biokids/extension-key.pem` and writes `biokids.crx`, `updates.xml` and
`extension-id` into `/var/lib/biokids-ext`. The daemon reads `extension-id` when it
writes the policy and adds the `ExtensionInstallForcelist` entry; with no packed
extension the policy simply has none. `extension/README.md` has the details.

---

## 3. The time engine

### 3.1 Why `CLOCK_BOOTTIME`

Wall clock can be changed by anyone with the settings app, and NTP jumps can move it
by hours. `CLOCK_BOOTTIME` is monotonic and includes time spent suspended, so:

```python
import time
def now_boot() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)
```

Usage is always a difference of two `BOOTTIME` readings. Wall clock is used for exactly
two things: deciding which calendar day's quota applies, and evaluating time windows
("only 16:00–20:00"). A wall-clock jump greater than 600 s is written to the audit log
and raised to the parent; it never changes the ledger.

Suspended time is measured rather than asked for: `CLOCK_BOOTTIME` runs through
suspend and `CLOCK_MONOTONIC` does not, so the difference between the two deltas
*is* the time the machine was asleep. That is exact, needs no logind round trip,
and cannot disagree with the clock it is correcting.

Because `BOOTTIME` resets on reboot, the ledger persists `(boot_id, boot_offset)` pairs.
`boot_id` comes from `/proc/sys/kernel/random/boot_id`. A new `boot_id` means a new
epoch; accumulated totals carry forward from the DB.

### 3.2 What counts as usage

A tick (default 10 s) adds to the ledger only when **all** of these hold:

- the child has a logind session, and `Active == true`
- the session is not locked
- the seat is the active seat (not switched away)
- suspend is not in progress
- we are not inside a forced break or a timeout

Idle behaves differently per category: idle time still consumes general quota (the child
is occupying the machine) but does **not** accrue earned time (§3.4).

### 3.3 Ledgers

Four independent counters per child per day:

| Ledger | Meaning |
|---|---|
| `general` | total screen time against the daily quota |
| `category[*]` | time in Games / Learning / Web / Free |
| `earned` | bonus minutes granted by the reward system |
| `penalty` | active timeout, counted down separately |

Effective remaining time:

```
remaining = (daily_quota + earned_today) - general_used
            ∩ (current time inside an allowed window)
            ∩ (category limit not exhausted for the active app)
            − (penalty active ⇒ 0)
```

Persisted every 30 s and on every state change, so a hard power cut loses at most 30 s.

Every second added to `general` is also filed under its local wall-clock hour
(`usage_hour`, D-206), split at the hour, and never across midnight, so a day's
hours add up to its `general`. Those rows are written after the ledger's commit, in a
transaction of their own: they feed the insights screen's chart, nothing that
enforces reads them, and a failure to write them is logged once and costs only the
chart.

### 3.4 Earned time

```
earn = floor(learning_minutes_active / 1) * ratio
```

with these guards:

- a learning session shorter than 10 minutes earns nothing (prevents app-flapping)
- 3 minutes without input pauses accrual
- daily earned cap, default 60 minutes
- earned time is spendable only in the Games category by default; the parent may make
  it general
- earned minutes never roll over to the next day

### 3.5 Warning and shutdown sequence

| Remaining | Action |
|---|---|
| 10 min | the launcher comes in front of the app for a few seconds: Zio/Sio, the sentence being said, the ring (D-160); speaks if enabled |
| 5 min | the same, amber |
| 1 min | the same, red, the ring pulsing |
| 0 | lock screen with friendly message, 120 s countdown |
| −120 s | graceful shutdown (`systemctl poweroff`) |

Parent-configurable end action: `lock` \| `logout` \| `poweroff` (default `poweroff`,
matching the owner's spec). Open applications receive a normal termination signal
sequence so files can be saved.

**Kapat**, the child's tile (D-100) or the parent's button on Today, takes the same
steps without the countdown, and is answered before they are taken (D-209). The daemon
decides, audits and answers at once; then a task the supervisor holds stops the apps
with their grace period, writes the ledger, waits until at least
`POWER_GRACE_SECONDS` (6 s) after the answer so the goodbye can be read and, for 4–7,
heard, and calls `systemctl poweroff`. Done inside the IPC call, all of that outlasted
the five seconds the launcher and the panel wait for an answer. While anyone else is
logged in, the child's tile ends only the child's own session (`loginctl
terminate-user`, as the `logout` end action does), and who is logged in is asked again
just before the machine goes. When the poweroff fails, the child's session is ended -
or, failing that, locked - and the parent's poweroff locks every child's screen; either
way an `alert` audit row reaches the parent (principle 3). A daemon stopped meanwhile
cancels what is left rather than switching off later, during an upgrade.

---

## 4. Policy engine

Pure, side-effect-free, exhaustively tested.

```python
@dataclass(frozen=True)
class Decision:
    allow: bool
    action: Literal["none","warn","lock","logout","poweroff","timeout","block"]
    reason_code: str          # i18n key, never a human sentence
    speak: SpeechRequest | None
    remaining_seconds: int
```

```python
def evaluate(policy: ChildPolicy,
             state: LedgerState,
             ctx: ActivityContext,
             now_wall: datetime) -> Decision: ...
```

Precedence, highest first:

1. active penalty / timeout
2. outside allowed time window
3. quota exhausted
4. forced break due
5. category limit exhausted
6. app not in the child's catalog
7. URL blocked by filter
8. allow

`reason_code` is always an i18n key like `quota.exhausted` or `filter.blocked.category`.
Never build a user-facing sentence in the engine. This is what keeps Turkish and
English honest.

---

## 5. Data model (SQLite)

Core tables. Migrations with Alembic from the first commit.

```
family(id, created_at, locale, timezone, setup_completed_at)
parent(id, display_name, pin_hash, passphrase_hash, recovery_hash, email,
       smtp_json_encrypted, pin_failures, pin_locked_until, pin_lockout_level)
panel_session(id, token_hash, parent_id, created_at, last_seen_at, passphrase_ok_until)
child(id, unix_user, display_name, birth_year, age_band, avatar, locale,
      shell_mode, supervision_indicator, enabled, first_run_done)
policy(child_id, weekday_quota_min, weekend_quota_min, windows_json, schedule_json,
       end_action, break_*, earn_*, filter_mode, assistant_enabled, assistant_voice,
       voice_enabled, voice_events_json)
category_limit(child_id, category, minutes)              -- set on the time screen (D-193)
app(slug, name_tr, name_en, exec_cmd, icon, default_category, min_age, max_age, kind, url)
child_app(child_id, app_slug, enabled, category_override, position)
filter_rule(id, child_id, kind /* allow|block */, pattern, source)
access_request(id, child_id, kind, url, minutes, created_at, status, decided_at)
boot_epoch(id, boot_id, first_seen_wall, last_seen_wall, last_boot_offset)
ledger_day(child_id, day, general_s, earned_s, granted_s, penalty_remaining_s, ...)
usage_tick(child_id, day, category, app_slug, seconds)
usage_hour(child_id, day, hour, seconds)                 -- hour 0-23, local (D-206)
usage_event(id, child_id, ts_wall, ts_boot, kind, payload_json)  -- "visit": {"host": …}
ai_alert(id, child_id, ts, score, category, reason_key, text, notified, dismissed)
audit_log(id, ts, actor, action, target, detail_json, severity)
voice_line(id, event, locale, text, child_id)
setting(key, value)
```

Every table that names a child deletes its rows with the child (`ON DELETE CASCADE`).

### Retention

Nothing is kept for ever (D-189). The daemon's `housekeeping` runs
`biokids_core.retention.prune` five minutes after it starts and then once a day, in one
transaction; a failure is logged and tried again the next day. The windows are
`[retention]` in `/etc/biokids/config.toml`:

| Key | Default | Deletes |
|---|---|---|
| `activity_days` | 30 | `usage_event` (visited sites, ledger gaps), `ai_alert` and `usage_hour` (D-206) |
| `usage_days` | 400 | `usage_tick` and `ledger_day` |
| `audit_days` | 365 | `audit_log` |
| `requests_days` | 90 | `access_request` |

It also deletes `panel_session` rows a day past the idle timeout, and keeps only the 20
newest `boot_epoch` rows.

---

## 6. Filtering pipeline

```
child types a URL
      │
      ├─ Chromium managed policy: URLAllowlist / URLBlocklist  ← instant, no network
      │      ("only the sites I choose": URLBlocklist ["*"] + explicit allows;
      │       open, the default since D-167: no wildcard)
      │
      │      blocked → ERR_BLOCKED_BY_ADMINISTRATOR → the extension opens the
      │                launcher's /blocked page (D-187)
      │
      ├─ DNS resolution → nftables sends a child's :53 to dnsmasq on :5354
      │      ├─ blocklist hit → NXDOMAIN → ERR_NAME_NOT_RESOLVED → the launcher's
      │      │                 /blocked page, by the extension (D-187)
      │      ├─ safesearch CNAME rewrites applied
      │      ├─ a site a parent enabled → upstream, never blocked
      │      └─ otherwise → the system's own resolvers
      │
      ├─ nftables: a child's :853 and HTTPS to known DoH resolvers rejected
      │
      └─ the page opens → the extension reports its host name to the child's
                          launcher, for the history (§2, D-187)
```

Generated files, rendered by the daemon (`biokids_daemon/dnsfilter.py`, D-167) and
never hand-edited:

- `/var/lib/biokids/dns/dnsmasq.conf` and `blocklist.conf`, read by `biokids-dns.service`
- `/var/lib/biokids/dns/biokids.nft`, loaded with `nft -f`
- `/etc/chromium/policies/managed/biokids-kiosk.json`, one for the machine (the
  strictest child's mode), naming every child's launcher origin and, once packed, the
  extension

The packed extension in `/var/lib/biokids-ext/` is not the daemon's: biokids-shell's
postinst writes it (§2).

Regeneration is atomic: write to a temp file, `fsync`, rename, reload the service, and
verify. A failed verification triggers the lockdown path rather than leaving a partial
config in place.

---

## 7. Application launching and attribution

The child clicks a tile → shell POSTs to the daemon → the daemon checks the policy →
if allowed, the shell spawns the app via `systemd-run --user --scope
--unit=biokids-app-<slug>-<n>`. A per-app scope gives us a cgroup, so:

- the active category is known from the focused window's cgroup, not a guess
- category time is attributed correctly even with several apps open
- killing an app at limit time is `systemctl --user stop`, clean and complete
- per-app network rules, if they are ever built (D-66), attach to the same cgroups

Focus tracking on X11: `_NET_ACTIVE_WINDOW` via python-xlib, mapped to a PID via
`_NET_WM_PID`, mapped to a cgroup via `/proc/<pid>/cgroup`.

The app bar (D-197, `biokids_shell/appbar/`) already walks that chain in the child's
session, through libwnck: the active window, its `_NET_WM_PID`, its
`biokids-app-*.scope`. It uses the scope to find the app's own sound streams and, when
an app will not close, to stop it; the launcher uses it to show an open app's window
again when its tile is tapped. The running bar is asked over its own socket,
`$XDG_RUNTIME_DIR/biokids-appbar.sock` (mode 0600, one line each way, answered on the
bar's GTK main loop, D-208); only where no bar is running does the launcher run the
bar's code once as a helper, `--bring-back <unit>`. The request carries the
launcher's `time.monotonic_ns()` (CLOCK_MONOTONIC, one clock for the whole machine),
and the bar does not act on one more than 10 s old by that stamp - also when it was
frozen and read the line late. Taps on one tile are taken one at a time, and an app
whose scope is running is never started a second time because the bar could not
look; only a scope systemd says has ended, or one with no window 30 s after it
started, is replaced. A launch is answered once `/proc/<pid>/exe` shows that
`systemd-run` has become the app (the 1.5 s refusal wait is only the upper bound), and
the activity report to the daemon goes after the answer. The app is still watched
until those 1.5 s are over: one that exits with an error by then is forgotten at once,
so `/running` stops listing it, and the page - told by the answer's `tracked` that it
can follow the app - asks `/running` two seconds after the answer and gives a tile
whose app is gone the calm Blocked screen instead of 25 seconds of "Açılıyor…".

---

## 8. i18n

- `gettext`-style catalogs under `src/biokids_core/locale/{tr,en}/LC_MESSAGES/`.
- Every string has a key. The engine emits keys; the presentation layer resolves them.
- Locale is per-child for the shell and per-session for the panel.
- Voice lines are separate from UI strings: `voice/tr/quota_10min.txt` etc., because
  they are phrased differently (spoken, warm, first-person) and a parent may edit them.

---

## 9. Failure behaviour

| Failure | Response |
|---|---|
| daemon crashes | `Restart=always`; `OnFailure=biokids-lockdown.service` locks the child session |
| daemon hangs | `Type=notify` with `WatchdogSec=90s`: only a tick that finished pings the watchdog, so systemd kills a hung daemon, the lockdown runs, and it restarts (D-172) |
| a lock does not take | the child's screen locker is asked every tick; a session still on screen after four ticks is ended (D-172) |
| the DNS filter is open | checked from outside once a minute (nftables table present, NXDOMAIN for a canary); one repair, then every child at the machine is locked until it closes (D-172) |
| the wall clock jumps | ignored for the day and the time windows until NTP confirms it (`GuardedClock`, D-172) |
| daemon unreachable from the launcher | the launcher shows a holding screen and starts nothing. A working launcher with no limits behind it is the failure principle #3 exists to prevent |
| daemon unreachable from the panel | the children are still listed, from the database, with their live state shown as unknown and the action buttons withheld. "Not responding" is never rendered as "no children" |
| panel crashes | restart; enforcement is unaffected |
| dnsmasq fails to reload | revert to the previous config; if that fails, lock and alert |
| AI worker crashes | log, alert the parent, continue without AI. Never blocks the child |
| voice service crashes | fall back to a plain on-screen banner |
| DB corrupt | fail closed: lock the child session, show a recovery message to the parent |
| disk full | the tick cannot persist; three failed ticks lock every child session and the daemon exits for systemd (D-139) |
| no network | everything works; mail to the parent is retried with a growing pause until it goes (D-177) |

The rule: **enforcement failures lock; convenience failures degrade.**
