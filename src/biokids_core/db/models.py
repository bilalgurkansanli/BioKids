"""SQLAlchemy models for `/var/lib/biokids/biokids.db`.

The schema is the one in `ARCHITECTURE.md` §5, plus three tables that section
implies without naming: `boot_epoch` and `ledger_day` (the ledger has to survive
a reboot, §3.1) and `panel_session` (a signed cookie alone cannot be revoked).

Conventions:

* Enum-ish columns store the plain string value of a `StrEnum`. SQLite has no
  enum type, and a readable value in a database a parent might one day open with
  `sqlite3` is worth more than four bytes.
* Times are stored as UTC-naive `DATETIME` for ordering, and `day` columns are
  `DATE` in the family's local timezone — which calendar day a quota belongs to
  is a local question, not a UTC one.
* Anything JSON-shaped is `TEXT` holding `json.dumps` output, with a typed
  property beside it. SQLAlchemy's JSON type on SQLite is the same thing with
  less control over the failure mode.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from biokids_core.types import (
    AgeBand,
    Category,
    EndAction,
    FilterMode,
)


def utcnow() -> dt.datetime:
    """Naive UTC. Used as a column default; never for time accounting."""
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base. `Base.metadata` is what Alembic autogenerates against."""


# ------------------------------------------------------------------ the family


class Family(Base):
    __tablename__ = "family"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    locale: Mapped[str] = mapped_column(String(8), default="tr")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Istanbul")
    setup_completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    """Null until the wizard finishes. The panel redirects to the wizard while null."""


class Parent(Base):
    """One person who may sign in to the panel (D-218).

    Each has their own PIN, lockout and recovery code. What the family shares -
    the passphrase and the mail settings - is kept on the first parent still in
    use (`repo.get_parent`), and moves to the next one if that parent is
    removed. A removed parent keeps their row and name, with every secret
    cleared, so the audit log can still say who did what and an id is never
    given to somebody else.
    """

    __tablename__ = "parent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(64), default="")
    pin_hash: Mapped[str] = mapped_column(Text, default="")
    passphrase_hash: Mapped[str] = mapped_column(Text, default="")
    recovery_hash: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    smtp_json_encrypted: Mapped[str | None] = mapped_column(Text, default=None)

    # Rate limiting for the PIN gate (D-48). Kept on the row rather than in
    # memory so a daemon restart cannot be used to clear a lockout.
    pin_failures: Mapped[int] = mapped_column(Integer, default=0)
    pin_locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    pin_lockout_level: Mapped[int] = mapped_column(Integer, default=0)
    """Number of lockouts served. The next one lasts twice as long (D-48)."""

    text_size: Mapped[str] = mapped_column(String(8), default="normal", server_default="normal")
    high_contrast: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    """How the panel is drawn for this parent (D-217), on whichever device."""

    removed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    """Set when another parent removed this one (D-218); they can no longer sign in."""

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def active(self) -> bool:
        return self.removed_at is None


class PanelSession(Base):
    """A logged-in parent browser. The cookie carries the token; we store its hash."""

    __tablename__ = "panel_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id", ondelete="CASCADE"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    passphrase_ok_until: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    """A short grace window after the passphrase gate, so one destructive flow
    does not ask three times."""
    user_agent: Mapped[str] = mapped_column(String(255), default="")


# ------------------------------------------------------------------- children


class Child(Base):
    __tablename__ = "child"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    unix_user: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(64))
    birth_year: Mapped[int] = mapped_column(Integer)
    age_band: Mapped[str] = mapped_column(String(8), default=AgeBand.YOUNG.value)
    avatar: Mapped[str] = mapped_column(String(64), default="fox")
    locale: Mapped[str] = mapped_column(String(8), default="tr")
    shell_mode: Mapped[str] = mapped_column(String(16), default="kiosk")
    supervision_indicator: Mapped[str] = mapped_column(String(16), default="badge")
    """`badge` | `small` | `hidden`. Hidden still runs the first-run explanation (D-76)."""
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    first_run_done: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    # The child's screen (D-217): text size, contrast and the reading font,
    # chosen by a parent and drawn by the launcher.
    text_size: Mapped[str] = mapped_column(String(8), default="normal", server_default="normal")
    high_contrast: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    reading_font: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")

    policy: Mapped[Policy | None] = relationship(
        back_populates="child", uselist=False, cascade="all, delete-orphan"
    )

    def age_on(self, day: dt.date) -> int:
        """Age in whole years. Birth *year* only — we never ask for a birth date."""
        return max(0, day.year - self.birth_year)

    def band(self, day: dt.date) -> AgeBand:
        try:
            return AgeBand(self.age_band)
        except ValueError:
            return AgeBand.for_age(self.age_on(day))


class Policy(Base):
    __tablename__ = "policy"

    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), primary_key=True
    )

    weekday_quota_min: Mapped[int] = mapped_column(Integer, default=60)
    weekend_quota_min: Mapped[int] = mapped_column(Integer, default=120)
    windows_json: Mapped[str] = mapped_column(Text, default='{"weekday": [], "weekend": []}')
    schedule_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    """Per day of the week (D-176), over the weekday/weekend pair: `{"0": {"quota":
    45, "windows": ["16:00-18:00"]}}` with Monday as 0. A day that is not there,
    or a key a day does not name, follows the weekday or weekend rule."""

    end_action: Mapped[str] = mapped_column(String(16), default=EndAction.POWEROFF.value)
    lock_countdown_seconds: Mapped[int] = mapped_column(Integer, default=120)

    break_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    break_work_min: Mapped[int] = mapped_column(Integer, default=40)
    break_rest_min: Mapped[int] = mapped_column(Integer, default=5)

    earn_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    earn_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    earn_cap_min: Mapped[int] = mapped_column(Integer, default=60)
    earn_min_session_min: Mapped[int] = mapped_column(Integer, default=10)
    earn_idle_pause_min: Mapped[int] = mapped_column(Integer, default=3)
    earn_spendable_general: Mapped[bool] = mapped_column(Boolean, default=False)

    filter_mode: Mapped[str] = mapped_column(String(16), default=FilterMode.ALLOWLIST.value)

    assistant_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    """The question-answering assistant (D-165). Off until a parent turns it on."""
    assistant_voice: Mapped[bool] = mapped_column(Boolean, default=False)
    """Press-and-speak for the assistant (D-166): the only use of a microphone."""

    voice_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    voice_events_json: Mapped[str] = mapped_column(Text, default="{}")
    """Per-event overrides; missing keys fall back to the defaults in VOICE.md §5."""

    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    child: Mapped[Child] = relationship(back_populates="policy")

    __table_args__ = (
        CheckConstraint("weekday_quota_min >= 0", name="ck_policy_weekday_quota"),
        CheckConstraint("weekend_quota_min >= 0", name="ck_policy_weekend_quota"),
        CheckConstraint("earn_ratio >= 0", name="ck_policy_earn_ratio"),
    )

    @property
    def windows(self) -> dict[str, list[str]]:
        raw = _load_json(self.windows_json, {})
        return {
            "weekday": [str(x) for x in raw.get("weekday", [])],
            "weekend": [str(x) for x in raw.get("weekend", [])],
        }

    @windows.setter
    def windows(self, value: dict[str, list[str]]) -> None:
        self.windows_json = json.dumps(
            {"weekday": value.get("weekday", []), "weekend": value.get("weekend", [])}
        )

    @property
    def schedule(self) -> dict[int, dict[str, Any]]:
        raw = _load_json(self.schedule_json, {})
        days: dict[int, dict[str, Any]] = {}
        for key, entry in raw.items():
            if not str(key).isdigit() or not 0 <= int(key) <= 6 or not isinstance(entry, dict):
                continue
            day: dict[str, Any] = {}
            quota = entry.get("quota")
            if isinstance(quota, int) and 0 <= quota <= 1440:
                day["quota"] = quota
            windows = entry.get("windows")
            if isinstance(windows, list):
                day["windows"] = [str(w) for w in windows]
            if day:
                days[int(key)] = day
        return days

    @schedule.setter
    def schedule(self, value: dict[int, dict[str, Any]]) -> None:
        self.schedule_json = json.dumps({str(k): v for k, v in sorted(value.items()) if v})

    @property
    def voice_events(self) -> dict[str, bool]:
        return {str(k): bool(v) for k, v in _load_json(self.voice_events_json, {}).items()}

    @voice_events.setter
    def voice_events(self, value: dict[str, bool]) -> None:
        self.voice_events_json = json.dumps(value)


class CategoryLimit(Base):
    __tablename__ = "category_limit"

    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), primary_key=True
    )
    category: Mapped[str] = mapped_column(String(16), primary_key=True)
    minutes: Mapped[int] = mapped_column(Integer)

    __table_args__ = (CheckConstraint("minutes >= 0", name="ck_category_limit_minutes"),)


# ------------------------------------------------------------------ the catalog


class App(Base):
    """The catalog. Metadata, never hardcoded lists (D-20)."""

    __tablename__ = "app"

    slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    name_tr: Mapped[str] = mapped_column(String(64))
    name_en: Mapped[str] = mapped_column(String(64))
    exec_cmd: Mapped[str] = mapped_column("exec", String(255))
    icon: Mapped[str] = mapped_column(String(64), default="app")
    default_category: Mapped[str] = mapped_column(String(16), default=Category.FREE.value)
    min_age: Mapped[int] = mapped_column(Integer, default=4)
    max_age: Mapped[int] = mapped_column(Integer, default=10)
    package: Mapped[str] = mapped_column(String(64), default="")
    """Debian package that provides `exec`. Empty for built-in tiles."""
    kind: Mapped[str] = mapped_column(String(16), default="app")
    """`app` | `web` | `builtin`. Web tiles open the browser at a URL."""
    url: Mapped[str] = mapped_column(String(255), default="")
    description_tr: Mapped[str] = mapped_column(Text, default="")
    description_en: Mapped[str] = mapped_column(Text, default="")

    def name(self, locale: str) -> str:
        return self.name_tr if locale.startswith("tr") else self.name_en

    def description(self, locale: str) -> str:
        return self.description_tr if locale.startswith("tr") else self.description_en


class ChildApp(Base):
    __tablename__ = "child_app"

    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), primary_key=True
    )
    app_slug: Mapped[str] = mapped_column(
        ForeignKey("app.slug", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    category_override: Mapped[str | None] = mapped_column(String(16), default=None)
    position: Mapped[int] = mapped_column(Integer, default=0)


# ------------------------------------------------------------------- filtering


class FilterRule(Base):
    __tablename__ = "filter_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(8))
    """`allow` | `block`."""
    pattern: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(16), default="manual")
    """`manual` | `list` | `request` — so an approved request can be found again."""
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("child_id", "kind", "pattern", name="uq_filter_rule"),
        CheckConstraint("kind in ('allow','block')", name="ck_filter_rule_kind"),
    )


class AccessRequest(Base):
    """A child pressed "ask a parent" on the block page (D-65)."""

    __tablename__ = "access_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str] = mapped_column(String(16), default="site", server_default="site")
    """`time` (more minutes today) | `site` | `app` (D-173)."""
    minutes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    """For `time`: what the parent granted when they approved it."""
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    """`pending` | `approved` | `denied` | `expired`."""
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    decided_by: Mapped[int | None] = mapped_column(Integer, default=None)


# ---------------------------------------------------------------- the ledger


class BootEpoch(Base):
    """One row per boot, so `CLOCK_BOOTTIME` readings can be compared across reboots.

    `boot_id` comes from `/proc/sys/kernel/random/boot_id`. A `boot_id` we have
    not seen means a new epoch: accumulated totals carry forward from
    `ledger_day`, and the boot offset restarts from zero (`ARCHITECTURE.md` §3.1).
    """

    __tablename__ = "boot_epoch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    boot_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    first_seen_wall: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_wall: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_boot_offset: Mapped[float] = mapped_column(Float, default=0.0)
    """The highest BOOTTIME reading persisted during this epoch, in seconds."""


class LedgerDay(Base):
    """The four counters of `ARCHITECTURE.md` §3.3, for one child on one day.

    `category_used` lives in `usage_tick` because it is per-category; everything
    else is a scalar and belongs here. Written every 30 s and on every state
    change, so a hard power cut costs at most half a minute.
    """

    __tablename__ = "ledger_day"

    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)

    general_s: Mapped[int] = mapped_column(Integer, default=0)
    earned_s: Mapped[int] = mapped_column(Integer, default=0)
    penalty_remaining_s: Mapped[int] = mapped_column(Integer, default=0)
    break_remaining_s: Mapped[int] = mapped_column(Integer, default=0)
    seconds_since_break_s: Mapped[int] = mapped_column(Integer, default=0)
    learning_streak_s: Mapped[int] = mapped_column(Integer, default=0)
    learning_credited_s: Mapped[int] = mapped_column(Integer, default=0)
    """How much of the current learning streak has already been turned into
    earned minutes, so a streak that keeps growing does not pay twice."""
    granted_s: Mapped[int] = mapped_column(Integer, default=0)
    """Minutes the parent added by hand with "+10 dk" (D-47), for the audit view."""

    warned_json: Mapped[str] = mapped_column(Text, default="[]")
    """Warning thresholds already spoken today. Survives a daemon restart so a
    crash at minute 9 does not make the child hear "ten minutes left" twice."""

    end_sequence_started_at: Mapped[float | None] = mapped_column(Float, default=None)
    """BOOTTIME reading when the lock screen appeared, for the 120 s countdown."""

    end_sequence_boot_id: Mapped[str | None] = mapped_column(String(64), default=None)
    """Which boot the reading above belongs to.

    Without it the offset is meaningless across a reboot: BOOTTIME restarts at
    zero, so an offset recorded at eight hours of uptime reads as "the countdown
    has not started" for the next eight hours — during which the child is
    neither locked nor shut down."""

    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def warned(self) -> set[int]:
        return {int(x) for x in _load_json(self.warned_json, [])}

    @warned.setter
    def warned(self, value: set[int]) -> None:
        self.warned_json = json.dumps(sorted(value))


class UsageTick(Base):
    """Per-category seconds, rolled up nightly. One row per child/day/category."""

    __tablename__ = "usage_tick"

    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    category: Mapped[str] = mapped_column(String(16), primary_key=True)
    app_slug: Mapped[str] = mapped_column(String(64), primary_key=True, default="")
    seconds: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (Index("ix_usage_tick_day", "day"),)


class UsageHour(Base):
    """Screen time per hour of the day, for the insights screen (D-206).

    Nothing else says *when* a child used the computer: `ledger_day` knows the
    day and `usage_tick` the day and the app. One row per child, day and local
    wall-clock hour, filed by the same tick that adds to `general_s`, so a day's
    hours add up to its screen time. An aggregate, never a timeline: the minute
    a session started is not kept. Still, *when* a child used the computer is
    the most personal of the usage records, and no screen reads more than
    thirty days of it: it is kept for `retention.activity_days`, with the sites
    visited, not for `usage_days` with the minutes (D-189). Nothing that
    enforces anything reads it.
    """

    __tablename__ = "usage_hour"

    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[dt.date] = mapped_column(Date, primary_key=True)
    hour: Mapped[int] = mapped_column(Integer, primary_key=True)
    """0-23, the machine's local time, the clock the day's quota is read with."""
    seconds: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        CheckConstraint("hour >= 0 AND hour <= 23", name="ck_usage_hour_hour"),
        Index("ix_usage_hour_day", "day"),
    )


class UsageEvent(Base):
    """An append-only stream of what happened, kept 30 days."""

    __tablename__ = "usage_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int | None] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), index=True, default=None
    )
    ts_wall: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ts_boot: Mapped[float] = mapped_column(Float, default=0.0)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")

    @property
    def payload(self) -> dict[str, Any]:
        return _as_dict(_load_json(self.payload_json, {}))


class AiAlert(Base):
    __tablename__ = "ai_alert"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    score: Mapped[int] = mapped_column(Integer)
    category: Mapped[str] = mapped_column(String(32), default="unknown")
    reason_key: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    band: Mapped[str] = mapped_column(String(16), default="log")
    """`log` | `notify` | `block` — the contract is the band, not the score."""
    app: Mapped[str] = mapped_column(String(64), default="")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    """Set when a parent marks an alert "this was fine" (AI-LAYER.md §7)."""
    text: Mapped[str] = mapped_column(Text, default="")
    """For an assistant alert (app "assistant"), the question the child asked.
    Kept so the parent can talk about it; the child was told a parent may see
    it (D-165). Pruned with the rest of the alert."""


class AuditLog(Base):
    """Append-only. Never truncated by the application (D-53)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64), index=True)
    """`parent:1`, `daemon`, `system`, `child:elif`, `cli`."""
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(128), default="")
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    severity: Mapped[str] = mapped_column(String(16), default="info")
    """`info` | `warning` | `alert`. The lockdown path writes `alert`."""

    @property
    def detail(self) -> dict[str, Any]:
        return _as_dict(_load_json(self.detail_json, {}))


class VoiceLine(Base):
    """A parent-edited override for one spoken line (VOICE.md §5)."""

    __tablename__ = "voice_line"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event: Mapped[str] = mapped_column(String(32))
    locale: Mapped[str] = mapped_column(String(8))
    text: Mapped[str] = mapped_column(Text)
    child_id: Mapped[int | None] = mapped_column(
        ForeignKey("child.id", ondelete="CASCADE"), default=None
    )

    __table_args__ = (Index("ix_voice_line_event", "event", "locale"),)


class Setting(Base):
    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


# ------------------------------------------------------------------- helpers


def _as_dict(value: Any) -> dict[str, Any]:
    """A stored blob that is valid JSON but not an object still has to render."""
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def _load_json(raw: str | None, fallback: Any) -> Any:
    """Never raise on a malformed stored blob; a bad row must not stop enforcement."""
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback
