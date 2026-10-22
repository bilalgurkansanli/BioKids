"""The launcher's HTTP surface.

The screens of UX.md §2.5 and no others. Everything a child can reach is here,
which is the point: the set of things they can do is the set of routes in this
file, and there is no address bar to type anything else into.

Two rules run through all of it.

**Feedback within 100 ms, always** (UX.md §2.4). A tile that does nothing for two
seconds gets tapped eight more times, so a tap shows its tile opening first and
asks the daemon second (D-208); without JavaScript, the launching screen does.

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
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import urlencode, urlsplit

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

from biokids_core import album, daily, english, search_filter
from biokids_core.appearance import Appearance
from biokids_core.browser_policy import packed_extension
from biokids_core.catalog import program_installed
from biokids_core.config import Config, get_config
from biokids_core.errors import IpcError
from biokids_core.i18n import Translator, normalise_locale, translator
from biokids_core.ipc import AsyncDaemonClient
from biokids_core.ipc import Event as IpcEvent
from biokids_core.ipc import Request as IpcRequest
from biokids_core.ipc import Response as IpcResponse
from biokids_core.types import AgeBand, Reason
from biokids_shell import device, pictures
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
    "diamond": "💎",
    "piano": "🎹",
    "globe": "🌐",
    "speaker": "🔊",
    "sun": "☀️",
    "power": "⏻",
    "gallery": "🏞️",
    "abc": "🔤",
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

BUILTIN_TILES = {"volume", "brightness", "shutdown", "drawings", english.SLUG}
"""Drawn by the launcher and handled in the browser; they start no process."""
PAGE_TILES: dict[str, str] = {"drawings": "/pictures", english.SLUG: "/english"}
"""Built-in tiles that are a page of the launcher's own: Resimlerim (D-212) and
the English corner (D-225)."""
DEFAULT_VOLUME = 70
"""Where the volume slider starts when the sound's own level cannot be read."""
CHEER_FILE = Path(".config") / "biokids" / "album-cheered"
"""Under the home, beside the kiosk's and the browser's profiles: the date the
album last celebrated a new sticker (D-211), and nothing else."""
PAGE_TILE_TTL_S = 10.0
"""How long a yes to "does this child have Resimlerim" is trusted (`may_open`)."""
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
        self.background: set[asyncio.Task[Any]] = set()
        """Work an answer does not wait for (D-208). Held, because the event
        loop keeps only a weak reference to a task and could collect one
        mid-flight."""
        self.home = Path.home()
        """The child's home: their pictures (D-212) and the album's one note
        (D-211). The launcher runs as the child, so it is theirs."""
        self.cheered: str | None = None
        """The day today's sticker was last celebrated, once read from disk."""
        self.page_ok: dict[str, float] = {}
        """Page tiles a recent state said this child has, until when (monotonic)."""

    @property
    def cheer_file(self) -> Path:
        return self.home / CHEER_FILE

    def take_cheer(self, found: album.Album | None) -> bool:
        """True the first time today's sticker is shown as new, and never again
        that day (D-211): not on every load, and not again after a restart of
        the launcher or a second login, which is why the day is kept in a file
        of the child's own. A file that cannot be written costs a second
        celebration, never the page."""
        if found is None or not found.earned_today:
            return False
        day = found.today.isoformat()
        if self.cheered is None:
            try:
                self.cheered = self.cheer_file.read_text(encoding="utf-8").strip()
            except OSError:
                self.cheered = ""
        if self.cheered == day:
            return False
        self.cheered = day
        try:
            self.cheer_file.parent.mkdir(parents=True, exist_ok=True)
            self.cheer_file.write_text(day + "\n", encoding="utf-8")
        except OSError as exc:
            log.info("could not remember today's sticker celebration: %s", exc)
        return True

    async def may_open(self, slug: str) -> bool:
        """Whether this child has the page tile `slug`, asked of the daemon at
        most every PAGE_TILE_TTL_S: a grid of pictures is a request per picture,
        and the answer does not change between them."""
        now = time.monotonic()
        if self.page_ok.get(slug, 0.0) > now:
            return True
        data = await self.client.state()
        if _find_tile(data, slug) is None:
            self.page_ok.pop(slug, None)
            return False
        self.page_ok[slug] = now + PAGE_TILE_TTL_S
        return True

    def in_background(self, work: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        """Run `work` after the answer has gone; a failure is logged, never raised."""
        task = asyncio.create_task(work, name=name)
        self.background.add(task)
        task.add_done_callback(self._background_done)
        return task

    def _background_done(self, task: asyncio.Task[Any]) -> None:
        self.background.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error("%s failed: %s", task.get_name(), error, exc_info=error)

    def remember_spoken(self, card: dict[str, Any]) -> None:
        self.spoken = dict(card)

    async def say(self, text: str, locale: str) -> None:
        """Zio reads a line aloud (D-165). Silence is not an error."""
        try:
            await self.voice.call("say", text=text, locale=locale, persona="zio")
        except (IpcError, OSError) as exc:
            log.info("the line was not read aloud: %s", exc)

    async def say_and_wait(self, text: str, locale: str, persona: str = "zio") -> bool:
        """Zio says `text`; this returns once it has been said, or cannot be (D-216).

        The voice service answers `say` at once and tells its subscribers when
        a line has finished, so this listens first and asks second: an end
        announced before the listening began would never be heard. False when
        nothing was said: no voice service, a queue full of more urgent lines,
        or no end heard in good time. Silence is not an error; the page simply
        goes on to the next sentence.
        """
        try:
            reader, writer = await _open_voice(voice_socket_path(self.config))
        except (OSError, TimeoutError, AttributeError, NotImplementedError) as exc:
            log.info("the answer was not read aloud: %s", exc)
            return False
        try:
            writer.write(IpcRequest(op="subscribe", id="said").encode())
            await writer.drain()
            ack = await asyncio.wait_for(reader.readline(), timeout=SAY_CONNECT_S)
            if not ack or not IpcResponse.decode(ack).ok:
                return False
            reply = await self.voice.call("say", text=text, locale=locale, persona=persona)
            if not reply.ok or reply.data.get("status") == "dropped":
                return False
            line = str(reply.data.get("id", ""))
            hold = max(0.0, float(reply.data.get("duration_ms") or 0) / 1000)
            async with asyncio.timeout(min(SAY_MAX_WAIT_S, 2 * hold + SAY_SLACK_S)):
                while raw := await reader.readline():
                    event = IpcEvent.decode(raw)
                    if event.kind == SPEAK_FINISHED and str(event.payload.get("id")) == line:
                        return True
            return False
        except (IpcError, OSError, TimeoutError, TypeError, ValueError) as exc:
            log.info("the answer was not heard to the end: %s", exc)
            return False
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()


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
            # What a parent chose for this child's screen (D-217), on <html>.
            # Checked again here: only known values may reach an attribute.
            "appearance": Appearance.from_state(data.get("appearance")),
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
        found = album.from_state(data)
        return await render(
            shell,
            request,
            "home.html",
            data,
            volume=DEFAULT_VOLUME if volume is None else volume,
            # Bugünün sürprizi (D-203): today's card, for this child's tiles.
            daily=daily.for_launcher(data, dt.date.today(), tile_can_start),
            # The sticker album's chip (D-211), for a child who has a sticker
            # or a tile that can earn one; and the day's celebration, once.
            album=found if found and (found.count or _learning_tiles(data)) else None,
            cheer=_cheer(shell, data, found),
            page_tiles=PAGE_TILES,
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
        that is happening (UX.md §2.4). Since D-208 the home page opens an app
        without coming here; this screen is for a launcher without JavaScript,
        whose button posts a plain form, and for any link that still leads here.
        """
        data = await shell.client.state()
        tile = _find_tile(data, slug)
        if tile is None:
            return await render(
                shell, request, "blocked.html", data, reason=Reason.APP_NOT_ALLOWED
            )
        if slug in PAGE_TILES:
            return RedirectResponse(PAGE_TILES[slug], status_code=303)
        return await render(shell, request, "launching.html", data, tile=tile)

    @app.post("/launch/{slug}")
    async def launch(request: Request, shell: Shell, slug: str) -> Response:
        """Start the app, or bring it back; answered as soon as that is settled.

        The page shows the tile opening from the tap on (D-208), so this answer
        matters for a refusal, which sends the child to the calm screen - and,
        with `tracked`, for an app that ends as it starts, which the page then
        sees missing from /running and treats the same. The
        tile and the permission are asked of the daemon together - the daemon
        decides the permission on its own list, never this one - and the
        activity is reported after the answer has gone. A plain form (the
        launching screen without JavaScript) gets a page back instead of JSON.
        """
        as_page = _is_form_post(request)
        if slug in BUILTIN_TILES:
            data = await shell.client.state()
            permission: tuple[bool, str] = (True, "")
        else:
            data, permission = await asyncio.gather(
                shell.client.state(), shell.client.may_launch(slug)
            )
        tile = _find_tile(data, slug)
        if tile is None:
            return _launch_answer(as_page, slug, ok=False, reason=Reason.APP_NOT_ALLOWED)

        if slug in BUILTIN_TILES:
            # Volume and brightness are the launcher's own controls; shutdown has
            # its own confirmation screen, and Resimlerim is a page (D-212).
            # None of them starts a process.
            page = "/shutdown" if slug == "shutdown" else PAGE_TILES.get(slug)
            if as_page:
                return RedirectResponse(page or "/", status_code=303)
            answer: dict[str, object] = {"ok": True, "builtin": slug}
            if page:
                answer["page"] = page
            return JSONResponse(answer)

        allowed, reason = permission
        if not allowed:
            return _launch_answer(as_page, slug, ok=False, reason=reason)

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
            shell.in_background(
                shell.client.report_activity(
                    slug, str(tile.get("name", slug)), str(tile.get("category", ""))
                ),
                "report-activity",
            )
        return _launch_answer(
            as_page,
            slug,
            ok=result.ok,
            reason=result.error,
            tracked=result.ok and result.tracked,
        )

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
        """The confirmation's "Evet, kapat": a goodbye, never the home screen (D-209).

        The daemon answers at once with what it will do and does it afterwards:
        the computer switches off, or, while someone else is logged in, only
        this child's session ends - and the page says which, in words a child
        can follow. When the daemon could not be asked, the child is told so and
        to find a grown-up. Put back on the home screen, as before, they could
        not tell "nothing happened" from "about to happen".
        """
        outcome = await shell.client.request_shutdown()
        data = await shell.client.state()
        if outcome is None:
            return await render(shell, request, "shutdown.html", data, failed=True)
        return await render(shell, request, "goodbye.html", data, outcome=outcome.value)

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
        """A question in; its refusal, or the start of its answer, out (D-216).

        JSON only, and only from the launcher's own page: a site open in the
        child's browser could otherwise post questions in the child's name and
        send their parent alarming alerts about things the child never asked.
        """
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "bad request"}, status_code=400)
        question = str(body.get("question", ""))[:600]
        if body.get("mode") != english.CHAT_MODE:
            return await _answer_so_far(shell, await shell.client.ask_assistant(question))
        # English with Sio (D-226): what was said so far, from the page, and
        # the words this child has met, from the daemon.
        raw = body.get("history")
        history = [
            [str(who), " ".join(str(said).split())[:300]]
            for who, said in (
                entry
                for entry in (raw if isinstance(raw, list) else [])[-8:]
                if isinstance(entry, list) and len(entry) == 2
            )
            if who in {"child", "sio"}
        ]
        progress, _today = _english_progress(await shell.client.english_state())
        words = english.known_words(english.load_units(), progress)
        return await _answer_so_far(
            shell,
            await shell.client.ask_assistant(
                question, mode=english.CHAT_MODE, history=history, words=words
            ),
        )

    @app.post("/assistant/more")
    async def assistant_more(request: Request, shell: Shell) -> Response:
        """The sentences of an answer written since the page last asked (D-216).

        The page asks about twice a second until the answer is done. Only from
        the launcher's own page, like the question itself.
        """
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await _json_body(request)
        if body is None:
            return JSONResponse({"error": "bad request"}, status_code=400)
        answer_id = str(body.get("id", ""))[:64]
        return await _answer_so_far(shell, await shell.client.assistant_more(answer_id))

    @app.post("/assistant/say")
    async def assistant_say(request: Request, shell: Shell) -> Response:
        """Zio says one sentence of an answer; answered once it has been said (D-216).

        The page sends the next sentence only when this one is answered, so an
        answer is read in order and never two sentences at once. It also keeps
        at most one of them waiting in the voice service's queue, which holds
        three lines and drops the oldest ordinary one when it is full: a fast
        model must not push a time warning out of it.
        """
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await _json_body(request)
        text = " ".join(str((body or {}).get("text", "")).split())[:MAX_SAY_CHARS]
        said = False
        if text and (body or {}).get("mode") == english.CHAT_MODE:
            # Sio, in English, whatever the child's own language (D-226).
            said = await shell.say_and_wait(text, english.LANGUAGE, "sio")
        elif text:
            data = await shell.client.state() or {}
            said = await shell.say_and_wait(
                text, normalise_locale(str(data.get("locale", "tr")))
            )
        return JSONResponse({"ok": said}, headers={"Cache-Control": "no-store"})

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
    async def assistant_hear(request: Request, shell: Shell, lang: str = "") -> Response:
        """Press-and-speak (D-166): a short WAV in, its words out, nothing kept.
        `lang=en` hears it as English: talking with Sio (D-226)."""
        if not _from_launcher(request, "audio/wav"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        audio = await request.body()
        if not audio or len(audio) > MAX_RECORDING_BYTES:
            return JSONResponse({"error": "too long"}, status_code=413)
        text = await shell.client.hear_assistant(
            base64.b64encode(audio).decode("ascii"),
            english.LANGUAGE if lang == english.LANGUAGE else "",
        )
        del audio
        return JSONResponse(
            {"text": text or "", "ok": text is not None}, headers={"Cache-Control": "no-store"}
        )

    # -------------------------------------------------------------- album

    @app.get("/album", response_class=HTMLResponse)
    async def album_page(request: Request, shell: Shell) -> Response:
        """Çıkartma albümüm (D-211): the stickers, the week, and how to get more.

        Drawn whole by the server, so it reads with scripts off. A child whose
        day has ended gets the screen that says so, from the home route.
        """
        data = await shell.client.state()
        if data is None:
            return await render(shell, request, "offline.html", None)
        found = album.from_state(data)
        if found is None or _screen_for(data) is not None:
            return RedirectResponse("/", status_code=303)
        tr = translator(normalise_locale(str(data.get("locale", "tr"))))
        return await render(
            shell,
            request,
            "album.html",
            data,
            album=found,
            words=_album_words(tr, found),
            earners=_learning_tiles(data),
            cheer=_cheer(shell, data, found),
            on_album=True,
        )

    @app.post("/album/cheer")
    async def album_cheer(request: Request, shell: Shell) -> Response:
        """Asked by the home page whenever the launcher comes back in front.

        The child is usually in a learning app when the quarter of an hour
        fills up, and the launcher's page was drawn long before: this is how
        the chip learns the new number and the day's celebration happens when
        the child comes back, once (`ShellState.take_cheer`). Only from the
        launcher's own page.
        """
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        data = await shell.client.state()
        found = album.from_state(data)
        if data is None or found is None:
            return JSONResponse({"count": None, "cheer": None})
        tr = translator(normalise_locale(str(data.get("locale", "tr"))))
        return JSONResponse(
            {
                "count": found.count,
                "label": tr("album.chip", count=found.count),
                "cheer": _cheer(shell, data, found),
            },
            headers={"Cache-Control": "no-store"},
        )

    # ----------------------------------------------------------- pictures

    @app.get("/pictures", response_class=HTMLResponse)
    async def pictures_page(request: Request, shell: Shell) -> Response:
        """Resimlerim (D-212): the child's own pictures, newest first."""
        data = await shell.client.state()
        if data is None:
            return await render(shell, request, "offline.html", None)
        if _find_tile(data, "drawings") is None or _screen_for(data) is not None:
            return RedirectResponse("/", status_code=303)
        shown = await asyncio.to_thread(pictures.pictures, shell.home)
        return await render(
            shell, request, "pictures.html", data, shown=shown, paint=_paint_tile(data)
        )

    @app.get("/pictures/view/{folder}/{name}", response_class=HTMLResponse)
    async def picture_page(request: Request, shell: Shell, folder: str, name: str) -> Response:
        """One picture, as big as the screen, with ‹ › and ✕. Links, so the
        child can page through without a script; picture.js adds the arrow
        keys and a swipe."""
        data = await shell.client.state()
        if data is None:
            return await render(shell, request, "offline.html", None)
        if _find_tile(data, "drawings") is None or _screen_for(data) is not None:
            return RedirectResponse("/", status_code=303)
        shown = await asyncio.to_thread(pictures.pictures, shell.home)
        place = pictures.neighbours(shown, folder, name)
        if place is None:
            return RedirectResponse("/pictures", status_code=303)
        index, newer, older = place
        return await render(
            shell,
            request,
            "picture.html",
            data,
            picture=shown[index],
            number=index + 1,
            total=len(shown),
            newer=newer,
            older=older,
        )

    @app.get("/pictures/file/{folder}/{name}")
    async def picture_file(request: Request, shell: Shell, folder: str, name: str) -> Response:
        """The picture itself, for the launcher's own pages and nobody else's.

        A website open in the child's browser could otherwise put the child's
        drawings on its own page, so a request another site makes is refused,
        and the answer tells the browser not to hand it to one
        (Cross-Origin-Resource-Policy). Only a picture of the child's own, in
        one of their picture folders (biokids_shell.pictures).
        """
        if request.headers.get("sec-fetch-site", "same-origin") not in {"same-origin", "none"}:
            return PlainTextResponse("", status_code=403)
        if not await shell.may_open("drawings"):
            return PlainTextResponse("", status_code=404)
        found = await asyncio.to_thread(pictures.find, shell.home, folder, name)
        content = await asyncio.to_thread(pictures.read, found) if found else None
        if found is None or content is None:
            return PlainTextResponse("", status_code=404)
        return Response(
            content,
            media_type=found.media_type,
            headers={
                "Cache-Control": "no-store",
                "Cross-Origin-Resource-Policy": "same-origin",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    # ------------------------------------------------- English corner (D-225)

    async def english_gate(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
        """The child's state, or the page to show instead: the corner is a tile
        like any other, closed when the parent has not given it and while a
        full screen - the time is up, a break - is what the child should see."""
        data = await get_state(request).client.state()
        if data is None:
            return None, await render(get_state(request), request, "offline.html", None)
        if _find_tile(data, english.SLUG) is None or _screen_for(data) is not None:
            return None, RedirectResponse("/", status_code=303)
        return data, None

    @app.get("/english", response_class=HTMLResponse)
    async def english_page(request: Request, shell: Shell) -> Response:
        """İngilizce köşesi: today's lesson, the word of the day, how far the child has come."""
        data, instead = await english_gate(request)
        if instead is not None:
            return instead
        progress, today = _english_progress(await shell.client.english_state())
        units = english.load_units()
        lesson = english.plan_lesson(units, progress, today)
        summary = english.summarize(units, progress)
        return await render(
            shell,
            request,
            "english.html",
            data,
            summary=summary,
            units=units,
            done={unit.slug for unit in units if _unit_done(unit, progress)},
            word=english.word_of_the_day(
                units, progress, today, salt=sum(map(ord, str(data.get("display_name", ""))))
            ),
            lesson_steps=len(lesson.steps),
        )

    @app.get("/english/lesson", response_class=HTMLResponse)
    async def english_lesson(request: Request, shell: Shell, more: int = 0) -> Response:
        """The lesson itself, drawn by english.js from the steps planned here.

        `more` is the "play a bit more" after today's lesson: words already
        met, found again, with no box moving (`english.practice`).
        """
        data, instead = await english_gate(request)
        if instead is not None or data is None:
            return instead or RedirectResponse("/", status_code=303)
        progress, today = _english_progress(await shell.client.english_state())
        units = english.load_units()
        steps = () if more else english.plan_lesson(units, progress, today).steps
        practice = not steps
        if practice:
            steps = english.practice(units, progress)
        if not steps:
            return RedirectResponse("/english", status_code=303)
        assistant = data.get("assistant") or {}
        tr = translator(normalise_locale(str(data.get("locale", "tr"))))
        return await render(
            shell,
            request,
            "english_lesson.html",
            data,
            lesson={
                "steps": [step.as_json() for step in steps],
                "practice": practice,
                "mic": bool(assistant.get("voice")),
                "older": str(data.get("age_band", "")) != AgeBand.YOUNG.value,
                "praise": list(english.PRAISE),
                "goodbye": english.GOODBYE,
                "strings": _english_strings(tr),
            },
        )

    @app.get("/english/chat", response_class=HTMLResponse)
    async def english_chat(request: Request, shell: Shell) -> Response:
        """Talk with Sio in English (D-226): the assistant, in conversation.

        Only where the parent turned the assistant on for this child, and the
        assistant can answer: the same switch, the same checks, the same note
        that a parent may see what is said.
        """
        data, instead = await english_gate(request)
        if instead is not None or data is None:
            return instead or RedirectResponse("/", status_code=303)
        if not (data.get("assistant") or {}).get("enabled"):
            return RedirectResponse("/english", status_code=303)
        progress, _today = _english_progress(await shell.client.english_state())
        units = english.load_units()
        return await render(
            shell,
            request,
            "english_chat.html",
            data,
            chat={
                "mode": english.CHAT_MODE,
                "opening": english.chat_opening(
                    english.current_unit(units, progress), str(data.get("display_name", ""))
                ),
            },
            chips=english.chat_chips(units, progress),
        )

    @app.get("/english/words", response_class=HTMLResponse)
    async def english_words(request: Request, shell: Shell) -> Response:
        """Sözlüğüm: every word the child has met, by unit; a tap says it."""
        data, instead = await english_gate(request)
        if instead is not None:
            return instead
        progress, _today = _english_progress(await shell.client.english_state())
        units = english.load_units()
        return await render(
            shell,
            request,
            "english_words.html",
            data,
            units=units,
            met={item for item, state in progress.items() if state.met},
            known={
                item for item, state in progress.items() if state.box >= english.MASTERED_BOX
            },
        )

    @app.post("/english/answer")
    async def english_answer(request: Request, shell: Shell) -> Response:
        """One answer from the lesson page; the daemon moves the item's box."""
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await _json_body(request) or {}
        item = str(body.get("item", ""))[:64]
        box = await shell.client.english_answer(item, body.get("right") is True)
        return JSONResponse({"ok": box is not None, "box": box or 0})

    @app.post("/english/say")
    async def english_say(request: Request, shell: Shell) -> Response:
        """One line read aloud, answered once it has been said: English by the
        English voice, an instruction in the child's own language."""
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        body = await _json_body(request) or {}
        text = " ".join(str(body.get("text", "")).split())[:MAX_SAY_CHARS]
        said = False
        if text:
            if body.get("lang") == english.LANGUAGE:
                locale = english.LANGUAGE
            else:
                data = await shell.client.state() or {}
                locale = normalise_locale(str(data.get("locale", "tr")))
            said = await shell.say_and_wait(text, locale)
        return JSONResponse({"ok": said}, headers={"Cache-Control": "no-store"})

    @app.post("/english/hear")
    async def english_hear(request: Request, shell: Shell, item: str = "") -> Response:
        """A word or line said back, heard as English and compared (D-225, D-166).

        Only where the parent let the child speak to the assistant: the same
        microphone, the same switch. The recording is dropped once heard.
        """
        if not _from_launcher(request, "audio/wav"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        card = english.card_for(item)
        audio = await request.body()
        if card is None or not audio or len(audio) > MAX_RECORDING_BYTES:
            return JSONResponse({"error": "bad request"}, status_code=400)
        text = await shell.client.hear_assistant(
            base64.b64encode(audio).decode("ascii"), english.LANGUAGE
        )
        del audio
        return JSONResponse(
            {
                "ok": text is not None,
                "heard": text or "",
                "match": bool(text) and english.heard_matches(text or "", card.en),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/english/active")
    async def english_active(request: Request, shell: Shell) -> Response:
        """The lesson page, every so often while it is in front: the minutes are
        the corner's, in the kind of app the parent gave it - learning, unless
        they chose otherwise - so they earn time like any learning app (D-45)."""
        if not _from_launcher(request, "application/json"):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        data = await shell.client.state()
        tile = _find_tile(data, english.SLUG)
        if tile is None:
            return JSONResponse({"ok": False})
        await shell.client.report_activity(
            english.SLUG, str(tile.get("name", english.SLUG)), str(tile.get("category", ""))
        )
        return JSONResponse({"ok": True})

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

    @app.post("/search-refused")
    async def search_refused(request: Request, shell: Shell) -> Response:
        """A search the extension refused, for the parent to read (D-222).

        Only the extension may post here: the launcher's own pages have no
        reason to. Its address is checked against the same expressions the
        extension installed, so nothing else becomes something the parent
        reads; of the address, only the words searched for and the site's name
        go on to the daemon. The block page the child is looking at says that
        a grown-up will see it.
        """
        if request.headers.get("origin") != _extension_origin(request):
            return PlainTextResponse("", status_code=403)
        body = await _json_body(request)
        url = str(body.get("url", "")) if body else ""
        words = search_filter.refused_search(url, shell.search_words or ())
        if not words:
            return Response(status_code=204)
        host = (urlsplit(url).hostname or "").lower()
        await shell.client.report_refused_search(
            words, host if _HOSTNAME.fullmatch(host) else ""
        )
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

MAX_SAY_CHARS = 600
"""One sentence of an answer, or a catalogue sentence that ends one."""
SPEAK_FINISHED = "speak_finished"
"""`biokids_voice.service.EVENT_FINISHED`, spelled out for the reason relay.py
spells out its op: biokids-voice is a Recommends of biokids-shell."""
SAY_CONNECT_S = 3.0
SAY_SLACK_S = 15.0
"""Beyond twice the line's own length: a time warning may be ahead of it."""
SAY_MAX_WAIT_S = 60.0


async def _json_body(request: Request) -> dict[str, Any] | None:
    """The request's JSON object, {} for an empty body, None for anything else."""
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def _answer_so_far(shell: ShellState, reply: dict[str, Any]) -> Response:
    """What the page needs of an answer in progress (D-216).

    The sentences released since it last asked, whether the answer is done,
    and - once it is - the sentence it ends with, translated here: a refusal,
    the ending of an answer the word list stopped, or "I'm resting". Never
    generated: those are sentences people wrote (D-165). `refused` asks the
    page to mark a bubble that holds nothing else.
    """
    done = bool(reply.get("done", True))
    sentences = [
        text for text in (str(s).strip() for s in reply.get("sentences") or []) if text
    ]
    ending = ""
    refused = bool(reply.get("refused"))
    if done and refused:
        data = await shell.client.state() or {}
        tr = translator(normalise_locale(str(data.get("locale", "tr"))))
        ending = tr(str(reply.get("refusal_key") or "assistant.refuse.default"))
    return JSONResponse(
        {
            "id": "" if done else str(reply.get("answer_id", "")),
            "sentences": sentences,
            "done": done,
            "ending": ending,
            "refused": refused,
        },
        headers={"Cache-Control": "no-store"},
    )


async def _open_voice(path: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    # typeshed declares the unix helpers on POSIX only, and mypy runs on the
    # Windows development host. Production is Linux (D-12).
    open_unix = cast(
        "Callable[[str], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]",
        getattr(asyncio, "open_unix_connection"),  # noqa: B009
    )
    return await asyncio.wait_for(open_unix(str(path)), timeout=SAY_CONNECT_S)


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
    reason = str(data.get("reason_code", ""))
    if data.get("bedtime") and reason in {Reason.QUOTA_EXHAUSTED, Reason.WINDOW_CLOSED}:
        # The day ended in the evening (D-214): good night, rather than a cold
        # "that is all". The same countdown and the same end action.
        return "good_night.html"
    match reason:
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
        "bedtime_warning": tr("shell.bedtime.warning", minutes="{minutes}"),
        "earned": tr("shell.earned.body", minutes="{minutes}"),
        "request_approved": tr("shell.request.approved"),
        "request_approved_time": tr("shell.request.approved_time", minutes="{minutes}"),
        "request_denied": tr("shell.request.denied"),
        "timeup": tr("shell.timeup.title"),
        # Over a tapped tile until its app's window is in front (D-208).
        "opening": tr("shell.opening"),
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
        # The evening's (D-214): the notice and the card, in the night's words.
        "bedtime_10": tr("shell.bedtime.warning", minutes=10),
        "bedtime_5": tr("shell.bedtime.warning", minutes=5),
        "bedtime_1": tr("shell.bedtime.warning", minutes=1),
        "bedtime": tr("shell.bedtime.line"),
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


EARNERS_SHOWN = 4
"""The album lists this many of the child's tiles that earn a sticker."""


def _learning_tiles(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The child's own tiles that earn a sticker (D-211): those in the learning
    category, as the parent set it, and able to start on this machine. The
    album names them, so a child who cannot read "öğrenme" still knows where
    a sticker comes from."""
    return [
        dict(tile)
        for tile in (data or {}).get("tiles", [])
        if isinstance(tile, dict)
        and tile.get("category") == "learning"
        and tile.get("slug") not in BUILTIN_TILES
        and tile_can_start(tile)
    ][:EARNERS_SHOWN]


def _paint_tile(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Boya, or failing that Çizim, when the child has it: the empty Resimlerim
    page offers it (D-212)."""
    for slug in ("tuxpaint", "kolourpaint"):
        tile = _find_tile(data, slug)
        if tile is not None and tile_can_start(tile):
            return tile
    return None


def _cheer(
    shell: ShellState, data: dict[str, Any] | None, found: album.Album | None
) -> dict[str, str] | None:
    """Today's sticker, as the celebration shows it - the first time only.

    Zio says the sentence as it is decided (D-175's voice, the same call the
    page's read-aloud makes), after the answer has gone: once a day, for
    either age band, whichever page or poll comes first.
    """
    if found is None or not shell.take_cheer(found):
        return None
    locale = normalise_locale(str((data or {}).get("locale", "tr")))
    tr = translator(locale)
    sticker = found.today_sticker
    name = tr(sticker.key)
    text = tr("album.cheer", sticker=name)
    shell.in_background(shell.say(text, locale), "album-cheer")
    return {"sticker": sticker.emoji, "name": name, "text": text}


def _english_progress(
    found: dict[str, Any] | None,
) -> tuple[dict[str, english.ItemState], dt.date]:
    """The daemon's answer to `english_state` as boxes and a day.

    Without the daemon, no progress and today's date: the child still gets a
    lesson, from the beginning, and nothing they answer is lost but that one.
    """
    today = dt.date.today()
    if not found:
        return {}, today
    with contextlib.suppress(ValueError, TypeError):
        today = dt.date.fromisoformat(str(found.get("today", "")))
    progress: dict[str, english.ItemState] = {}
    items = found.get("items")
    if isinstance(items, dict):
        for item, raw in items.items():
            try:
                box, due, first = raw
                progress[str(item)] = english.ItemState(
                    int(box),
                    dt.date.fromisoformat(due) if due else None,
                    dt.date.fromisoformat(first) if first else None,
                )
            except (TypeError, ValueError):
                continue
    return progress, today


def _unit_done(unit: english.Unit, progress: dict[str, english.ItemState]) -> bool:
    return all(
        (state := progress.get(item)) is not None and state.met for item in unit.item_ids()
    )


ENGLISH_STRINGS = (
    "new_word",
    "find",
    "wrong",
    "line",
    "story",
    "question",
    "again",
    "say",
    "said",
    "next",
    "listening",
    "heard_ok",
    "heard_again",
    "mic_failed",
    "done",
    "home",
    "more",
)


def _english_strings(tr: Translator) -> dict[str, str]:
    """What the lesson page says and shows in the child's own language."""
    return {key: tr(f"english.{key}") for key in ENGLISH_STRINGS}


def _album_words(tr: Translator, found: album.Album) -> dict[str, str]:
    """The week in a sentence or two a parent can read out to a five-year-old.

    The week first - how many stickers since Monday - then today: earned, or
    how many minutes more, or how to start. The minutes are whole and rounded
    up, as on the ring: half a minute left is "1 more".
    """
    count = found.week_count
    if count == 0:
        week = tr("album.week.none")
    elif count == 1:
        week = tr("album.week.one")
    else:
        week = tr("album.week.many", count=count)
    if found.earned_today:
        today = tr("album.today.earned", sticker=tr(found.today_sticker.key))
    elif found.today_seconds <= 0:
        today = tr("album.today.start", minutes=found.goal_minutes)
    elif found.minutes_left == 1:
        today = tr("album.today.left_one")
    else:
        today = tr("album.today.left", minutes=found.minutes_left)
    return {"week": week, "today": today}


FORM_TYPES = frozenset({"application/x-www-form-urlencoded", "multipart/form-data"})


def _is_form_post(request: Request) -> bool:
    """A form the launching screen posts without JavaScript, not the page's fetch."""
    sent = request.headers.get("content-type", "").split(";")[0].strip().lower()
    return sent in FORM_TYPES


def _launch_answer(
    as_page: bool, slug: str, *, ok: bool, reason: str, tracked: bool = False
) -> Response:
    """JSON for the page's script; for a plain form, where the child goes next:
    home while the app opens, or the calm screen that says why it did not.

    `tracked`: an app tile's app, which /running lists for as long as it runs.
    The yes comes as soon as the app is running, a moment before one that
    cannot start has failed; the page asks /running a little later, and ends
    the tile's opening with the calm screen when it is gone (D-208)."""
    if not as_page:
        answer: dict[str, object] = {"ok": ok, "reason": reason}
        if tracked:
            answer["tracked"] = True
        return JSONResponse(answer)
    if ok:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse(
        "/blocked?" + urlencode({"reason": reason, "slug": slug}), status_code=303
    )


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
