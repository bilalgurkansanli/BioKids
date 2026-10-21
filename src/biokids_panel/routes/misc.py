"""What is not a screen: the health check, the service worker, the manifest."""

from __future__ import annotations

import functools
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from biokids_panel.common import Ctx, Db, View
from biokids_panel.context import (
    SHARED_STATIC_DIR,
    STATIC_DIR,
    request_locale,
    request_translator,
)

SW_CACHE_NAME = '"biokids-shell-v1"'
"""The placeholder in sw.js that `service_worker` replaces."""

router = APIRouter()


@router.get("/healthz")
def healthz(ctx: Ctx) -> JSONResponse:
    """Liveness only. Deliberately says nothing about a family."""
    return JSONResponse({"ok": True, "daemon": ctx.client.available()})


# ----------------------------------------------------------------------- PWA


@router.get("/sw.js")
def service_worker() -> Response:
    """Served from the root, not from /static.

    A service worker can only control paths at or below its own URL, so one
    at `/static/sw.js` would never see a navigation to `/today`.
    """
    # The cache name carries a fingerprint of the files it caches. With a
    # fixed name a phone kept the first stylesheet it ever saw, for good:
    # new screens arrived with no styles after every upgrade.
    script = (STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    script = script.replace(SW_CACHE_NAME, f'"biokids-shell-{static_fingerprint()}"')
    return Response(
        script,
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


HELP_TOPICS = ("time", "locked", "phone", "site", "login", "pin", "off", "remove")
"""The help page's questions, in the order a parent meets them (D-195)."""

HELP_COMMANDS = {
    "pin": "sudo biokids-admin reset-pin",
    "remove": "sudo apt remove biokids-core",
}


@dataclass(frozen=True, slots=True)
class Guide:
    """One step-by-step guide (D-224). Its words are catalogue entries:
    `guide.<slug>.title`, `.intro`, `.step.<n>`, `.tip.<n>.q` and `.a`, `.open`."""

    slug: str
    icon: str
    steps: int
    tips: int
    page: str = ""
    """Where in the panel it is done, for a button once signed in."""
    commands: Mapping[int, str] = field(default_factory=dict)
    """A command to type, under the step it belongs to. Never translated."""


GUIDES: Final = (
    Guide("email", "✉️", 6, 3, "/settings#mail"),
    Guide("phone", "📱", 5, 3, "/settings/phone"),
    Guide("child", "🧒", 5, 3, "/children"),
    Guide("time", "⏰", 6, 3, "/children"),
    Guide("internet", "🌐", 5, 3, "/children"),
    Guide("voice", "🗣️", 4, 2, "/voices"),
    Guide(
        "forgot",
        "🔑",
        5,
        2,
        commands={3: "sudo biokids-admin reset-pin", 5: "sudo biokids-admin reset-passphrase"},
    ),
)
"""The guides, in the order a family meets what they explain. The FAQ (D-195)
answers in a sentence; a guide is for the parent who needs every step, which
for mail means another site's settings as well as ours."""

MAIL_PROVIDERS: Final = (
    ("Gmail", "smtp.gmail.com", 587, "starttls"),
    ("Yandex", "smtp.yandex.com", 465, "ssl"),
    ("iCloud", "smtp.mail.me.com", 587, "starttls"),
)
"""Mail services whose own settings still let a program send with a user name
and an app password. Not Outlook.com or Hotmail: Microsoft turned that off for
personal accounts, and the guide says so (D-224)."""


@router.get("/help", response_class=HTMLResponse)
def help_page(view: View) -> Response:
    """Open without signing in: "I forgot my PIN" is one of the questions."""
    return view.render("help.html", topics=HELP_TOPICS, commands=HELP_COMMANDS, guides=GUIDES)


@router.get("/guides/{slug}", response_class=HTMLResponse)
def guide_page(view: View, slug: str) -> Response:
    """One guide, step by step (D-224). Open without signing in, like /help:
    the one about a forgotten PIN is read by someone who cannot sign in."""
    guide = next((g for g in GUIDES if g.slug == slug), None)
    if guide is None:
        return view.not_found()
    return view.render("guide.html", guide=guide, providers=MAIL_PROVIDERS)


@router.get("/offline", response_class=HTMLResponse)
def offline(ctx: Ctx, session: Db, request: Request) -> HTMLResponse:
    """The page the service worker keeps for when the panel cannot be reached.

    Rendered without the session on purpose: it is cached on the phone, so it
    carries no name, no token and no navigation (D-194).
    """
    tr = request_translator(request, session)
    html = ctx.env.get_template("offline.html").render(_=tr, locale=tr.locale)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/manifest.webmanifest")
def manifest(session: Db, request: Request) -> JSONResponse:
    tr = request_translator(request, session)
    return JSONResponse(
        {
            "name": "BioKids",
            "short_name": "BioKids",
            "description": tr("panel.subtitle"),
            "start_url": "/today",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#FBF7F2",
            "theme_color": "#4B54C8",
            "lang": request_locale(request, session),
            "icons": [
                {
                    "src": "/static/icons/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
                {
                    "src": "/static/icons/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        },
        media_type="application/manifest+json",
    )


@functools.cache
def static_fingerprint() -> str:
    """Twelve hex digits that change whenever any file the panel serves does."""
    digest = hashlib.sha256()
    for directory in (STATIC_DIR, SHARED_STATIC_DIR):
        for path in sorted(Path(directory).rglob("*")):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
