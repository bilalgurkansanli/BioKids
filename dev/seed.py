#!/usr/bin/env python3
"""Fill a fresh database with a family you can log into, in about a second.

Walking the setup wizard by hand is the right test exactly once. After that it is
six screens standing between a developer and the thing they actually wanted to
look at, and the temptation is to leave a half-configured database lying around
and forget which half. So this script writes the same rows the wizard writes —
through `biokids_core.security`, `biokids_core.catalog` and `biokids_core.repo`,
never by hand and never with SQL — and prints what it made.

The PIN and the passphrase are fixed and printed. That is deliberate: these
credentials are for a throwaway development machine, and a random PIN nobody
writes down is how you end up re-running the seed instead of testing the lockout.
Never run this against a database a real child is using; it refuses to touch one
that already has a family unless `--force` says otherwise.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from biokids_core import repo  # noqa: E402
from biokids_core.catalog import enable_for_child, sync_catalog  # noqa: E402
from biokids_core.config import ENV_PREFIX_DIR, Config, set_config  # noqa: E402
from biokids_core.db import Child, Database, Family, Parent, utcnow  # noqa: E402
from biokids_core.db.models import (  # noqa: E402
    AccessRequest,
    AuditLog,
    CategoryLimit,
    LedgerDay,
    UsageEvent,
    UsageHour,
    UsageTick,
)
from biokids_core.security import (  # noqa: E402
    generate_recovery_code,
    hash_secret,
    normalise_recovery_code,
    validate_passphrase,
    validate_pin,
)
from biokids_core.types import AgeBand, Category, EndAction  # noqa: E402

DEFAULT_PIN: Final = "284913"
DEFAULT_PASSPHRASE: Final = "acik kapi mavi deniz"


@dataclass(frozen=True, slots=True)
class ChildSpec:
    """One child, as a parent would have entered them in the wizard."""

    unix_user: str
    display_name: str
    age: int
    """Age in years today, not a birth year. A literal 2020 here would quietly
    stop meaning "a six-year-old" next year, and the point of these two children
    is that one sits in each age band whenever the script is run."""
    band: AgeBand
    avatar: str
    weekday_windows: tuple[str, ...]
    weekend_windows: tuple[str, ...]
    break_work_min: int
    break_rest_min: int
    earn_enabled: bool
    category_limits: dict[Category, int] = field(default_factory=dict)

    def birth_year(self, today: dt.date) -> int:
        return today.year - self.age


CHILDREN: Final = (
    ChildSpec(
        unix_user="elif",
        display_name="Elif",
        age=6,
        band=AgeBand.YOUNG,
        avatar="fox",
        # An hour after school, split mornings and afternoons at the weekend.
        weekday_windows=("16:00-19:00",),
        weekend_windows=("09:00-12:00", "14:00-19:00"),
        # Shorter stretches for the younger band: half an hour, then ten minutes off.
        break_work_min=30,
        break_rest_min=10,
        earn_enabled=False,
        category_limits={Category.GAMES: 30},
    ),
    ChildSpec(
        unix_user="kerem",
        display_name="Kerem",
        age=9,
        band=AgeBand.OLDER,
        avatar="owl",
        weekday_windows=("15:30-20:00",),
        weekend_windows=("09:00-12:30", "14:00-20:30"),
        break_work_min=45,
        break_rest_min=10,
        # Old enough for earned time to mean something, and to notice if it does not.
        earn_enabled=True,
        category_limits={Category.GAMES: 45},
    ),
)


def seed(
    session: Session, *, pin: str, passphrase: str, locale: str, timezone: str
) -> list[str]:
    """Write the family, and return the lines describing what was written.

    `pin` and `passphrase` arrive already validated — the caller checks them
    before anything is deleted, so a typo cannot cost a `--force` run its data.
    """
    today = dt.date.today()
    lines: list[str] = []

    family = repo.get_or_create_family(session)
    family.locale = locale
    family.timezone = timezone
    # The panel redirects to the wizard while this is null, and the whole point
    # of seeding is to arrive somewhere past the wizard.
    family.setup_completed_at = utcnow()
    lines.append(f"family       locale {locale}, timezone {timezone}, setup marked complete")

    recovery = generate_recovery_code()
    parent = Parent(
        display_name="Anne",
        pin_hash=hash_secret(pin),
        passphrase_hash=hash_secret(passphrase),
        # Hashed after normalisation, because that is the form `verify_recovery_code`
        # compares against — a code typed with Turkish diacritics still matches.
        recovery_hash=hash_secret(normalise_recovery_code(recovery)),
    )
    session.add(parent)
    session.flush()
    lines.append(f'parent       {parent.display_name} — PIN {pin}, passphrase "{passphrase}"')
    lines.append(f"             recovery code: {recovery}")
    lines.append("             (argon2id hashes are stored; these three lines are not)")

    added, updated, gone = sync_catalog(session)
    lines.append(f"catalog      {added} apps added, {updated} updated, {gone} marked gone")

    for spec in CHILDREN:
        lines += _seed_child(session, spec, today, locale)

    repo.audit(
        session,
        actor="cli",
        action="seed",
        target=f"family:{family.id}",
        detail={"children": [spec.unix_user for spec in CHILDREN], "script": "dev/seed.py"},
    )
    session.commit()
    return lines


def _seed_child(session: Session, spec: ChildSpec, today: dt.date, locale: str) -> list[str]:
    child = Child(
        unix_user=spec.unix_user,
        display_name=spec.display_name,
        birth_year=spec.birth_year(today),
        age_band=spec.band.value,
        avatar=spec.avatar,
        # The child's own locale, not the family's by coincidence: it is what the
        # launcher renders in and what Zio and Sio speak, so `--locale en` has to
        # reach here or the seed produces a Turkish child in an English family.
        locale=locale,
    )
    session.add(child)
    session.flush()

    weekday_quota, weekend_quota = repo.default_quota_minutes(spec.band)
    policy = repo.get_or_create_policy(session, child.id)
    policy.weekday_quota_min = weekday_quota
    policy.weekend_quota_min = weekend_quota
    policy.windows = {
        "weekday": list(spec.weekday_windows),
        "weekend": list(spec.weekend_windows),
    }
    policy.end_action = EndAction.POWEROFF.value
    policy.break_enabled = True
    policy.break_work_min = spec.break_work_min
    policy.break_rest_min = spec.break_rest_min
    policy.earn_enabled = spec.earn_enabled
    policy.earn_cap_min = 30
    policy.filter_mode = repo.default_filter_mode(spec.band).value

    for category, minutes in spec.category_limits.items():
        session.add(CategoryLimit(child_id=child.id, category=category.value, minutes=minutes))

    age = child.age_on(today)
    slugs = [app.slug for app in repo.suggested_apps_for_age(session, age)]
    enable_for_child(session, child.id, slugs)
    session.flush()

    limits = ", ".join(f"{cat.value} {mins} min" for cat, mins in spec.category_limits.items())
    return [
        f"child        {spec.display_name} ({spec.unix_user}), born {child.birth_year},"
        f" age {age}, band {spec.band.value}, locale {child.locale}",
        f"             quota {weekday_quota} min weekday / {weekend_quota} min weekend,"
        f" end action {policy.end_action}",
        f"             windows {', '.join(spec.weekday_windows)} on weekdays,"
        f" {', '.join(spec.weekend_windows)} at weekends",
        f"             break {spec.break_work_min}/{spec.break_rest_min} min,"
        f" earned time {'on' if spec.earn_enabled else 'off'},"
        f" filter {policy.filter_mode}, category limits: {limits or 'none'}",
        f"             {len(slugs)} apps enabled: {', '.join(slugs)}",
    ]


USAGE_WEEKS: Final = 5
SITES: Final = ("tr.wikipedia.org", "www.trt.net.tr", "www.youtube.com", "scratch.mit.edu")


def seed_usage(session: Session, today: dt.date, *, seed: int = 206) -> list[str]:
    """Five weeks of made-up use per child, ending today (`--usage`).

    For looking at History (D-192) and Insights (D-206) on a development
    machine without waiting five weeks. Written as the daemon writes it - the
    ledger's minutes, the apps' seconds, the hours of the day, a `quota_exhausted`
    entry when a day's time ran out - plus a few visited sites and requests.
    Deterministic, so the same bars come back for the next screenshot.
    """
    rng = random.Random(seed)
    names = []
    for child in repo.list_children(session):
        names.append(child.display_name)
        policy = repo.load_policy(session, child, today=today)
        apps = [
            (link.app_slug, repo.child_app_category(link, app).value)
            for link, app in repo.list_child_apps(session, child.id)
            if app.kind != "builtin"
        ][:5]
        for back in range(USAGE_WEEKS * 7):
            day = today - dt.timedelta(days=back)
            quota = (policy.quota_minutes(day) if policy else 60) * 60
            used = 0 if rng.random() < 0.08 else int(quota * rng.uniform(0.5, 1.0))
            if back == 0:
                used //= 3  # today is under way
            ran_out = back > 0 and used > 0 and rng.random() < 0.3
            if ran_out:
                used = quota
            earned = 600 if child.age_on(today) >= 8 and used and rng.random() < 0.5 else 0
            session.add(LedgerDay(child_id=child.id, day=day, general_s=used, earned_s=earned))
            _spread_over_apps(session, rng, child.id, day, used, apps)
            _spread_over_hours(session, rng, child.id, day, used)
            if ran_out:
                session.add(
                    AuditLog(
                        actor="daemon",
                        action="quota_exhausted",
                        target=f"child:{child.id}",
                        ts=_utc(day, 18),
                        detail_json='{"reason": "quota.exhausted"}',
                    )
                )
            if back < 20 and used:
                for host in rng.sample(SITES, rng.randint(0, 2)):
                    session.add(
                        UsageEvent(
                            child_id=child.id,
                            kind="visit",
                            ts_wall=_utc(day, 17, rng.randint(0, 59)),
                            payload_json=json.dumps({"host": host}),
                        )
                    )
            if back < 14 and rng.random() < 0.25:
                session.add(
                    AccessRequest(
                        child_id=child.id,
                        url="",
                        kind="time",
                        created_at=_utc(day, 18, 30),
                        status=rng.choice(("approved", "denied", "expired")),
                        minutes=10,
                    )
                )
    session.commit()
    return [f"usage        {USAGE_WEEKS} weeks of made-up use for {', '.join(names)}"]


def _spread_over_apps(
    session: Session,
    rng: random.Random,
    child_id: int,
    day: dt.date,
    seconds: int,
    apps: list[tuple[str, str]],
) -> None:
    """Most of a day's time in two or three of the child's apps; the rest at home."""
    if not seconds or not apps:
        return
    chosen = rng.sample(apps, min(len(apps), rng.randint(1, 3)))
    weights = [rng.random() + 0.2 for _ in chosen]
    in_apps = int(seconds * 0.9)
    for (slug, category), weight in zip(chosen, weights, strict=True):
        session.add(
            UsageTick(
                child_id=child_id,
                day=day,
                category=category,
                app_slug=slug,
                seconds=int(in_apps * weight / sum(weights)),
            )
        )


def _spread_over_hours(
    session: Session, rng: random.Random, child_id: int, day: dt.date, seconds: int
) -> None:
    """After school on weekdays, mornings and afternoons at the weekend, and
    now and then a little after nine in the evening."""
    hour = rng.choice((10, 15)) if day.weekday() >= 5 else rng.choice((16, 17))
    late = 600 if seconds > 1800 and rng.random() < 0.15 else 0
    left = seconds - late
    filed: dict[int, int] = {21: late} if late else {}
    while left > 0:
        take = min(left, rng.randint(1200, 3000))
        filed[hour] = filed.get(hour, 0) + take
        left -= take
        hour = min(hour + 1, 20)
    for hour_of_day, value in filed.items():
        session.add(UsageHour(child_id=child_id, day=day, hour=hour_of_day, seconds=value))


def _utc(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    """Local wall-clock time on `day`, as the naive UTC the tables store."""
    local = dt.datetime.combine(day, dt.time(hour, minute)).astimezone()
    return local.astimezone(dt.UTC).replace(tzinfo=None)


def wipe(session: Session) -> None:
    """Remove the existing family. The catalog stays: it is data, not a family's."""
    for child in repo.list_children(session):
        session.delete(child)
    for parent in session.scalars(select(Parent)).all():
        session.delete(parent)
    for family in session.scalars(select(Family)).all():
        session.delete(family)
    session.flush()


def _prepare_stdout() -> None:
    """Turkish output must never be the reason a dev tool crashes on Windows."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Populate a development database with a family, a parent and two children."
    )
    parser.add_argument("--force", action="store_true", help="replace an existing family")
    parser.add_argument("--pin", default=DEFAULT_PIN, help="parent PIN (6 digits)")
    parser.add_argument("--passphrase", default=DEFAULT_PASSPHRASE)
    parser.add_argument("--locale", default="tr", choices=["tr", "en"])
    parser.add_argument("--timezone", default="Europe/Istanbul")
    parser.add_argument(
        "--prefix", default=None, help="scratch root; sets BIOKIDS_PREFIX for this run"
    )
    parser.add_argument(
        "--usage",
        action="store_true",
        help=f"also write {USAGE_WEEKS} weeks of made-up use, for History and Insights",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _prepare_stdout()
    args = parse_args(argv)
    if args.prefix:
        os.environ[ENV_PREFIX_DIR] = args.prefix

    try:
        # Checked before the database is opened, let alone wiped: `validate_pin`
        # raises i18n keys rather than sentences, and on a CLI the key is the
        # most useful thing to print.
        pin = validate_pin(args.pin)
        passphrase = validate_passphrase(args.passphrase)
    except ValueError as exc:
        print(f"refusing: {exc}")
        return 2

    config = Config.load()
    set_config(config)
    print("BioKids seed")
    print(f"  database     {config.paths.db_path}")
    print()

    database = Database(config)
    # Production migrates with Alembic in `postinst`; a development database is
    # usually one that does not exist yet, and a missing table here is just noise.
    database.create_all()
    try:
        with database.session() as session:
            existing = repo.get_family(session)
            if existing is not None and not args.force:
                children = (
                    ", ".join(c.display_name for c in repo.list_children(session)) or "none"
                )
                print(f"refusing: this database already has a family (children: {children}).")
                print("Pass --force to replace it, or run dev/reset.sh to start from nothing.")
                return 1
            if existing is not None:
                wipe(session)
                print("replaced     the family that was already there (--force)")

            lines = seed(
                session,
                pin=pin,
                passphrase=passphrase,
                locale=args.locale,
                timezone=args.timezone,
            )
            if args.usage:
                lines += seed_usage(session, dt.date.today())
            for line in lines:
                print(line)
    finally:
        database.dispose()

    print()
    # `pin`, not `args.pin`: the stored hash is of the validated form, and a PIN
    # printed here that does not open the panel is a twenty-minute detour.
    print(f"  Panel:  http://{config.panel.host}:{config.panel.port}  with PIN {pin}")
    print("  Apps a machine does not have installed simply will not appear on the tiles.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
