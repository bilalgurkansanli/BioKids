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
                    │  Jinja2 + HTMX, PWA          │
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
        │ :7879, kiosk    │ │ Zio & Sio  │    │ OCR → VLM        │
        └───────┬─────────┘ └────────────┘    └──────────────────┘
                │
        ┌───────▼──────────────────────────┐
        │ Chromium (child profile)         │
        │  managed policy + BioKids ext ───┼──→ reports navigations to daemon
        └──────────────────────────────────┘

   system level:  dnsmasq (127.0.0.1:53)   nftables   lightdm   systemd-logind
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
- **EventBus** fans out events to the panel (SSE), the voice service, the AI worker,
  and the notifier.

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
Server-sent events for live counters.

### `biokids-shell` (child user, user service)

A small FastAPI app on `127.0.0.1:7879` serving the launcher UI, plus a supervisor that
starts the kiosk Chromium. Launching an app is a POST to the shell, which asks the
daemon for permission and then spawns the process in a tracked cgroup so usage can be
attributed to a category.

### `biokids-voice` (child user, user service)

Receives `speak(text, persona, priority)` over a unix socket. Renders with Piper,
optionally converts timbre with OpenVoice V2, caches by hash, plays through PipeWire,
and tells the shell to show the character card. See `docs/VOICE.md`.

### `biokids-ai` (dedicated user, optional)

Subscribes to trigger events, captures the screen, runs the pipeline, emits a risk
score. Never enforces anything itself. See `docs/AI-LAYER.md`.

### Chromium extension (`extension/`)

Force-installed via managed policy. Reports `{child, url, title, timestamp}` for every
committed navigation to the daemon over `127.0.0.1` with a per-install token. This is
far more reliable than parsing window titles, and it is how the daemon knows a new
domain was visited (an AI trigger) and how per-domain reporting works.

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
| 10 min | shell banner + Zio/Sio speaks (if enabled) |
| 5 min | banner turns amber, ring turns amber, speaks |
| 1 min | full-width banner, ring red, speaks |
| 0 | lock screen with friendly message, 120 s countdown |
| −120 s | graceful shutdown (`systemctl poweroff`) |

Parent-configurable end action: `lock` \| `logout` \| `poweroff` (default `poweroff`,
matching the owner's spec). Open applications receive a normal termination signal
sequence so files can be saved.

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
8. AI risk above the block threshold
9. allow

`reason_code` is always an i18n key like `quota.exhausted` or `filter.blocked.category`.
Never build a user-facing sentence in the engine. This is what keeps Turkish and
English honest.

---

## 5. Data model (SQLite)

Core tables. Migrations with Alembic from the first commit.

```
family(id, created_at, locale, timezone)
parent(id, pin_hash, passphrase_hash, recovery_hash, email, smtp_json_encrypted)
child(id, unix_user, display_name, birth_year, age_band, avatar, locale,
      shell_mode, supervision_indicator)
policy(child_id, weekday_quota_min, weekend_quota_min, windows_json,
       end_action, break_enabled, break_work_min, break_rest_min,
       earn_enabled, earn_ratio, earn_cap_min, filter_mode, ai_enabled,
       ai_block_enabled, ai_timeout_min)
category_limit(child_id, category, minutes)
app(slug, name_tr, name_en, exec, icon, default_category, min_age, max_age)
child_app(child_id, app_slug, enabled, category_override)
filter_rule(child_id, kind /* allow|block */, pattern, source /* manual|list|request */)
access_request(id, child_id, url, created_at, status, decided_at)
usage_tick(child_id, day, category, seconds)            -- rolled up nightly
usage_event(id, child_id, ts_wall, ts_boot, kind, payload_json)
ai_alert(id, child_id, ts, score, category, reason, evidence_path, notified)
audit_log(id, ts, actor, action, target, detail_json)
voice_profile(id, persona /* zio|sio */, kind /* stock|cloned */, model_path)
setting(key, value)
```

Retention: `usage_tick` rolled up to daily aggregates after 14 days and kept for 1 year.
`usage_event` kept 30 days. `ai_alert` evidence pruned per the retention setting.

---

## 6. Filtering pipeline

```
child types a URL
      │
      ├─ Chromium managed policy: URLAllowlist / URLBlocklist  ← instant, no network
      │      (allowlist mode for 4–7: URLBlocklist ["*"] + explicit allows)
      │
      ├─ BioKids extension reports the navigation to the daemon
      │
      ├─ DNS resolution → local dnsmasq
      │      ├─ blocklist hit → 127.0.0.1 → local block page
      │      ├─ safesearch CNAME rewrites applied
      │      └─ otherwise → upstream (1.1.1.3 default)
      │
      └─ nftables: all :53 and :853 redirected to local resolver,
                   known DoH bootstrap hosts dropped
```

Generated files, all rendered from the DB by `FilterManager` and never hand-edited:

- `/etc/dnsmasq.d/10-biokids-upstream.conf`
- `/etc/dnsmasq.d/20-biokids-safesearch.conf`
- `/etc/dnsmasq.d/30-biokids-block.conf`
- `/etc/chromium/policies/managed/biokids-<child>.json`
- `/etc/nftables.d/biokids.nft`

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
- Phase 4's per-app network rules attach to the same cgroups

Focus tracking on X11: `_NET_ACTIVE_WINDOW` via python-xlib, mapped to a PID via
`_NET_WM_PID`, mapped to a cgroup via `/proc/<pid>/cgroup`.

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
| daemon unreachable from the launcher | the launcher shows a holding screen and starts nothing. A working launcher with no limits behind it is the failure principle #3 exists to prevent |
| daemon unreachable from the panel | the children are still listed, from the database, with their live state shown as unknown and the action buttons withheld. "Not responding" is never rendered as "no children" |
| panel crashes | restart; enforcement is unaffected |
| dnsmasq fails to reload | revert to the previous config; if that fails, lock and alert |
| AI worker crashes | log, alert the parent, continue without AI. Never blocks the child |
| voice service crashes | fall back to a plain on-screen banner |
| DB corrupt | fail closed: lock the child session, show a recovery message to the parent |
| disk full | stop writing evidence first, then ticks; alert the parent |
| no network | everything works; blocklist refresh and email are queued |

The rule: **enforcement failures lock; convenience failures degrade.**
