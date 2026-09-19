"""Per-process and per-request context.

One `PanelContext` is built at startup and holds the things that are expensive
or global: the config, the database handle, the daemon client and the Jinja
environment. Everything per-request — the session, the translator, the
authenticated parent — hangs off the request instead, so two browser tabs in two
languages do not fight over a shared translator.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import Request
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from biokids_core import repo
from biokids_core.config import Config, get_config
from biokids_core.db.session import Database
from biokids_core.i18n import Translator, normalise_locale, translator
from biokids_core.ipc import DaemonClient
from biokids_panel.auth import AuthState, csrf_token

log = logging.getLogger(__name__)

PANEL_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PANEL_DIR / "templates"
STATIC_DIR = PANEL_DIR / "static"
SHARED_STATIC_DIR = Path(__file__).resolve().parent.parent / "biokids_core" / "data" / "static"


class LoudUndefined(ChainableUndefined):
    """Renders as empty, but says so in the log.

    A silently missing variable in a parental-control panel is how a parent ends
    up staring at a blank number and assuming everything is fine. Chainable so
    that `child.policy.quota` on a child with no policy renders blank rather
    than raising in the middle of a page.
    """

    def _fail_with_undefined_error(self, *args: object, **kwargs: object) -> Any:
        log.warning("a template used an undefined value: %s", self._undefined_name)
        return super()._fail_with_undefined_error(*args, **kwargs)


AVATARS: tuple[tuple[str, str], ...] = (
    ("fox", "🦊"),
    ("bear", "🐻"),
    ("cat", "🐱"),
    ("owl", "🦉"),
    ("rabbit", "🐰"),
    ("turtle", "🐢"),
    ("whale", "🐳"),
    ("bee", "🐝"),
)
"""Avatars are emoji rather than images: they render at any size, need no
asset pipeline, and a six-year-old picks one instantly."""


@dataclass
class PanelContext:
    config: Config
    database: Database
    client: DaemonClient
    env: Environment

    @classmethod
    def build(cls, config: Config | None = None) -> PanelContext:
        config = config or get_config()
        database = Database(config)
        env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=LoudUndefined,
        )
        env.filters["minutes"] = filter_minutes
        env.filters["duration"] = filter_duration
        env.filters["when"] = filter_when
        env.filters["avatar"] = filter_avatar
        return cls(
            config=config,
            database=database,
            client=DaemonClient(config.paths.daemon_socket),
            env=env,
        )

    def session(self) -> Iterator[Session]:
        with self.database.session() as session:
            yield session

    def dispose(self) -> None:
        self.database.dispose()


# ------------------------------------------------------------------- filters


def filter_minutes(seconds: object) -> int:
    """Seconds to whole minutes, rounded up. The UI never shows seconds (UX §3.3)."""
    try:
        value = int(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return -(-max(0, value) // 60)


def filter_duration(seconds: object, tr: Translator | None = None) -> str:
    """`95` -> "1 sa 35 dk". Hours and minutes only, never a timestamp."""
    minutes = filter_minutes(seconds)
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} sa {rest} dk" if _is_tr(tr) else f"{hours} h {rest} min"
    if hours:
        return f"{hours} sa" if _is_tr(tr) else f"{hours} h"
    return f"{rest} dk" if _is_tr(tr) else f"{rest} min"


def _is_tr(tr: Translator | None) -> bool:
    return tr is None or tr.locale.startswith("tr")


def filter_when(value: object) -> str:
    """A time a parent can act on: no ISO strings, no seconds (UX.md §3.3)."""
    if not isinstance(value, dt.datetime):
        return ""
    local = value.replace(tzinfo=dt.UTC).astimezone()
    today = dt.datetime.now().date()
    if local.date() == today:
        return local.strftime("%H:%M")
    if local.date() == today - dt.timedelta(days=1):
        return f"{local:%H:%M}"
    return local.strftime("%d.%m %H:%M")


def filter_avatar(slug: object) -> str:
    lookup = dict(AVATARS)
    return lookup.get(str(slug), "🦊")


# -------------------------------------------------------------- render helper


def base_context(
    request: Request,
    session: Session,
    state: AuthState,
    tr: Translator,
) -> dict[str, Any]:
    """What every template needs, assembled once."""
    pending = 0
    if state.authenticated:
        from biokids_core.db.models import AccessRequest

        pending = session.query(AccessRequest).filter(AccessRequest.status == "pending").count()
    return {
        "request": request,
        "_": tr,
        "locale": tr.locale,
        "auth": state,
        "csrf": csrf_token(state),
        "path": request.url.path,
        "pending_requests": pending,
        "avatars": AVATARS,
        "version": _version(),
    }


def request_locale(request: Request, session: Session) -> str:
    """Per-session for the panel: a query parameter wins, then the family's."""
    chosen = request.query_params.get("lang") or request.cookies.get("biokids_lang")
    if chosen:
        return normalise_locale(chosen)
    family = repo.get_family(session)
    return normalise_locale(family.locale if family else None)


def request_translator(request: Request, session: Session) -> Translator:
    return translator(request_locale(request, session))


def _version() -> str:
    from biokids_core import __version__

    return __version__
