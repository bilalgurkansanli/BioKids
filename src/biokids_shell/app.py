"""The launcher's HTTP surface.

Seven screens (UX.md §2.5) and no eighth. Everything a child can reach is here,
which is the point: the set of things they can do is the set of routes in this
file, and there is no address bar to type anything else into.

Two rules run through all of it.

**Feedback within 100 ms, always** (UX.md §2.4). A tile that does nothing for two
seconds gets tapped eight more times, so a tap paints the launching screen first
and asks the daemon second.

**Never blame the child.** Every refusal renders the same calm screen with a way
to ask a grown-up. A four-year-old who meets a red error page learns that the
computer is angry at them, which is not what any of this is for.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import functools
import hashlib
import json
import logging
import math
import re
import shlex
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

from biokids_core import daily, search_filter
from biokids_core.browser_policy import packed_extension
from biokids_core.catalog import program_installed
from biokids_core.config import Config, get_config
from biokids_core.errors import IpcError
from biokids_core.i18n import Translator, normalise_locale, translator
from biokids_core.ipc import AsyncDaemonClient
from biokids_core.types import AgeBand, Reason
from biokids_shell import device
from biokids_shell.client import ShellClient
from biokids_shell.launcher import AppLauncher, LaunchRequest
from biokids_shell.relay import NOTICE_SECONDS, voice_socket_path

log = logging.getLogger(__name__)

SHELL_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = SHELL_DIR / "templates"
STATIC_DIR = SHELL_DIR / "static"
SHARED_STATIC_DIR = SHELL_DIR.parent / "biokids_core" / "data" / "static"
CHARACTER_DIR = SHELL_DIR.parent.parent / "assets" / "characters"
"""The source tree's copy. Two levels above the package is `assets/` in a
checkout and `/usr/lib/python3` on an installed machine, so this alone meant Zio
and Sio were invisible on every real install - the first-run screen introduced
"ben Zio" next to nothing, and every speech card had an empty character slot."""

INSTALLED_CHARACTER_DIR = "characters"
"""Under the share directory, beside `catalog/` and `voice/`, which is where the
packaging already puts data the code needs to find at runtime."""

TILE_ICONS: dict[str, str] = {
    "blocks": "🧩",
    "brush": "🖌️",
    "numbers": "🔢",
    "keyboard": "⌨️",
    "star": "⭐",
    "palette": "🎨",
    "penguin": "🐧",
    "bubble": "🫧",
    "tiles": "🀄",
    "document": "📝",
    "table": "📊",
    "folder": "📁",
    "school": "🏫",
    "tv": "📺",
    "cat": "🐱",
    "book": "📚",
    "encyclopedia": "📖",
    "science": "🔬",
    "ladybug": "🐞",
    "robot": "🤖",
    "video": "🎬",
    "game": "🎮",
    "earth": "🌍",
    "satellite": "🛰️",
    "potato": "🥔",
    "memory": "🧠",
    "jigsaw": "🖼️",
    "map": "🗺️",
    "turtle": "🐢",
    "fractions": "🍕",
    "chess": "♟️",
    "globe": "🌐",
    "speaker": "🔊",
    "sun": "☀️",
    "power": "⏻",
    "app": "🟦",
}
"""Emoji rather than an icon set: they render at any size, need no asset
pipeline, and a five-year-old reads them faster than a line drawing."""

AVATARS: dict[str, str] = {
    "fox": "🦊",
    "bear": "🐻",
    "cat": "🐱",
    "owl": "🦉",
    "rabbit": "🐰",
    "turtle": "🐢",
    "whale": "🐳",
    "bee": "🐝",
}
"""The same set the panel offers. A child picks theirs in the wizard and it is
the first thing they see on their own screen, so it has to be the one they chose
rather than a default square."""

BUILTIN_TILES = {"volume", "brightness", "shutdown"}
DEFAULT_VOLUME = 70
"""Where the volume slider starts when the sound's own level cannot be read."""
"""Drawn by the launcher and handled in the browser; they start no process."""
HEALTHZ_DAEMON_S = 0.5
"""How long /healthz waits to say whether the daemon answers. The extension only
needs to know this is the launcher, and it gives up on a slow answer."""


class ShellState:
    """One process-wide handle. The launcher is a single child's window."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = ShellClient(config.paths.shell_socket)
        self.launcher = AppLauncher(config)
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=ChainableUndefined,
        )
        self.env.filters["icon"] = lambda name: TILE_ICONS.get(str(name), TILE_ICONS["app"])
        self.env.filters["avatar"] = lambda slug: AVATARS.get(str(slug), AVATARS["fox"])
        self.env.filters["expression"] = set_expression
        # The setting sun on the young band's ring (D-175) is placed in the template.
        self.env.filters["cos"] = math.cos
        self.env.filters["sin"] = math.sin
        self.characters = _load_characters(config)
        self.voice = AsyncDaemonClient(voice_socket_path(config))
        self.warming: asyncio.Task[None] | None = None
        """The assistant's warm-up, held so it is not collected mid-flight."""
        self.spoken: dict[str, Any] = {}
        """The last line the voice service accepted, as it resolved it: the
        sentence actually being said and the character saying it (D-160)."""
        self.search_words: list[str] | None = None
        """The parent's own search terms as the daemon last sent them (D-202),
        kept for a moment the daemon does not answer."""

    def remember_spoken(self, card: dict[str, Any]) -> None:
        self.spoken = dict(card)

    async def say(self, text: str, locale: str) -> None:
        """Zio reads the assistant's answer aloud (D-165). Silence is not an error."""
        try:
            await self.voice.call("say", text=text, locale=locale, persona="zio")
        except (IpcError, OSError) as exc:
            log.info("the answer was not read aloud: %s", exc)


EXPRESSIONS = ("neutral", "happy", "concerned", "sleepy")


def set_expression(svg: str, expression: str) -> str:
    """Choose which of the four faces a character SVG shows.

    The SVGs carry all four and switch between them with an `is-*` class on the
    root element, so this is a class attribute edit rather than a second file per
    mood. `neutral` is the default the stylesheet already shows, so it adds
    nothing.
    """
    if not svg or expression not in EXPRESSIONS or expression == "neutral":
        return svg
    marker = 'class="bk-char'
    index = svg.find(marker)
    if index < 0:
        return svg
    insert_at = index + len(marker)
    return f"{svg[:insert_at]} is-{expression}{svg[insert_at:]}"


def character_dirs(config: Config | None = None) -> list[Path]:
    """Where to look for Zio and Sio, installed copy first.

    The same shape as the catalogue and the voice lines: `/usr/share/biokids/...`
    on a real machine, the source tree when running from a checkout.
    """
    config = config or get_config()
    return [config.paths.share_dir / INSTALLED_CHARACTER_DIR, CHARACTER_DIR]


def _load_characters(config: Config | None = None) -> dict[str, str]:
    """Inline Zio and Sio, so the card needs no network and no second request."""
    found: dict[str, str] = {}
    for persona in ("zio", "sio"):
        for directory in character_dirs(config):
            path = directory / f"{persona}.svg"
            if path.is_file():
                found[persona] = path.read_text(encoding="utf-8")
                break
    missing = [p for p in ("zio", "sio") if p not in found]
    if missing:
        # Said out loud. It used to return {} in silence, and the only symptom
        # was a child's screen with a hole where a character should be.
        log.warning(
            "character art for %s not found in %s; the speech card will have an "
            "empty character slot",
            ", ".join(missing),
            " or ".join(str(d) for d in character_dirs(config)),
        )
    return found


def get_state(request: Request) -> ShellState:
    state: ShellState = request.app.state.shell
    return state


Shell = Annotated[ShellState, Depends(get_state)]


def create_app(config: Config | None = None) -> FastAPI:
    config = config or get_config()
    app = FastAPI(title="BioKids", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.shell = ShellState(config)
    app.mount("/static/shared", StaticFiles(directory=str(SHARED_STATIC_DIR)), name="shared")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next: Any) -> Response:
        """Static files are checked with the launcher before every use.

        A Debian package stamps every file with the same old modification time,
        so a browser guessing freshness from Last-Modified decided an upgraded
        stylesheet was the one it already had and kept the old one for days:
        after an upgrade the child saw yesterday's launcher. `no-cache` still
        lets the browser keep a copy; it only has to ask first, and the answer
        from a server on the same machine is a 304 in a millisecond.
        """
        response: Response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.middleware("http")
    async def only_the_launcher(request: Request, call_next: Any) -> Response:
        """Answer the launcher's own page, and nothing a website can forge.

        The launcher acts as the child on the daemon's shell socket, so a request
        here is the child asking: to start an app, to power the machine off, to
        file a request with a parent. Any website the child opens could submit a
        form to 127.0.0.1:7879, and a page on a name that resolves to 127.0.0.1
        (DNS rebinding) is even same-origin with itself. So the Host must be a
        loopback name, and a request that changes anything must not come from
        another site.
        """
        if not _loopback_host(request.headers.get("host", "")):
            return PlainTextResponse("", status_code=421)
        if request.method not in SAFE_METHODS and not _same_origin(request):
            return PlainTextResponse("", status_code=403)
        response: Response = await call_next(request)
        return response

    register_routes(app)
    return app


def register_routes(app: FastAPI) -> None:
    """The child's whole world, in one readable list."""

    async def render(
        shell: ShellState,
        request: Request,
        template: str,
        data: dict[str, Any] | None,
        **values: Any,
    ) -> HTMLResponse:
        data = dict(data or {})
        tr = translator(normalise_locale(str(data.get("locale", "tr"))))
        band = str(data.get("age_band", AgeBand.YOUNG.value))
        context: dict[str, Any] = {
            "request": request,
            "_": tr,
            "locale": tr.locale,
            "band": band,
            "young": band == AgeBand.YOUNG.value,
            "state": data,
            "tiles": [tile for tile in data.get("tiles", []) if tile_can_start(tile)],
            "characters": shell.characters,
            "strings": _browser_strings(tr),
            "lines": _spoken_lines(tr),
            "notice_ms": int(NOTICE_SECONDS * 1000),
            # Appended to every stylesheet and script: a new file is a new URL,
            # so no browser can show a child last version's launcher.
            "asset_version": asset_fingerprint(),
        }
        context.update(values)
        return HTMLResponse(shell.env.get_template(template).render(**context))

    # --------------------------------------------------------------- home

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, shell: Shell) -> Response:
        data = await shell.client.state()
        if data is None:
            return await render(shell, request, "offline.html", None)
        if not data.get("first_run_done", True):
            return RedirectResponse("/first-run", status_code=303)

        screen = _screen_for(data)
        if screen is not None:
            return await render(shell, request, screen, data)
        volume = await device.get_volume()
        return await render(
            shell,
            request,
            "home.html",
            data,
            volume=DEFAULT_VOLUME if volume is None else volume,
            # Bugünün sürprizi (D-203): today's card, for this child's tiles.
            daily=daily.for_launcher(data, dt.date.today(), tile_can_start),
        )

    @app.get("/first-run", response_class=HTMLResponse)
    async def first_run(request: Request, shell: Shell) -> Response:
        """Non-skippable on first login (D-111). The child is told what happens."""
        data = await shell.client.state()
        return await render(shell, request, "first_run.html", data)

    @app.post("/first-run")
    async def first_run_done(shell: Shell) -> Response:
        await shell.client.first_run_done()
        return RedirectResponse("/", status_code=303)

    @app.get("/start", response_class=HTMLResponse)
    async def start_page(request: Request, shell: Shell) -> Response:
        """The browser's home page: this child's own sites, nothing else.

        Chromium's new-tab page is a wall of suggestions, which is the wrong
        first thing to put in front of a five-year-old with an allowlist.
        """
        data = await shell.client.state()
        return await render(shell, request, "start.html", data)

    # ------------------------------------------------------------- launch

    @app.get("/launch/{slug}", response_class=HTMLResponse)
    async def launching(request: Request, shell: Shell, slug: str) -> Response:
        """Paint first, ask second.

        The loading screen shows the app's own icon and name rather than a
        spinner, so the child can see that the thing they tapped is the thing
        that is happening (UX.md §2.4).
        """
        data = await shell.client.state()
        tile = _find_tile(data, slug)
        if tile is None:
            return await render(
                shell, request, "blocked.html", data, reason=Reason.APP_NOT_ALLOWED
            )
        return await render(shell, request, "launching.html", data, tile=tile)

    @app.post("/launch/{slug}")
    async def launch(request: Request, shell: Shell, slug: str) -> JSONResponse:
        data = await shell.client.state()
        tile = _find_tile(data, slug)
        if tile is None:
            return JSONResponse({"ok": False, "reason": Reason.APP_NOT_ALLOWED})

        if slug in BUILTIN_TILES:
            # Volume and brightness are the launcher's own controls; shutdown has
            # its own confirmation screen. None of them starts a process.
            return JSONResponse({"ok": True, "builtin": slug})

        allowed, reason = await shell.client.may_launch(slug)
        if not allowed:
            return JSONResponse({"ok": False, "reason": reason})

        result = await shell.launcher.launch(
            LaunchRequest(
                slug=slug,
                name=str(tile.get("name", slug)),
                kind=str(tile.get("kind", "app")),
                exec_cmd=str(tile.get("exec", "")),
                url=str(tile.get("url", "")),
                category=str(tile.get("category", "")),
                locale=normalise_locale(str((data or {}).get("locale", "tr"))),
            )
        )
        if result.ok:
            await shell.client.report_activity(
                slug, str(tile.get("name", slug)), str(tile.get("category", ""))
            )
        return JSONResponse({"ok": result.ok, "reason": result.error})

    # ------------------------------------------------------------ screens

    @app.get("/blocked", response_class=HTMLResponse)
    async def blocked(
        request: Request,
        shell: Shell,
        url: str = "",
        slug: str = "",
        reason: str = Reason.FILTER_BLOCKED,
    ) -> Response:
        data = await shell.client.state()
        kind = _request_kind_for(reason, url, slug)
        return await render(
            shell,
            request,
            "blocked.html",
            data,
            reason=reason,
            blocked_url=url,
            ask_kind=kind,
            # A refused search carries nothing to the parent (D-202).
            ask_what="" if kind == "search" else url or slug,
            in_browser=_in_browser(reason, url),
        )

    @app.post("/ask-a-parent")
    async def ask_a_parent(
        request: Request,
        shell: Shell,
        url: Annotated[str, Form()] = "",
        kind: Annotated[str, Form()] = "site",
        browser: Annotated[str, Form()] = "",
    ) -> Response:
        """The button that turns a wall into a conversation (D-65, D-173)."""
        ok, reason = await shell.client.ask_a_parent(url, kind)
        data = await shell.client.state()
        return await render(
            shell, request, "asked.html", data, ok=ok, reason=reason, in_browser=browser == "1"
        )

    @app.get("/shutdown", response_class=HTMLResponse)
    async def shutdown_confirm(request: Request, shell: Shell) -> Response:
        data = await shell.client.state()
        return await render(shell, request, "shutdown.html", data)

    @app.post("/shutdown")
    async def shutdown(request: Request, shell: Shell) -> Response:
        ok = await shell.client.request_shutdown()
        data = await shell.client.state()
        if ok:
            return await render(shell, request, "goodbye.html", data)
        return await render(shell, request, "home.html", data)

    # ---------------------------------------------------------- assistant

    @app.get("/assistant", response_class=HTMLResponse)
    async def assistant_page(request: Request, shell: Shell) -> Response:
        """Ask Zio (D-165). Only when this child's parent turned it on."""
        data = await shell.client.state()
        if data is None:
            return await render(shell, request, "offline.html", None)
        if not (data.get("assistant") or {}).get("enabled"):
            return RedirectResponse("/", status_code=303)
        return await render(shell, request, "assistant.html", data)

    @app.post("/assistant/ask")
    async def assistant_ask(request: Request, shell: Shell) -> Response:
        """A question in, the sentence to show out; Zio says it as well.

        JSON only, and only from the launcher's own page: a site open in the
        child's browser could otherwise post questions in the child's name and
        send their parent alarming alerts about things the child never asked.
        """
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse({"error": "bad request"}, status_code=400)
        question = str(body.get("question", "") if isinstance(body, dict) else "")[:600]
        data = await shell.client.state() or {}
        locale = normalise_locale(str(data.get("locale", "tr")))
        tr = translator(locale)
        reply = await shell.client.ask_assistant(question)
        refused = bool(reply.get("refused"))
        if refused:
            # Never generated: a refusal is a sentence people wrote (D-165).
            text = tr(str(reply.get("refusal_key") or "assistant.refuse.default"))
        else:
            text = str(reply.get("text", ""))
        if text:
            await shell.say(text, locale)
        return JSONResponse(
            {"text": text, "refused": refused}, headers={"Cache-Control": "no-store"}
        )

    @app.post("/speak-label")
    async def speak_label(request: Request, shell: Shell) -> Response:
        """A tile's name, or a screen's sentence, read out for a child who cannot
        read yet (UX.md section 2.2, D-175). Only from the launcher's own page."""
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse({"error": "bad request"}, status_code=400)
        text = " ".join(str(body.get("text", "") if isinstance(body, dict) else "").split())
        if text:
            data = await shell.client.state() or {}
            await shell.say(text[:300], normalise_locale(str(data.get("locale", "tr"))))
        return JSONResponse({"ok": bool(text)})

    @app.post("/assistant/warmup")
    async def assistant_warmup(request: Request, shell: Shell) -> Response:
        """The page asks this as it opens; the answer does not wait for the model."""
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        shell.warming = asyncio.create_task(shell.client.warm_assistant())
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/assistant/hear")
    async def assistant_hear(request: Request, shell: Shell) -> Response:
        """Press-and-speak (D-166): a short WAV in, its words out, nothing kept."""
        if not _from_launcher(request, "audio/wav"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        audio = await request.body()
        if not audio or len(audio) > MAX_RECORDING_BYTES:
            return JSONResponse({"error": "too long"}, status_code=413)
        text = await shell.client.hear_assistant(base64.b64encode(audio).decode("ascii"))
        del audio
        return JSONResponse(
            {"text": text or "", "ok": text is not None}, headers={"Cache-Control": "no-store"}
        )

    # ------------------------------------------------------------- events

    @app.get("/spoken")
    async def spoken(shell: Shell) -> Response:
        """The sentence and character of the line being said, for the page to show.

        The page draws the warning the moment the event arrives, with the
        catalog's sentence; this lets it replace that with the words the voice
        service actually chose, so what a child reads is what they hear.
        """
        return JSONResponse(shell.spoken, headers={"Cache-Control": "no-store"})

    @app.get("/events")
    async def events(request: Request, shell: Shell) -> Response:
        return StreamingResponse(
            _event_stream(shell, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/state")
    async def state_json(shell: Shell) -> JSONResponse:
        """What the ring and the banners read. Never computed here."""
        data = await shell.client.state()
        if data:
            # The parent's search words are the extension's, not a page's: the
            # child's browser may open this address too (D-202).
            data = {key: value for key, value in data.items() if key != "search_words"}
        return JSONResponse(data or {"offline": True})

    @app.get("/running")
    async def running(shell: Shell) -> JSONResponse:
        """The apps open in the background, for the footer's "Açık" (D-201).

        Slugs only; the page draws each from its own tile, and a tap on one
        goes through /launch like a tap on the tile, which brings it back.
        """
        return JSONResponse(
            {"running": await shell.launcher.open_apps()},
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz")
    async def healthz(shell: Shell) -> JSONResponse:
        # `biokids: launcher` is how the extension recognises its own launcher
        # among the launcher ports (D-187); nftables refuses it the others. It
        # answers at once: a daemon slow to answer (up to its five-second
        # timeout) made the extension's probe give up, and a refused search
        # stayed on Chromium's own error page.
        try:
            daemon = await asyncio.wait_for(shell.client.available(), HEALTHZ_DAEMON_S)
        except TimeoutError:
            daemon = False
        return JSONResponse({"biokids": "launcher", "ok": True, "daemon": daemon})

    @app.post("/visit")
    async def visit(request: Request, shell: Shell) -> Response:
        """A host the child opened, from the extension: the name, never the page.

        Only the extension and the launcher's own pages may post here (the
        middleware), and only a host name is accepted.
        """
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse({"ok": False}, status_code=400)
        host = str(body.get("host", "") if isinstance(body, dict) else "").strip().lower()
        if not _HOSTNAME.fullmatch(host):
            return JSONResponse({"ok": False}, status_code=400)
        await shell.client.report_visit(host)
        return Response(status_code=204)

    @app.get("/search-rules")
    async def search_rules(request: Request, shell: Shell) -> Response:
        """The patterns the extension installs to stop forbidden searches (D-202).

        The default list and the parent's own words, which the daemon sends with
        the state. Without the daemon, the words it sent last; and with none
        ever sent, the default list alone, marked incomplete so the extension
        keeps a fuller set it already has. Not a page: the launcher's address
        is open in the child's browser, and the parent's words are not for
        reading there.
        """
        if request.headers.get("sec-fetch-mode") == "navigate":
            return PlainTextResponse("", status_code=404)
        data = await shell.client.state()
        terms = data.get("search_words") if isinstance(data, dict) else None
        if isinstance(terms, list):
            shell.search_words = [str(term) for term in terms]
        known = shell.search_words
        if known is None:
            log.warning("the parent's search terms are unknown; serving the default list")
        return JSONResponse(
            search_filter.payload(known or (), complete=known is not None),
            headers={"Cache-Control": "no-store"},
        )

    # ------------------------------------------------------------- device

    @app.post("/device/volume")
    async def set_volume(value: int = 70) -> JSONResponse:
        return JSONResponse({"ok": await device.set_volume(value)})

    @app.post("/device/brightness")
    async def set_brightness(value: int = 100) -> JSONResponse:
        return JSONResponse({"ok": await device.set_brightness(value)})


# ------------------------------------------------------------------ helpers


@functools.cache
def asset_fingerprint() -> str:
    """Twelve hex digits that change whenever a file the launcher serves does."""
    digest = hashlib.sha256()
    for directory in (STATIC_DIR, SHARED_STATIC_DIR):
        for path in sorted(Path(directory).rglob("*")):
            if path.is_file():
                digest.update(path.name.encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def tile_can_start(tile: dict[str, Any]) -> bool:
    """A packaged app whose program is missing is not drawn (D-20, D-164).

    The catalog promised this from the start and the launcher never checked:
    on a machine without GCompris the tile was there and did nothing at all.
    """
    if tile.get("kind") == "web":
        return local_service_up(str(tile.get("url", "")))
    if tile.get("kind") != "app":
        return True
    argv = shlex.split(str(tile.get("exec", "")))
    return bool(argv) and program_installed(argv[0])


def local_service_up(url: str) -> bool:
    """A web tile on this machine (the offline Wikipedia, D-190) is drawn only
    while something answers there; any other web tile always is."""
    parts = urlsplit(url)
    if parts.hostname not in {"127.0.0.1", "localhost"} or parts.port is None:
        return True
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=0.2):
            return True
    except OSError:
        return False


MAX_RECORDING_BYTES = 16_000 * 2 * 16 + 1024
"""Sixteen seconds of 16 kHz mono: the button stops at fifteen."""


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(rf"(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})*")


def _extension_origin(request: Request) -> str | None:
    """`chrome-extension://<id>` of the extension packed on this machine."""
    shell: ShellState = request.app.state.shell
    found = packed_extension(shell.config.paths.extension_dir)
    return f"chrome-extension://{found[0]}" if found else None


LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})


def _loopback_host(host: str) -> bool:
    """True for a Host header naming this machine's loopback, with any port."""
    host = host.strip().lower()
    name = host.split("]", 1)[0] + "]" if host.startswith("[") else host.rsplit(":", 1)[0]
    return name in LOOPBACK_NAMES


def _same_origin(request: Request) -> bool:
    """Whether the browser says this request came from the launcher's own page.

    Chromium sends Sec-Fetch-Site and Origin with every POST it makes. A request
    with neither did not come from a browser, and the child has no other program
    that could send one.

    "Origin: null" is what Chromium sends for a form posted by a page whose
    referrer policy is no-referrer - the launcher's own pages until D-204, and
    any copy of one still open after an upgrade. Sec-Fetch-Site, which no page
    can set, still says where it came from: same-origin is the launcher, and a
    sandboxed frame on a website says cross-site.
    """
    origin = request.headers.get("origin")
    if origin is not None and origin == _extension_origin(request):
        # The BioKids extension (D-187), which is cross-site by nature.
        return True
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in {"same-origin", "none"}:
        return False
    if origin is None:
        return True
    if origin == "null":
        return site == "same-origin"
    host = request.headers.get("host", "")
    return origin in {f"http://{host}", f"https://{host}"}


def _from_launcher(request: Request, content_type: str) -> bool:
    """The launcher's own page, and nothing a web page can forge.

    A content type other than a form's makes the browser ask before sending a
    cross-site request, and the launcher never says yes; an Origin, when the
    browser sends one, must be the launcher's.
    """
    sent = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if sent != content_type:
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    host = request.headers.get("host", "")
    return origin in {f"http://{host}", f"https://{host}"}


def _in_browser(reason: str, url: str) -> bool:
    """Whether this block page is open in the child's browser, not the launcher.

    The extension sends a closed site's address or a refused search there
    (D-187, D-202); the launcher itself sends the rest. "Back" in the browser is
    the child's start page (/start): the launcher's home inside a browser tab is
    the wrong screen, and the browser's own history leads to the error page of
    the address that was just closed, which closes it again.
    """
    return bool(url) or reason == Reason.SEARCH_BLOCKED


def _request_kind_for(reason: str, url: str, slug: str) -> str:
    """What "ask a grown-up" asks for, from why the child was stopped.

    Out of minutes for the day or for a kind of app: more time. An app that is
    not on their screen: that app. A search the browser refused: a talk, which
    names nothing (D-202). Anything else: the page they wanted.
    """
    if reason in {Reason.QUOTA_EXHAUSTED, Reason.CATEGORY_EXHAUSTED}:
        return "time"
    if reason == Reason.SEARCH_BLOCKED:
        return "search"
    if reason == Reason.APP_NOT_ALLOWED and slug and not url:
        return "app"
    return "site"


def _screen_for(data: dict[str, Any]) -> str | None:
    """Which full-screen state, if any, replaces the tile grid right now."""
    if data.get("allow"):
        return None
    match str(data.get("reason_code", "")):
        case Reason.QUOTA_EXHAUSTED:
            return "time_up.html"
        case Reason.WINDOW_CLOSED | Reason.WINDOW_WAITING:
            return "window_closed.html"
        case Reason.BREAK_ACTIVE | Reason.BREAK_DUE:
            return "break.html"
        case Reason.PENALTY_ACTIVE:
            return "timeout.html"
        case _:
            return None


def _browser_strings(tr: Translator) -> dict[str, str]:
    """The few strings the page substitutes into a live event.

    Handed over as data rather than built in JavaScript, so that every
    user-facing sentence still comes from the catalogs (ARCHITECTURE.md §8) and
    a language the child cannot read never appears on their own screen.
    """
    return {
        "warning": tr("quota.warning", minutes="{minutes}"),
        "earned": tr("shell.earned.body", minutes="{minutes}"),
        "request_approved": tr("shell.request.approved"),
        "request_approved_time": tr("shell.request.approved_time", minutes="{minutes}"),
        "request_denied": tr("shell.request.denied"),
        "timeup": tr("shell.timeup.title"),
        # What a screen reader says for the ring, kept in step with the number.
        "remaining": tr("shell.remaining_label")
        + ": "
        + tr("shell.remaining_minutes", minutes="{minutes}"),
    }


def _spoken_lines(tr: Translator) -> dict[str, str]:
    """What the character card shows for each spoken event.

    The voice service picks the actual wording from its own line files, which a
    parent may have edited; the card needs something to show immediately, and
    the catalog sentence is the right fallback.
    """
    return {
        "welcome": tr("shell.greeting", name=""),
        "warn_10": tr("quota.warning", minutes=10),
        "warn_5": tr("quota.warning", minutes=5),
        "warn_1": tr("quota.warning", minutes=1),
        "time_up": tr("shell.timeup.title"),
        "blocked": tr("shell.blocked.title"),
        "earned": tr("shell.earned.title"),
        "break_start": tr("shell.break.title"),
        "request_approved": tr("shell.request.approved"),
        "request_denied": tr("shell.request.denied"),
    }


def _find_tile(data: dict[str, Any] | None, slug: str) -> dict[str, Any] | None:
    """The tile with this slug, or None.

    "Or None" is the security property, not a convenience: a slug that is not on
    this child's own list must not resolve to anything, whatever the daemon
    happens to have sent.
    """
    for tile in (data or {}).get("tiles", []):
        if isinstance(tile, dict) and tile.get("slug") == slug:
            return dict(tile)
    return None


async def _event_stream(shell: ShellState, request: Request) -> AsyncIterator[bytes]:
    while not await request.is_disconnected():
        try:
            async for event in shell.client.events():
                if await request.is_disconnected():
                    return
                payload = json.dumps(event.payload, ensure_ascii=False)
                yield f"event: {event.kind}\ndata: {payload}\n\n".encode()
        except Exception as exc:  # the daemon restarting is not an error here
            log.debug("event stream dropped: %s", exc)
            yield b'event: daemon_state\ndata: {"state":"unreachable"}\n\n'
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(2)


__all__ = ["ShellState", "create_app", "kiosk_translator"]


def kiosk_translator(locale: str) -> Translator:
    return translator(normalise_locale(locale))
