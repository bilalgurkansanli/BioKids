"""Loading the application catalog.

The catalog is data (D-20), shipped as TOML in `/usr/share/biokids/catalog/` and
synchronised into the `app` table on install and upgrade. Keeping it in the
database as well as on disk is not redundancy for its own sake: the panel joins
against it, and a parent's per-child overrides have to point at something with a
foreign key.

Synchronisation is additive. An app that disappears from the file is marked
gone, never deleted, because a `child_app` row may point at it and a family's
arrangement of tiles should survive an upgrade that drops a package. A slug is
never reused for something else: a tile that changes what it is gets a new
slug, and `inherits` hands it to the children who had the old one (D-203).
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from biokids_core import repo
from biokids_core.config import Config, get_config
from biokids_core.db.models import App, ChildApp
from biokids_core.errors import ConfigError
from biokids_core.types import Category

BUNDLED_CATALOG = Path(__file__).resolve().parent / "data" / "catalog" / "catalog.toml"


GAMES_DIRS = ("/usr/games", "/usr/local/games")
"""Where Debian puts games. On a user's PATH, and on no systemd service's: the
panel asked `which tuxmath` with the service PATH and was told it was missing."""


def resolve_program(program: str) -> str | None:
    """The full path of `program`, looked for on PATH and in Debian's games
    directories, or None. The launcher runs what this returns: a user service's
    PATH has no /usr/games, so `gcompris-qt` by name would not start."""
    search = os.pathsep.join([os.environ.get("PATH", os.defpath), *GAMES_DIRS])
    return shutil.which(program, path=search)


def program_installed(program: str) -> bool:
    """Is `program` on PATH or in Debian's games directories?"""
    return resolve_program(program) is not None


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    slug: str
    name_tr: str
    name_en: str
    exec_argv: tuple[str, ...] = ()
    url: str = ""
    package: str = ""
    kind: str = "app"
    icon: str = "app"
    default_category: str = Category.FREE.value
    min_age: int = 4
    max_age: int = 10
    description_tr: str = ""
    description_en: str = ""
    suggest: bool = True
    """False keeps the tile out of the age-based pre-selection: there for a
    parent to tick, never ticked for them (YouTube Kids, an English-only site)."""
    allow: tuple[str, ...] = ()
    """What a web tile may open in "selected sites only" mode, as Chromium URL
    filters. Empty means the host of `url`, which is right for most sites."""
    inherits: tuple[str, ...] = ()
    """Tiles this one takes over from (D-203). When it first enters the `app`
    table, every child with one of these enabled gets it too, beside it: the
    tile called "Dünya" became Marble, and a child whose parent ticked "Dünya"
    should still have a Dünya that works."""

    def allowed_patterns(self) -> tuple[str, ...]:
        if self.allow:
            return self.allow
        host = (urlsplit(self.url).hostname or "").removeprefix("www.")
        return (host,) if host else ()

    @property
    def exec_cmd(self) -> str:
        """argv joined for storage. The shell splits it again before spawning."""
        return " ".join(self.exec_argv)

    @property
    def is_builtin(self) -> bool:
        return self.kind == "builtin"

    @property
    def is_web(self) -> bool:
        return self.kind == "web"

    def available(self) -> bool:
        """True when this tile can actually start on this machine.

        Built-in and web tiles are always available. A packaged app is available
        only when its executable is on PATH — showing a child a tile that does
        nothing is worse than showing them one tile fewer.
        """
        if self.is_builtin or self.is_web:
            return True
        return bool(self.exec_argv) and program_installed(self.exec_argv[0])


@dataclass(frozen=True, slots=True)
class Catalog:
    entries: tuple[CatalogEntry, ...] = field(default_factory=tuple)

    def __iter__(self) -> Any:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def get(self, slug: str) -> CatalogEntry | None:
        return next((e for e in self.entries if e.slug == slug), None)

    def for_age(self, age: int, *, installed_only: bool = False) -> list[CatalogEntry]:
        found = [e for e in self.entries if e.min_age <= age <= e.max_age]
        return [e for e in found if e.available()] if installed_only else found

    def suggested_for_age(
        self, age: int, *, installed_only: bool = False
    ) -> list[CatalogEntry]:
        """What a child of this age starts with unless a parent chooses otherwise."""
        return [e for e in self.for_age(age, installed_only=installed_only) if e.suggest]


def load_catalog(path: Path | None = None, config: Config | None = None) -> Catalog:
    """Read the catalog from disk, falling back to the copy inside the package."""
    candidates = []
    if path is not None:
        candidates.append(path)
    else:
        config = config or get_config()
        candidates.append(config.paths.catalog_dir / "catalog.toml")
    candidates.append(BUNDLED_CATALOG)

    for candidate in candidates:
        if candidate.is_file():
            return _parse(candidate)
    raise ConfigError(f"no application catalog found; looked in {candidates}")


def _parse(path: Path) -> Catalog:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read: {exc}") from exc

    apps = raw.get("app", [])
    if not isinstance(apps, list):
        raise ConfigError(f"{path}: [[app]] must be a list of tables")

    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(apps):
        if not isinstance(item, dict):
            raise ConfigError(f"{path}: entry {index} is not a table")
        slug = str(item.get("slug", "")).strip()
        if not slug:
            raise ConfigError(f"{path}: entry {index} has no slug")
        if slug in seen:
            raise ConfigError(f"{path}: duplicate slug {slug!r}")
        seen.add(slug)
        entries.append(
            CatalogEntry(
                slug=slug,
                name_tr=str(item.get("name_tr", slug)),
                name_en=str(item.get("name_en", slug)),
                exec_argv=tuple(str(x) for x in item.get("exec", []) or []),
                url=str(item.get("url", "")),
                package=str(item.get("package", "")),
                kind=str(item.get("kind", "app")),
                icon=str(item.get("icon", "app")),
                default_category=_category(item.get("default_category")),
                min_age=int(item.get("min_age", 4)),
                max_age=int(item.get("max_age", 10)),
                description_tr=str(item.get("description_tr", "")),
                description_en=str(item.get("description_en", "")),
                suggest=bool(item.get("suggest", True)),
                allow=tuple(str(x) for x in item.get("allow", []) or []),
                inherits=tuple(str(x) for x in item.get("inherits", []) or []),
            )
        )
    return Catalog(entries=tuple(entries))


def _category(value: Any) -> str:
    try:
        return Category(str(value)).value
    except ValueError:
        return Category.FREE.value


GONE_PACKAGE_MARKER = "__gone__"
"""Written into `app.kind` when an app leaves the catalog, so `child_app` rows
keep their foreign key and a downgrade can bring the app back."""


def sync_catalog(session: Session, catalog: Catalog | None = None) -> tuple[int, int, int]:
    """Bring the `app` table in line with the file. Returns (added, updated, gone)."""
    catalog = catalog or load_catalog()
    existing = {row.slug: row for row in session.scalars(select(App)).all()}
    added = updated = 0
    arrived: list[CatalogEntry] = []

    for entry in catalog.entries:
        row = existing.pop(entry.slug, None)
        if row is None:
            arrived.append(entry)
            session.add(
                App(
                    slug=entry.slug,
                    name_tr=entry.name_tr,
                    name_en=entry.name_en,
                    exec_cmd=entry.exec_cmd,
                    url=entry.url,
                    package=entry.package,
                    kind=entry.kind,
                    icon=entry.icon,
                    default_category=entry.default_category,
                    min_age=entry.min_age,
                    max_age=entry.max_age,
                    description_tr=entry.description_tr,
                    description_en=entry.description_en,
                )
            )
            added += 1
            continue
        row.name_tr = entry.name_tr
        row.name_en = entry.name_en
        row.exec_cmd = entry.exec_cmd
        row.url = entry.url
        row.package = entry.package
        row.kind = entry.kind
        row.icon = entry.icon
        row.default_category = entry.default_category
        row.min_age = entry.min_age
        row.max_age = entry.max_age
        row.description_tr = entry.description_tr
        row.description_en = entry.description_en
        updated += 1

    gone = 0
    for row in existing.values():
        if row.kind != GONE_PACKAGE_MARKER:
            row.kind = GONE_PACKAGE_MARKER
            gone += 1

    session.flush()
    for entry in arrived:
        if entry.inherits:
            hand_down(session, entry)
    return added, updated, gone


def hand_down(session: Session, entry: CatalogEntry) -> int:
    """Give a new tile to every child who has one it takes over from (D-203).

    Only ever from `sync_catalog`, when the tile's row is first added - once
    per database - so a parent who unticks it later is never overruled. It
    takes the old tile's place and category, right after it; the old tile
    stays, because nobody loses a tile to an upgrade. Returns how many
    children were given it.
    """
    links = session.scalars(
        select(ChildApp)
        .where(ChildApp.app_slug.in_(entry.inherits), ChildApp.enabled.is_(True))
        .order_by(ChildApp.child_id, ChildApp.position)
    ).all()
    given: set[int] = set()
    for link in links:
        if link.child_id in given:
            continue
        if session.get(ChildApp, (link.child_id, entry.slug)) is not None:
            continue
        session.add(
            ChildApp(
                child_id=link.child_id,
                app_slug=entry.slug,
                enabled=True,
                position=link.position,
                category_override=link.category_override,
            )
        )
        # In the parent's audit log like any change to a child's tiles, so a
        # tile that appeared after an upgrade can be traced to why.
        repo.audit(
            session,
            actor="system",
            action="apps_changed",
            target=f"child:{link.child_id}",
            detail={"added": [entry.slug], "removed": [], "inherits": list(entry.inherits)},
        )
        given.add(link.child_id)
    session.flush()
    return len(given)


ALWAYS_ON_TILES: tuple[str, ...] = ("volume", "brightness", "shutdown")
"""Built-in tiles every child keeps whatever the apps screen says (D-164). The
shutdown tile is how a child ends their own session properly."""


def enable_for_child(
    session: Session, child_id: int, slugs: list[str], *, replace: bool = True
) -> None:
    """Set exactly which tiles a child sees, in the given order.

    `replace=True` disables anything not named rather than deleting the row, so
    a parent who unticks an app and ticks it again tomorrow gets their category
    override back.
    """
    wanted = {slug: position for position, slug in enumerate(slugs)}
    rows = session.scalars(select(ChildApp).where(ChildApp.child_id == child_id)).all()
    known = {row.app_slug: row for row in rows}

    for slug, position in wanted.items():
        row = known.get(slug)
        if row is None:
            session.add(
                ChildApp(child_id=child_id, app_slug=slug, enabled=True, position=position)
            )
        else:
            row.enabled = True
            row.position = position

    if replace:
        for slug, row in known.items():
            if slug not in wanted:
                row.enabled = False
    session.flush()
