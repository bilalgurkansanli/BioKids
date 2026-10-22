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
from biokids_core.appearance import Appearance
from biokids_core.config import Config, get_config
from biokids_core.db.session import Database
from biokids_core.i18n import (
    DEFAULT_LOCALE,
    Translator,
    duration_text,
    normalise_locale,
    translator,
)
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
    """Seconds to whole minutes, rounded up. The UI never shows seconds (UX §3.3).

    Takes `object` because a template hands it whatever the daemon sent, which
    on a bad day is a string or nothing at all. A template filter that raises
    takes the whole page with it.
    """
    if isinstance(seconds, bool) or not isinstance(seconds, int | float | str):
        return 0
    try:
        value = int(float(seconds))
    except (TypeError, ValueError):
        return 0
    return -(-max(0, value) // 60)


def filter_duration(seconds: object, tr: Translator | None = None) -> str:
    """`5700` -> "1 sa 35 dk". Hours and minutes only, never a timestamp.

    The words come from the catalogue (`duration.*`); without a translator,
    Turkish, the product's primary voice.
    """
    return duration_text(filter_minutes(seconds) * 60, tr or translator(DEFAULT_LOCALE))


def filter_when(value: object) -> str:
    """A time a parent can act on: no ISO strings, no seconds (UX.md §3.3)."""
    if not isinstance(value, dt.datetime):
        return ""
    local = value.replace(tzinfo=dt.UTC).astimezone()
    today = dt.datetime.now().date()
    if local.date() == today:
        return local.strftime("%H:%M")
    if local.date() == today - dt.timedelta(days=1):
        # Yesterday returned a bare time, identical to today's. The table shows
        # fifty rows, which routinely spans two days, so a parent looking for
        # "when did the computer shut itself off" read "23:40 - Günlük süre
        # doldu" and could not tell which night it was. Only rows two days old
        # carried a date at all.
        return f"{local:%d.%m %H:%M}"
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
        pending = len(repo.access_requests(session, decided_limit=0)[0])
    return {
        "request": request,
        "_": tr,
        "locale": tr.locale,
        "auth": state,
        # The signed-in parent's text size and contrast (D-217), as attributes
        # on <html>. Before signing in nobody is known, so the defaults.
        "appearance": (
            Appearance.of(state.parent.text_size, state.parent.high_contrast)
            if state.authenticated and state.parent is not None
            else Appearance()
        ),
        "csrf": csrf_token(state),
        "path": request.url.path,
        "pending_requests": pending,
        "avatars": AVATARS,
        "version": _version(),
        "flash": flash_for(request, tr),
        "at_the_machine": at_the_machine(request),
    }


#: `?error=<slug>` to the sentence a parent reads. A slug containing a dot is
#: taken to be a catalogue key already - that is how a ValueError from
#: `validate_pin` reaches the screen - and anything unrecognised falls back to a
#: generic failure rather than being dropped, because dropping it is the bug
#: this table exists to fix.
FORWARDED_HEADERS = ("x-forwarded-for", "forwarded", "tailscale-user-login")
"""What `tailscale serve` adds to a request it brings from the parent's phone."""


def at_the_machine(request: Request) -> bool:
    """Whether the panel was opened on this computer's own screen, not from a phone.

    "Oturumu değiştir" (D-227) shows the login screen of the computer itself,
    which from a phone would be a screen nobody is looking at. A browser on the
    machine asks 127.0.0.1 directly; Tailscale's proxy says whom it carries the
    request for. Only for a button: nothing that matters for security rests on it.
    """
    if any(name in request.headers for name in FORWARDED_HEADERS):
        return False
    host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
    return host in {"127.0.0.1", "localhost", "::1"}


ERROR_KEYS: dict[str, str] = {
    "csrf": "error.stale_form",
    "daemon": "error.daemon_unreachable",
    "pin": "auth.pin.wrong",
    "passphrase": "auth.passphrase.wrong",
    "remote": "remote.failed",
    "remote_approval": "remote.needs_approval",
    "provision": "child.provision_failed",
}


def flash_for(request: Request, tr: Translator) -> dict[str, str] | None:
    """The outcome of the thing the parent just did, for every page at once.

    Assembled here rather than in each template because the bug was templates
    that did not know about it: five redirect targets carried an `?error=` that
    nothing rendered, so a rejected passphrase change and a successful one
    produced the same page.
    """
    query = request.query_params

    failure = query.get("error", "")
    if failure:
        key = ERROR_KEYS.get(failure, failure if "." in failure else "error.unknown")
        return {"tone": "danger", "text": tr(key, remaining="", minutes="")}

    saved = query.get("saved", "")
    if saved == "mail_sent":
        return {"tone": "ok", "text": tr("mail.panel.sent")}
    if saved == "removed":
        return {"tone": "ok", "text": tr("child.removed")}
    if saved == "parent_added":
        return {"tone": "ok", "text": tr("parents.added")}
    if saved == "parent_removed":
        return {"tone": "ok", "text": tr("parents.removed")}
    if saved == "download":
        # Started, not finished: it takes minutes, and the status card says when.
        return {"tone": "ok", "text": tr("assistant.panel.download.started")}
    if saved:
        return {"tone": "ok", "text": tr("common.saved")}
    return None


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
