"""The application catalogue (D-20, D-163, D-164, D-203, D-207).

What a parent can tick and a child can tap. Each entry must name the Debian
package that brings it, start in a window a child can close, have a picture a
child who cannot read recognises, and be offered at sensible ages; a change to
the catalogue must never take a tile away from a child who had it; and what an
upgrade adds is remembered for a month, so the parent hears about it.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core import catalog as catalog_module
from biokids_core import repo
from biokids_core.catalog import (
    BUNDLED_CATALOG,
    NEW_APPS_SETTING,
    NEW_FOR,
    Catalog,
    CatalogEntry,
    dump_new_apps,
    enable_for_child,
    load_catalog,
    new_apps,
    new_for_child,
    read_new_apps,
    remember_new_apps,
    sync_catalog,
)
from biokids_core.db import Child
from biokids_core.db.models import AuditLog, ChildApp
from biokids_core.types import Category
from biokids_shell.app import TILE_ICONS

ROOT = Path(__file__).resolve().parent.parent
CONTROL = ROOT / "debian" / "control"

FULL_SCREEN_UNLESS_TOLD = {
    "gcompris-qt": "--window",
    "tuxpaint": "--windowed",
    "tuxmath": "--windowed",
    "supertux2": "--window",
    "frozen-bubble": "--no-fullscreen",
}
"""Programs that start full screen by default, and the flag that stops them.
Full screen hides the title bar and its close button (D-163)."""

FULL_SCREEN_FLAGS = re.compile(r"^(-f|--fullscreen|--full-screen|-fs|--kiosk)$")


@pytest.fixture(scope="module")
def entries() -> list[CatalogEntry]:
    return list(load_catalog(BUNDLED_CATALOG))


def _stanza(package: str) -> str:
    text = CONTROL.read_text(encoding="utf-8")
    for stanza in text.split("\n\n"):
        if re.search(rf"^Package: {re.escape(package)}$", stanza, flags=re.MULTILINE):
            return stanza
    raise AssertionError(f"no {package} stanza in debian/control")


def _field(stanza: str, name: str) -> set[str]:
    match = re.search(rf"^{name}:(.*?)(?=^\S|\Z)", stanza, flags=re.MULTILINE | re.DOTALL)
    if match is None:
        return set()
    names = (part.strip().split(" ")[0] for part in match.group(1).split(","))
    return {name for name in names if name and not name.startswith("$")}


# --------------------------------------------------------------- the entries


def test_every_app_names_its_package_and_a_program(entries: list[CatalogEntry]) -> None:
    for entry in entries:
        if entry.kind == "app":
            assert entry.package, f"{entry.slug}: which Debian package brings it?"
            assert entry.exec_argv, f"{entry.slug}: nothing to start"
            assert not entry.url, f"{entry.slug}: an app is not a web page"
        elif entry.kind == "web":
            assert entry.url.startswith(("https://", "http://127.0.0.1:")), entry.slug
            assert not entry.exec_argv and not entry.package, entry.slug
        else:
            assert entry.kind == "builtin", f"{entry.slug}: unknown kind {entry.kind!r}"


def test_no_app_starts_full_screen(entries: list[CatalogEntry]) -> None:
    """A child with no close button is stuck in the app (D-163)."""
    for entry in entries:
        if entry.kind != "app":
            continue
        assert not any(FULL_SCREEN_FLAGS.match(arg) for arg in entry.exec_argv), entry.slug
        program = entry.exec_argv[0]
        if program in FULL_SCREEN_UNLESS_TOLD:
            assert FULL_SCREEN_UNLESS_TOLD[program] in entry.exec_argv, (
                f"{entry.slug}: {program} starts full screen without "
                f"{FULL_SCREEN_UNLESS_TOLD[program]}"
            )


def test_ages_are_sane(entries: list[CatalogEntry]) -> None:
    for entry in entries:
        assert 4 <= entry.min_age <= entry.max_age <= 10, entry.slug


def test_every_tile_has_a_picture(entries: list[CatalogEntry]) -> None:
    """A child of four reads the picture, not the name."""
    for entry in entries:
        assert entry.icon in TILE_ICONS, f"{entry.slug}: icon {entry.icon!r} has no emoji"


def test_every_tile_is_named_and_described_in_both_languages(
    entries: list[CatalogEntry],
) -> None:
    for entry in entries:
        assert entry.name_tr.strip() and entry.name_en.strip(), entry.slug
        assert entry.description_tr.strip() and entry.description_en.strip(), entry.slug
        assert entry.default_category in {c.value for c in Category}, entry.slug


def test_no_two_tiles_share_a_name(entries: list[CatalogEntry]) -> None:
    for language in ("name_tr", "name_en"):
        names = [
            getattr(e, language)
            for e in entries
            if e.slug not in {"vikipedi", "vikipedi-cocuk"}
        ]
        assert len(names) == len(set(names)), f"two tiles look the same: {language}"


def test_every_app_package_comes_with_the_child_desktop(entries: list[CatalogEntry]) -> None:
    """biokids-desktop brings them as Recommends: never Depends, so a parent can
    drop one without fighting the package manager (D-20)."""
    desktop = _stanza("biokids-desktop")
    recommends, suggests = _field(desktop, "Recommends"), _field(desktop, "Suggests")
    depends = _field(desktop, "Depends")
    packages = {e.package for e in entries if e.kind == "app"}
    for package in packages - {"thunar"}:  # Thunar is part of xfce4 itself
        assert package in recommends | suggests, f"{package} is not offered by biokids-desktop"
        assert package not in depends, f"{package} must not be a Depends"
    assert recommends <= packages, (
        f"recommended but not in the catalogue: {recommends - packages}"
    )


# --------------------------------------------------------------- D-203


def test_dunya_is_the_globe_that_works_without_a_graphics_card(
    entries: list[CatalogEntry],
) -> None:
    """Google Earth needs WebGL, which the VM and old laptops do not have."""
    by_slug = {e.slug: e for e in entries}
    dunya = next(e for e in entries if e.name_tr == "Dünya")
    assert dunya.slug == "marble" and dunya.kind == "app"
    assert dunya.package == "marble" and dunya.exec_argv == ("marble",)
    assert dunya.suggest and dunya.default_category == Category.LEARNING.value
    assert dunya.inherits == ("google-earth",)

    earth = by_slug["google-earth"]
    assert earth.name_tr == earth.name_en == "Google Earth"
    assert earth.min_age == 8 and not earth.suggest
    assert earth.icon != dunya.icon, "two globes with one picture"


def test_the_learning_apps_are_offered_at_their_ages(entries: list[CatalogEntry]) -> None:
    added = {"marble", "ktuberling", "blinken", "palapeli", "kgeography", "kturtle", "kbruch"}
    assert added | {"gnome-chess"} <= {e.slug for e in entries}
    young = {e.slug for e in Catalog(tuple(entries)).suggested_for_age(4)}
    assert "ktuberling" in young and "kgeography" not in young
    older = {e.slug for e in Catalog(tuple(entries)).suggested_for_age(10)}
    assert {"marble", "kgeography", "kbruch"} <= older
    assert "google-earth" not in older, "Google Earth is offered, never ticked"


# ------------------------------------------------- catalogue changes (sync)


def _catalog_without(slug: str) -> Catalog:
    return Catalog(tuple(e for e in load_catalog(BUNDLED_CATALOG) if e.slug != slug))


def _links(session: Session, child_id: int) -> dict[str, ChildApp]:
    rows = session.scalars(select(ChildApp).where(ChildApp.child_id == child_id)).all()
    return {row.app_slug: row for row in rows}


def test_a_child_who_had_dunya_keeps_it_and_gets_the_globe(
    session: Session, child: Child
) -> None:
    """Before D-203 "Dünya" was Google Earth. After the upgrade the child keeps
    that tile, now named Google Earth, and has the new Dünya beside it."""
    sync_catalog(session, _catalog_without("marble"))
    enable_for_child(session, child.id, ["tuxpaint", "google-earth", "browser"])
    link = _links(session, child.id)["google-earth"]
    link.category_override = Category.GAMES.value
    session.commit()

    sync_catalog(session, load_catalog(BUNDLED_CATALOG))
    session.commit()

    links = _links(session, child.id)
    assert links["google-earth"].enabled, "nobody loses a tile to an upgrade"
    assert links["marble"].enabled
    assert links["marble"].position == links["google-earth"].position
    assert links["marble"].category_override == Category.GAMES.value
    audited = session.scalars(select(AuditLog).where(AuditLog.action == "apps_changed")).all()
    assert [entry.target for entry in audited] == [f"child:{child.id}"]


def test_the_globe_is_handed_down_once_and_never_again(session: Session, child: Child) -> None:
    """A parent who unticks the new tile is not overruled by the next sync."""
    sync_catalog(session, _catalog_without("marble"))
    enable_for_child(session, child.id, ["google-earth"])
    sync_catalog(session, load_catalog(BUNDLED_CATALOG))
    enable_for_child(session, child.id, ["google-earth"])  # the parent unticks Dünya
    session.commit()

    for _ in range(2):
        sync_catalog(session, load_catalog(BUNDLED_CATALOG))
    assert not _links(session, child.id)["marble"].enabled


def test_only_a_child_who_had_the_old_tile_gets_the_new_one(
    session: Session, child: Child
) -> None:
    sync_catalog(session, _catalog_without("marble"))
    enable_for_child(session, child.id, ["google-earth", "tuxpaint"])
    enable_for_child(session, child.id, ["tuxpaint"])  # Google Earth unticked
    session.commit()

    sync_catalog(session, load_catalog(BUNDLED_CATALOG))
    assert "marble" not in _links(session, child.id)


def test_a_fresh_install_gives_nobody_anything(session: Session, child: Child) -> None:
    sync_catalog(session, load_catalog(BUNDLED_CATALOG))
    assert _links(session, child.id) == {}


def test_a_tile_that_leaves_the_catalogue_is_kept_for_its_child(
    session: Session, child: Child
) -> None:
    sync_catalog(session, load_catalog(BUNDLED_CATALOG))
    enable_for_child(session, child.id, ["tuxpaint", "gcompris"])
    session.commit()

    sync_catalog(session, _catalog_without("gcompris"))
    links = _links(session, child.id)
    assert links["gcompris"].enabled, "the row stays, so a downgrade brings it back"
    assert (
        session.get(catalog_module.App, "gcompris").kind == catalog_module.GONE_PACKAGE_MARKER
    )


# ------------------------------------------- what an upgrade added (D-207)

D203 = frozenset(
    {
        "marble",
        "ktuberling",
        "blinken",
        "palapeli",
        "kgeography",
        "kbruch",
        "kturtle",
        "gnome-chess",
    }
)
"""The tiles D-203 added: what a family upgrading from 0.1.0-1 finds new."""

UPGRADED_AT = dt.datetime(2026, 10, 11, 9, 30, 0)


def _before_d203() -> Catalog:
    return Catalog(tuple(e for e in load_catalog(BUNDLED_CATALOG) if e.slug not in D203))


def _upgrade(session: Session) -> None:
    """The catalogue of the release before, then this one, as an upgrade runs."""
    sync_catalog(session, _before_d203(), now=UPGRADED_AT - dt.timedelta(days=60))
    sync_catalog(session, load_catalog(BUNDLED_CATALOG), now=UPGRADED_AT)


def test_an_upgrade_remembers_the_tiles_it_added_and_when(session: Session) -> None:
    _upgrade(session)
    assert new_apps(session, now=UPGRADED_AT) == dict.fromkeys(D203, UPGRADED_AT)


def test_a_fresh_install_has_nothing_new(session: Session) -> None:
    """Every tile arrives with the first sync; marking them all would mean nothing."""
    sync_catalog(session, load_catalog(BUNDLED_CATALOG), now=UPGRADED_AT)
    assert new_apps(session, now=UPGRADED_AT) == {}
    assert repo.get_setting(session, NEW_APPS_SETTING) == "", "nothing written at all"


def test_a_later_sync_keeps_when_a_tile_arrived(session: Session) -> None:
    """The panel and the wizard sync too; a tile is new from its upgrade, not
    from the parent's last save."""
    _upgrade(session)
    stored = repo.get_setting(session, NEW_APPS_SETTING)
    sync_catalog(session, load_catalog(BUNDLED_CATALOG), now=UPGRADED_AT + dt.timedelta(days=3))
    assert repo.get_setting(session, NEW_APPS_SETTING) == stored
    assert set(new_apps(session, now=UPGRADED_AT).values()) == {UPGRADED_AT}


def test_a_tile_is_new_for_thirty_days(session: Session) -> None:
    _upgrade(session)
    almost = UPGRADED_AT + NEW_FOR - dt.timedelta(minutes=1)
    assert set(new_apps(session, now=almost)) == D203
    assert new_apps(session, now=UPGRADED_AT + NEW_FOR) == {}, "dropped when read"

    sync_catalog(session, load_catalog(BUNDLED_CATALOG), now=UPGRADED_AT + NEW_FOR)
    assert repo.get_setting(session, NEW_APPS_SETTING) == "{}", "and when written"


def test_a_second_upgrade_adds_to_the_first(session: Session) -> None:
    bundled = load_catalog(BUNDLED_CATALOG)
    oldest = Catalog(tuple(e for e in bundled if e.slug not in {"marble", "kbruch"}))
    sync_catalog(session, oldest, now=UPGRADED_AT - dt.timedelta(days=60))
    first = Catalog(tuple(e for e in bundled if e.slug != "kbruch"))
    sync_catalog(session, first, now=UPGRADED_AT)
    later = UPGRADED_AT + dt.timedelta(days=20)
    sync_catalog(session, load_catalog(BUNDLED_CATALOG), now=later)

    remembered = new_apps(session, now=later)
    assert remembered == {"marble": UPGRADED_AT, "kbruch": later}
    assert new_apps(session, now=UPGRADED_AT + NEW_FOR) == {"kbruch": later}


def test_a_stored_value_that_cannot_be_read_is_skipped() -> None:
    """One word on a panel page; a hand-edited row must not stop the page."""
    now = UPGRADED_AT
    assert read_new_apps("", now) == {}
    assert read_new_apps("not json", now) == {}
    assert read_new_apps('["marble"]', now) == {}
    raw = json.dumps(
        {
            "marble": "2026-10-10T08:00:00",
            "blinken": "yesterday",
            "kbruch": 5,
            "kturtle": None,
            "palapeli": "2026-10-10T08:00:00+03:00",
            "ktuberling": "2026-10-09",
        }
    )
    assert read_new_apps(raw, now) == {
        "marble": dt.datetime(2026, 10, 10, 8, 0, 0),
        "palapeli": dt.datetime(2026, 10, 10, 5, 0, 0),
        "ktuberling": dt.datetime(2026, 10, 9, 0, 0, 0),
    }


def test_a_time_from_a_clock_that_was_wrong_is_brought_back_to_now() -> None:
    """Otherwise a clock set to 2031 during an upgrade keeps "Yeni" for years."""
    raw = dump_new_apps({"marble": dt.datetime(2031, 1, 1)})
    assert read_new_apps(raw, UPGRADED_AT) == {"marble": dt.datetime(2031, 1, 1)}
    assert remember_new_apps(raw, [], UPGRADED_AT) == {"marble": UPGRADED_AT}
    assert remember_new_apps(raw, ["kbruch"], UPGRADED_AT) == {
        "marble": UPGRADED_AT,
        "kbruch": UPGRADED_AT,
    }


def test_the_new_tiles_worth_a_line_suit_the_child_and_are_not_ticked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = load_catalog(BUNDLED_CATALOG)
    new = dict.fromkeys(D203, UPGRADED_AT)

    def slugs(**kwargs: Any) -> list[str]:
        return [entry.slug for entry in new_for_child(catalog, new, **kwargs)]

    # Five: Patates Kafa (4-6) and Hafıza (5-7), in catalogue order; Dünya is
    # from six. Ten: never Yapboz, Kaplumbağa or Satranç, which are offered
    # and never ticked for a parent (`suggest = false`).
    assert slugs(age=5, enabled=set()) == ["ktuberling", "blinken"]
    assert slugs(age=10, enabled=set()) == ["marble", "kgeography", "kbruch"]
    assert slugs(age=5, enabled={"blinken", "tuxpaint"}) == ["ktuberling"]

    before = UPGRADED_AT - dt.timedelta(days=1)
    after = UPGRADED_AT + dt.timedelta(minutes=5)
    assert slugs(age=5, enabled=set(), answered_at=before) == ["ktuberling", "blinken"]
    assert slugs(age=5, enabled=set(), answered_at=after) == [], "the parent has answered"

    monkeypatch.setattr(catalog_module, "program_installed", lambda p: p != "ktuberling")
    assert slugs(age=5, enabled=set()) == ["blinken"], "a box that cannot be ticked"


def test_only_a_parents_save_answers_the_line(session: Session, child: Child) -> None:
    assert repo.apps_saved_at(session, child.id) is None
    repo.audit(session, actor="system", action="apps_changed", target=f"child:{child.id}")
    assert repo.apps_saved_at(session, child.id) is None, "the hand-down is not a parent"

    repo.audit(session, actor="parent:1", action="apps_changed", target="child:99")
    row = repo.audit(
        session, actor="parent:1", action="apps_changed", target=f"child:{child.id}"
    )
    assert repo.apps_saved_at(session, child.id) == row.ts
