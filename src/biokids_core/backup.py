"""Yedekle ve taşı: the family's BioKids in one file, for a new computer (D-238).

A family replaces a computer every few years, and until now everything their
children built up on it stayed behind: the sticker album, the English words,
the history, the rules a parent tuned over months. This is one file the
parent downloads from the panel - to their phone, to a USB stick - and gives
to the panel of the new machine.

What is in it: each child as the panel knows them (name, birth year, picture,
language, screen, birthday), their rules, their apps, their sites, their
special days to come, their English progress, their minutes per day and per
app, the family's own search words, and the parent's own recorded voices.
What is never in it: any PIN, passphrase or recovery code, the mail settings
(encrypted with this machine's own key), sessions, alerts, sites visited and
the audit log. A new computer gets new secrets, from its own wizard.

The file is encrypted with the family's passphrase: argon2id makes the key,
AES-256-GCM seals the contents with the header as associated data, so a file
that was changed in any byte does not open. Both come from libraries BioKids
already uses (argon2-cffi, cryptography). Nothing leaves the machine except
as the file the parent asked for, through their own browser.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime as dt
import gzip
import json
import logging
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from argon2.exceptions import Argon2Error
from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core import appearance, repo
from biokids_core.db.models import (
    CategoryLimit,
    Child,
    ChildApp,
    DayOverride,
    EnglishItem,
    FilterRule,
    LedgerDay,
    Policy,
    ReadingItem,
    UsageTick,
    birthday_value,
    utcnow,
)
from biokids_core.special_days import MAX_RANGE_DAYS
from biokids_core.types import AgeBand, Category, EndAction, FilterMode

log = logging.getLogger(__name__)

MAGIC: Final = b"BIOKIDS-BACKUP 1\n"
FORMAT: Final = 1
SUFFIX: Final = ".biokids"
LAST_BACKUP_SETTING: Final = "backup_last_at"
"""When the last file was downloaded, naive UTC ISO: Koruma durumu says how long ago."""

MAX_FILE_BYTES: Final = 32 * 1024 * 1024
"""A family's file is a few hundred kilobytes, recordings and all."""

KDF: Final = {"type": "argon2id", "time_cost": 3, "memory_cost": 65536, "parallelism": 4}
"""The PIN's own parameters (SECURITY.md section 3): about a second per try."""

POLICY_FIELDS: Final = (
    "weekday_quota_min",
    "weekend_quota_min",
    "windows_json",
    "schedule_json",
    "end_action",
    "lock_countdown_seconds",
    "break_enabled",
    "break_work_min",
    "break_rest_min",
    "earn_enabled",
    "earn_ratio",
    "earn_cap_min",
    "earn_min_session_min",
    "earn_idle_pause_min",
    "earn_spendable_general",
    "finish_up_min",
    "learn_first_min",
    "filter_mode",
    "assistant_enabled",
    "assistant_voice",
    "voice_enabled",
    "voice_events_json",
    "enabled",
)
CHILD_FIELDS: Final = (
    "display_name",
    "birth_year",
    "age_band",
    "avatar",
    "locale",
    "supervision_indicator",
    "enabled",
    "first_run_done",
    "text_size",
    "high_contrast",
    "reading_font",
    "calm_screen",
    "birthday",
)


class BackupError(ValueError):
    """The file cannot be read: not a BioKids file, the wrong passphrase, or changed."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key
        """An i18n key for the parent."""


# ------------------------------------------------------------------ the seal


def _key(passphrase: str, salt: bytes, kdf: Mapping[str, Any]) -> bytes:
    return hash_secret_raw(
        secret=passphrase.strip().encode("utf-8"),
        salt=salt,
        time_cost=int(kdf["time_cost"]),
        memory_cost=int(kdf["memory_cost"]),
        parallelism=int(kdf["parallelism"]),
        hash_len=32,
        type=Type.ID,
    )


def seal(
    contents: Mapping[str, Any], passphrase: str, *, now: dt.datetime | None = None
) -> bytes:
    """The file: a readable first line, a JSON header, then the sealed contents."""
    salt, nonce = os.urandom(16), os.urandom(12)
    kdf = dict(KDF)
    header = json.dumps(
        {
            "format": FORMAT,
            "created": (now or utcnow()).isoformat(timespec="seconds"),
            "kdf": kdf,
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
        },
        sort_keys=True,
    ).encode("utf-8")
    plain = gzip.compress(json.dumps(contents, ensure_ascii=False).encode("utf-8"), mtime=0)
    sealed = AESGCM(_key(passphrase, salt, kdf)).encrypt(nonce, plain, MAGIC + header)
    return MAGIC + header + b"\n" + sealed


def open_sealed(data: bytes, passphrase: str) -> dict[str, Any]:
    """The contents of a file `seal` wrote, or BackupError."""
    if len(data) > MAX_FILE_BYTES or not data.startswith(MAGIC):
        raise BackupError("backup.error.not_ours")
    try:
        header_raw, sealed = data[len(MAGIC) :].split(b"\n", 1)
        header = json.loads(header_raw)
        if int(header["format"]) != FORMAT:
            raise BackupError("backup.error.version")
        kdf = header["kdf"]
        # Only the parameters BioKids writes: the header is not authenticated
        # until the key has been made, so a file must not choose how long, or
        # with how much memory, the panel works to make it (D-238).
        if kdf != dict(KDF):
            raise BackupError("backup.error.not_ours")
        salt = base64.b64decode(header["salt"])
        nonce = base64.b64decode(header["nonce"])
    except BackupError:
        raise
    except (ValueError, KeyError, TypeError):
        raise BackupError("backup.error.not_ours") from None
    try:
        key = _key(passphrase, salt, kdf)
    except (Argon2Error, ValueError, TypeError):
        raise BackupError("backup.error.not_ours") from None
    try:
        plain = AESGCM(key).decrypt(nonce, sealed, MAGIC + header_raw)
    except (InvalidTag, ValueError):
        raise BackupError("backup.error.passphrase") from None
    try:
        contents = json.loads(gzip.decompress(plain))
    except (OSError, ValueError):
        raise BackupError("backup.error.not_ours") from None
    if not isinstance(contents, dict) or contents.get("format") != FORMAT:
        raise BackupError("backup.error.version")
    return contents


# ---------------------------------------------------------------- exporting


def _row(obj: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: _plain(getattr(obj, name)) for name in fields}


def _plain(value: Any) -> Any:
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def export(
    session: Session, *, recordings_dir: Path | None = None, today: dt.date | None = None
) -> dict[str, Any]:
    """Everything the file carries, as plain data."""
    today = today or dt.date.today()
    children: list[dict[str, Any]] = []
    for child in repo.list_children(session):
        policy = session.get(Policy, child.id)
        children.append(
            {
                "unix_user": child.unix_user,
                "child": _row(child, CHILD_FIELDS),
                "policy": _row(policy, POLICY_FIELDS) if policy else None,
                "category_limits": {
                    row.category: row.minutes
                    for row in session.scalars(
                        select(CategoryLimit).where(CategoryLimit.child_id == child.id)
                    )
                },
                "apps": [
                    {
                        "slug": link.app_slug,
                        "enabled": link.enabled,
                        "category_override": link.category_override,
                        "position": link.position,
                    }
                    for link in session.scalars(
                        select(ChildApp).where(ChildApp.child_id == child.id)
                    )
                ],
                "filter_rules": [
                    {"kind": rule.kind, "pattern": rule.pattern, "source": rule.source}
                    for rule in session.scalars(
                        select(FilterRule).where(FilterRule.child_id == child.id)
                    )
                ],
                "special_days": [
                    {
                        "start_day": row.start_day.isoformat(),
                        "end_day": row.end_day.isoformat(),
                        "extra_minutes": row.extra_minutes,
                        "quota_minutes": row.quota_minutes,
                        "no_limit": row.no_limit,
                        "weekend_rules": row.weekend_rules,
                        "label": row.label,
                    }
                    for row in session.scalars(
                        select(DayOverride).where(
                            DayOverride.child_id == child.id, DayOverride.end_day >= today
                        )
                    )
                ],
                "english": [
                    {
                        "item": row.item,
                        "box": row.box,
                        "due": row.due.isoformat(),
                        "first_day": row.first_day.isoformat(),
                        "right": row.right,
                        "wrong": row.wrong,
                        "last_day": row.last_day.isoformat(),
                    }
                    for row in session.scalars(
                        select(EnglishItem).where(EnglishItem.child_id == child.id)
                    )
                ],
                "reading": [
                    {
                        "item": row.item,
                        "box": row.box,
                        "due": row.due.isoformat(),
                        "first_day": row.first_day.isoformat(),
                        "right": row.right,
                        "wrong": row.wrong,
                        "last_day": row.last_day.isoformat(),
                    }
                    for row in session.scalars(
                        select(ReadingItem).where(ReadingItem.child_id == child.id)
                    )
                ],
                "days": [
                    {
                        "day": row.day.isoformat(),
                        "general_s": row.general_s,
                        "earned_s": row.earned_s,
                        "granted_s": row.granted_s,
                    }
                    for row in session.scalars(
                        select(LedgerDay).where(
                            LedgerDay.child_id == child.id, LedgerDay.day < today
                        )
                    )
                ],
                "usage": [
                    {
                        "day": row.day.isoformat(),
                        "category": row.category,
                        "app_slug": row.app_slug,
                        "seconds": row.seconds,
                    }
                    for row in session.scalars(
                        select(UsageTick).where(
                            UsageTick.child_id == child.id, UsageTick.day < today
                        )
                    )
                ],
            }
        )
    return {
        "format": FORMAT,
        "children": children,
        "search_words": repo.search_words(session),
        "recordings": _recordings(recordings_dir),
    }


RECORDING_SUFFIXES: Final = (".wav", ".txt")
RECORDINGS_MAX_BYTES: Final = 8 * 1024 * 1024


def _recordings(directory: Path | None) -> dict[str, str]:
    """The parent's own voice lines (D-174): `<locale>/<event>.wav` and `.txt`."""
    found: dict[str, str] = {}
    if directory is None or not directory.is_dir():
        return found
    total = 0
    for path in sorted(directory.glob("*/*")):
        if path.suffix not in RECORDING_SUFFIXES or not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        total += len(data)
        if total > RECORDINGS_MAX_BYTES:
            log.warning(
                "the recordings are larger than a backup carries; the rest are left out"
            )
            break
        found[f"{path.parent.name}/{path.name}"] = base64.b64encode(data).decode("ascii")
    return found


# ---------------------------------------------------------------- importing


@dataclasses.dataclass
class Restored:
    children: list[str] = dataclasses.field(default_factory=list)
    """The names of the children restored, as the panel will show them."""
    created: list[Child] = dataclasses.field(default_factory=list)
    """Children who had no account here yet: the caller gives them one."""
    recordings: int = 0


CreateChild = Callable[[Session, Mapping[str, Any]], Child | None]


def restore(
    session: Session,
    contents: Mapping[str, Any],
    *,
    create_child: CreateChild,
    known_apps: set[str],
    known_avatars: set[str] | None = None,
    recordings_dir: Path | None = None,
) -> Restored:
    """Put a file's contents into this machine's database.

    A child already here (by account name) is updated; one who is not is made by
    `create_child`, the panel's own way of adding a child. What a child did on the
    old computer is added where this one has nothing for that day, never written
    over what happened here. Apps this machine's catalogue does not know are left
    out. Every value is checked as the panel's own forms check it, and a file can
    switch protection on but never off: that is behind this machine's passphrase
    (D-48), not the old one's. Does not commit: the caller does, once every child is in.
    """
    done = Restored()
    for entry in contents.get("children", []):
        if not isinstance(entry, Mapping):
            continue
        child = repo.get_child_by_user(session, str(entry.get("unix_user", "")))
        if child is None:
            child = create_child(session, entry)
            if child is None:
                continue
            done.created.append(child)
        _restore_child(session, child, entry, known_apps, known_avatars)
        done.children.append(child.display_name)
    words = contents.get("search_words")
    if isinstance(words, list) and words:
        merged = list(dict.fromkeys([*repo.search_words(session), *[str(w) for w in words]]))
        repo.set_search_words(session, merged)
    done.recordings = _restore_recordings(contents.get("recordings"), recordings_dir)
    return done


def _restore_child(
    session: Session,
    child: Child,
    entry: Mapping[str, Any],
    known_apps: set[str],
    known_avatars: set[str] | None,
) -> None:
    fields = entry.get("child")
    if isinstance(fields, Mapping):
        for name, value in _child_values(fields, known_avatars).items():
            setattr(child, name, value)
    session.flush()
    raw_policy = entry.get("policy")
    if isinstance(raw_policy, Mapping):
        policy = repo.get_or_create_policy(session, child.id)
        for name, value in _policy_values(raw_policy).items():
            setattr(policy, name, value)

    limits = entry.get("category_limits")
    if isinstance(limits, Mapping) and all(
        str(category) in CATEGORIES and _int_in(minutes, 0, 1440) is not None
        for category, minutes in limits.items()
    ):
        for row in session.scalars(
            select(CategoryLimit).where(CategoryLimit.child_id == child.id)
        ):
            session.delete(row)
        session.flush()
        for category, minutes in limits.items():
            session.add(
                CategoryLimit(child_id=child.id, category=str(category), minutes=int(minutes))
            )

    apps = entry.get("apps")
    if isinstance(apps, list):
        for app in apps:
            slug = str(app.get("slug", ""))
            if slug not in known_apps:
                continue
            link = session.get(ChildApp, {"child_id": child.id, "app_slug": slug})
            if link is None:
                link = ChildApp(child_id=child.id, app_slug=slug)
                session.add(link)
            link.enabled = bool(app.get("enabled", True))
            link.category_override = app.get("category_override")
            link.position = int(app.get("position", 0))

    for rule in entry.get("filter_rules") or []:
        kind, pattern = str(rule.get("kind", "")), str(rule.get("pattern", "")).lower()
        # Only a site's name, as the panel makes them (D-250): a pattern like
        # "*" in the allow list would undo "selected sites only" in Chromium.
        if kind not in ("allow", "block") or not HOST.fullmatch(pattern):
            continue
        exists = session.scalar(
            select(FilterRule).where(
                FilterRule.child_id == child.id,
                FilterRule.kind == kind,
                FilterRule.pattern == pattern,
            )
        )
        if exists is None:
            session.add(
                FilterRule(
                    child_id=child.id,
                    kind=kind,
                    pattern=pattern,
                    source=str(rule.get("source", "manual"))[:16],
                )
            )

    for special in entry.get("special_days") or []:
        values = _special_values(special)
        if values is None:
            continue
        # The same file restored twice must not add the same day twice: the
        # extra minutes of two "+60 today" add up (D-250).
        same_day = session.scalar(
            select(DayOverride).where(
                DayOverride.child_id == child.id,
                *(getattr(DayOverride, name) == value for name, value in values.items()),
            )
        )
        if same_day is None:
            session.add(DayOverride(child_id=child.id, **values))

    for item in entry.get("english") or []:
        known = session.get(EnglishItem, {"child_id": child.id, "item": str(item["item"])})
        if known is not None and known.box >= int(item.get("box", 1)):
            continue
        if known is None:
            known = EnglishItem(child_id=child.id, item=str(item["item"]))
            session.add(known)
        known.box = int(item.get("box", 1))
        known.due = dt.date.fromisoformat(item["due"])
        known.first_day = dt.date.fromisoformat(item["first_day"])
        known.last_day = dt.date.fromisoformat(item["last_day"])
        known.right = int(item.get("right", 0))
        known.wrong = int(item.get("wrong", 0))

    for item in entry.get("reading") or []:
        learning = session.get(ReadingItem, {"child_id": child.id, "item": str(item["item"])})
        if learning is not None and learning.box >= int(item.get("box", 1)):
            continue
        if learning is None:
            learning = ReadingItem(child_id=child.id, item=str(item["item"]))
            session.add(learning)
        learning.box = int(item.get("box", 1))
        learning.due = dt.date.fromisoformat(item["due"])
        learning.first_day = dt.date.fromisoformat(item["first_day"])
        learning.last_day = dt.date.fromisoformat(item["last_day"])
        learning.right = int(item.get("right", 0))
        learning.wrong = int(item.get("wrong", 0))

    # A day this machine already counted keeps its own numbers: its minutes
    # and its minutes per app together, never one of them from the file (D-250).
    counted = set(session.scalars(select(LedgerDay.day).where(LedgerDay.child_id == child.id)))
    for day in entry.get("days") or []:
        when = dt.date.fromisoformat(day["day"])
        if (
            when not in counted
            and session.get(LedgerDay, {"child_id": child.id, "day": when}) is None
        ):
            session.add(
                LedgerDay(
                    child_id=child.id,
                    day=when,
                    general_s=int(day.get("general_s", 0)),
                    earned_s=int(day.get("earned_s", 0)),
                    granted_s=int(day.get("granted_s", 0)),
                )
            )

    for usage in entry.get("usage") or []:
        key = {
            "child_id": child.id,
            "day": dt.date.fromisoformat(usage["day"]),
            "category": str(usage["category"]),
            "app_slug": str(usage.get("app_slug", "")),
        }
        if key["day"] not in counted and session.get(UsageTick, key) is None:
            session.add(UsageTick(**key, seconds=int(usage.get("seconds", 0))))
    session.flush()


HOST: Final = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
"""A site's name, as an allow or block rule holds it: no wildcard, no path."""


def _special_values(special: Mapping[str, Any]) -> dict[str, Any] | None:
    """A special day from a file, as the panel would have made it, or None
    (`special_days`: at most 100 days, minutes within a day, a day without a
    limit only a single day)."""
    try:
        start = dt.date.fromisoformat(str(special["start_day"]))
        end = dt.date.fromisoformat(str(special["end_day"]))
    except (KeyError, ValueError):
        return None
    extra = _int_in(special.get("extra_minutes", 0), 0, 24 * 60)
    quota = special.get("quota_minutes")
    no_limit = special.get("no_limit", False) is True
    if (
        end < start
        or (end - start).days + 1 > MAX_RANGE_DAYS
        or extra is None
        or (quota is not None and _int_in(quota, 0, 24 * 60) is None)
        or (no_limit and end != start)
    ):
        return None
    return {
        "start_day": start,
        "end_day": end,
        "extra_minutes": extra,
        "quota_minutes": quota,
        "no_limit": no_limit,
        "weekend_rules": special.get("weekend_rules", False) is True,
        "label": " ".join(str(special.get("label", "")).split())[:40],
    }


CATEGORIES: Final = frozenset(category.value for category in Category)
INDICATORS: Final = frozenset({"badge", "small", "hidden"})
LOCALES: Final = frozenset({"tr", "en"})
FINISH_UP_CHOICES: Final = frozenset({0, 2, 3, 5})


def _int_in(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _child_values(fields: Mapping[str, Any], avatars: set[str] | None) -> dict[str, Any]:
    """What may be taken from a file for a child: each value as the edit form
    would take it, and `enabled` only ever switched on."""
    out: dict[str, Any] = {}
    name = " ".join(str(fields.get("display_name", "")).split())[:64]
    if name:
        out["display_name"] = name
    this_year = dt.date.today().year
    year = _int_in(fields.get("birth_year"), this_year - 14, this_year)
    if year is not None:
        out["birth_year"] = year
        out["age_band"] = AgeBand.for_age(this_year - year).value
    if fields.get("age_band") in {band.value for band in AgeBand}:
        out["age_band"] = fields["age_band"]
    avatar = fields.get("avatar")
    if isinstance(avatar, str) and (avatars is None or avatar in avatars):
        out["avatar"] = avatar
    if fields.get("locale") in LOCALES:
        out["locale"] = fields["locale"]
    if fields.get("supervision_indicator") in INDICATORS:
        out["supervision_indicator"] = fields["supervision_indicator"]
    if fields.get("enabled") is True:
        out["enabled"] = True
    for flag in ("first_run_done", "high_contrast", "reading_font", "calm_screen"):
        if isinstance(fields.get(flag), bool):
            out[flag] = fields[flag]
    if isinstance(fields.get("text_size"), str):
        out["text_size"] = appearance.text_size(fields["text_size"])
    birthday = fields.get("birthday")
    if birthday is None or birthday == "":
        out["birthday"] = None
    elif isinstance(birthday, str) and len(birthday) == 5 and birthday[2] == "-":
        with contextlib.suppress(ValueError):
            out["birthday"] = birthday_value(int(birthday[:2]), int(birthday[3:]))
    return out


def _policy_reads(blob: str, value: str, reads_as: str) -> bool:
    """Whether a policy holding `value` can be read back - and the windows
    each a time range (D-250). `{"weekday": 5}` used to be restored, and then
    neither the daemon nor the panel's time page could load the policy."""
    try:
        read = getattr(Policy(**{blob: value}), reads_as)
    except (TypeError, ValueError, AttributeError):
        return False
    if reads_as == "windows":
        return all(WINDOW.fullmatch(window) for windows in read.values() for window in windows)
    return True


WINDOW: Final = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-4]):[0-5]\d")


def _json_object(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return json.dumps(parsed) if isinstance(parsed, dict) else None


POLICY_RANGES: Final = {
    "weekday_quota_min": (0, 1440),
    "weekend_quota_min": (0, 1440),
    "lock_countdown_seconds": (10, 3600),
    "break_work_min": (5, 240),
    "break_rest_min": (1, 60),
    "earn_cap_min": (0, 480),
    "earn_min_session_min": (1, 120),
    "earn_idle_pause_min": (1, 60),
    "learn_first_min": (0, 120),
}
POLICY_FLAGS: Final = (
    "break_enabled",
    "earn_enabled",
    "earn_spendable_general",
    "assistant_enabled",
    "assistant_voice",
    "voice_enabled",
)


def _policy_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The rules a file may carry, each checked; protection is never turned off."""
    out: dict[str, Any] = {}
    for name, (low, high) in POLICY_RANGES.items():
        value = _int_in(raw.get(name), low, high)
        if value is not None:
            out[name] = value
    if raw.get("finish_up_min") in FINISH_UP_CHOICES and not isinstance(
        raw.get("finish_up_min"), bool
    ):
        out["finish_up_min"] = raw["finish_up_min"]
    ratio = raw.get("earn_ratio")
    if isinstance(ratio, int | float) and not isinstance(ratio, bool) and 0 <= ratio <= 5:
        out["earn_ratio"] = float(ratio)
    for flag in POLICY_FLAGS:
        if isinstance(raw.get(flag), bool):
            out[flag] = raw[flag]
    if raw.get("end_action") in {action.value for action in EndAction}:
        out["end_action"] = raw["end_action"]
    if raw.get("filter_mode") in {mode.value for mode in FilterMode}:
        out["filter_mode"] = raw["filter_mode"]
    for blob, reads_as in (
        ("windows_json", "windows"),
        ("schedule_json", "schedule"),
        ("voice_events_json", "voice_events"),
    ):
        cleaned = _json_object(raw.get(blob))
        if cleaned is not None and _policy_reads(blob, cleaned, reads_as):
            out[blob] = cleaned
    if raw.get("enabled") is True:
        out["enabled"] = True
    return out


def _restore_recordings(raw: object, directory: Path | None) -> int:
    """The parent's own voice lines, where this machine has none of its own."""
    if directory is None or not isinstance(raw, Mapping):
        return 0
    written = 0
    for name, encoded in raw.items():
        locale, _, filename = str(name).partition("/")
        path = Path(filename)
        if (
            not locale.isalpha()
            or len(locale) > 8
            or path.name != filename
            or path.suffix not in RECORDING_SUFFIXES
            or filename.startswith(".")
        ):
            continue
        target = directory / locale / filename
        if target.exists():
            continue
        try:
            data = base64.b64decode(str(encoded), validate=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o644)
        except (OSError, ValueError) as exc:
            log.warning("a recording was not restored: %s", exc)
            continue
        written += 1
    return written


def last_taken(session: Session) -> dt.datetime | None:
    raw = repo.get_setting(session, LAST_BACKUP_SETTING, "")
    try:
        return dt.datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def file_name(today: dt.date | None = None) -> str:
    return f"biokids-yedek-{(today or dt.date.today()).isoformat()}{SUFFIX}"
