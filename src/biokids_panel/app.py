"""The panel application: the app itself and the two hard rules.

**It binds to 127.0.0.1 and nothing else.** There is no setting for a LAN
address, because the moment one exists someone forwards a port to it. Remote
access is `tailscale serve`, which publishes it inside the family's own tailnet
over real HTTPS with no open port at all (D-33).

**Every mutation that affects a running child goes through the daemon socket.**
The panel writes policy rows and then asks the daemon to reload; it never locks
a session or stops a process itself. That keeps one writer of live state and one
place where enforcement can go wrong.

Everything works without JavaScript: the forms are ordinary forms and every POST
ends in a redirect. The live counters on the Today screen are an enhancement
layered on top with `EventSource`, so a phone with a flaky tailnet degrades to a
page that is merely not live rather than a page that does not work.

The screens live in `biokids_panel.routes`, one module per area, and what they
share - the dependencies, `render`, the guard, the daemon calls - in
`biokids_panel.common`. The names this module used to define are re-exported
from it, so `from biokids_panel.app import render` keeps working.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from biokids_core.catalog import ALWAYS_ON_TILES
from biokids_core.config import Config, get_config
from biokids_panel.common import (
    Auth,
    Ctx,
    Db,
    Tr,
    _actor,
    _call_daemon,
    _children_from_database,
    _daemon_status,
    _guard,
    _needs_setup,
    _set_session_cookie,
    current_auth,
    current_tr,
    get_ctx,
    get_db,
    render,
    see_other,
)
from biokids_panel.context import SHARED_STATIC_DIR, STATIC_DIR, PanelContext
from biokids_panel.forms import (
    _one_of,
    _parse_window_list,
    _policy_snapshot,
    _schedule_from_form,
    _schedule_from_json,
    _windows_from_pickers,
)
from biokids_panel.routes import (
    audit,
    children,
    history,
    insights,
    login,
    misc,
    parents,
    requests,
    settings,
    special_days,
    today,
    voices,
)
from biokids_panel.routes.audit import _utc_bounds_of_local_day
from biokids_panel.routes.children import _assistant_status
from biokids_panel.routes.misc import SW_CACHE_NAME, static_fingerprint
from biokids_panel.routes.requests import REQUEST_GRANT_CHOICES
from biokids_panel.routes.today import MAX_GRANT_SECONDS, _action_notice, _event_stream

ROUTERS = (
    login.router,
    misc.router,
    today.router,
    requests.router,
    voices.router,
    children.router,
    special_days.router,
    audit.router,
    history.router,
    insights.router,
    settings.router,
    parents.router,
)
"""Every screen but the wizard's, in the order they are matched."""


def create_app(config: Config | None = None) -> FastAPI:
    config = config or get_config()
    app = FastAPI(
        title="BioKids",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        # The panel is a web page for one parent, not an API for anyone. There is
        # nothing to document and an interactive docs page would be one more
        # surface on a host a child shares.
    )
    app.state.ctx = PanelContext.build(config)
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

    register_routes(app)

    from biokids_panel.wizard import register_wizard

    register_wizard(app)
    return app


def register_routes(app: FastAPI) -> None:
    """One router per area of the panel; see `biokids_panel.routes`."""
    for router in ROUTERS:
        app.include_router(router)


__all__ = [
    "ALWAYS_ON_TILES",
    "MAX_GRANT_SECONDS",
    "REQUEST_GRANT_CHOICES",
    "ROUTERS",
    "SW_CACHE_NAME",
    "Auth",
    "Ctx",
    "Db",
    "Tr",
    "_action_notice",
    "_actor",
    "_assistant_status",
    "_call_daemon",
    "_children_from_database",
    "_daemon_status",
    "_event_stream",
    "_guard",
    "_needs_setup",
    "_one_of",
    "_parse_window_list",
    "_policy_snapshot",
    "_schedule_from_form",
    "_schedule_from_json",
    "_set_session_cookie",
    "_utc_bounds_of_local_day",
    "_windows_from_pickers",
    "create_app",
    "current_auth",
    "current_tr",
    "get_ctx",
    "get_db",
    "register_routes",
    "render",
    "see_other",
    "static_fingerprint",
]
