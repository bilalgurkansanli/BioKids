"""The application catalogue (D-20, D-163, D-164, D-203).

What a parent can tick and a child can tap. Each entry must name the Debian
package that brings it, start in a window a child can close, have a picture a
child who cannot read recognises, and be offered at sensible ages; and a change
to the catalogue must never take a tile away from a child who had it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core import catalog as catalog_module
from biokids_core.catalog import (
    BUNDLED_CATALOG,
    Catalog,
    CatalogEntry,
    enable_for_child,
    load_catalog,
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
