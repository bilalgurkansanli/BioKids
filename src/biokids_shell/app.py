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
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import ChainableUndefined, Environment, FileSystemLoader, select_autoescape

from biokids_core.config import Config, get_config
from biokids_core.i18n import Translator, normalise_locale, translator
from biokids_core.types import AgeBand, Reason
from biokids_shell import device
from biokids_shell.client import ShellClient
from biokids_shell.launcher import AppLauncher, LaunchRequest

log = logging.getLogger(__name__)

SHELL_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = SHELL_DIR / "templates"
STATIC_DIR = SHELL_DIR / "static"
SHARED_STATIC_DIR = SHELL_DIR.parent / "biokids_core" / "data" / "static"
CHARACTER_DIR = SHELL_DIR.parent.parent / "assets" / "characters"

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
"""Drawn by the launcher and handled in the browser; they start no process."""


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
        self.characters = _load_characters()


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


def _load_characters() -> dict[str, str]:
    """Inline Zio and Sio, so the card needs no network and no second request."""
    found: dict[str, str] = {}
    for persona in ("zio", "sio"):
        path = CHARACTER_DIR / f"{persona}.svg"
        if path.is_file():
            found[persona] = path.read_text(encoding="utf-8")
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
            "tiles": data.get("tiles", []),
            "characters": shell.characters,
            "strings": _browser_strings(tr),
            "lines": _spoken_lines(tr),
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
        return await render(shell, request, "home.html", data)

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
        reason: str = Reason.FILTER_BLOCKED,
    ) -> Response:
        data = await shell.client.state()
        return await render(
            shell, request, "blocked.html", data, reason=reason, blocked_url=url
        )

    @app.post("/ask-a-parent")
    async def ask_a_parent(
        request: Request, shell: Shell, url: Annotated[str, Form()] = ""
    ) -> Response:
        """The button that turns the filter into a conversation (D-65)."""
        ok = await shell.client.ask_a_parent(url)
        data = await shell.client.state()
        return await render(shell, request, "asked.html", data, ok=ok)

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

    # ------------------------------------------------------------- events

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
        return JSONResponse(data or {"offline": True})

    @app.get("/healthz")
    async def healthz(shell: Shell) -> JSONResponse:
        return JSONResponse({"ok": True, "daemon": await shell.client.available()})

    # ------------------------------------------------------------- device

    @app.post("/device/volume")
    async def set_volume(value: int = 70) -> JSONResponse:
        return JSONResponse({"ok": await device.set_volume(value)})

    @app.post("/device/brightness")
    async def set_brightness(value: int = 100) -> JSONResponse:
        return JSONResponse({"ok": await device.set_brightness(value)})


# ------------------------------------------------------------------ helpers


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
        "timeup": tr("shell.timeup.title"),
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
