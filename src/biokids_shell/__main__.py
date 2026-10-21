"""`biokids-shell` — the child's session.

Runs as a systemd **user** unit inside the child's own login, serving the
launcher on `127.0.0.1:7879` and supervising the kiosk browser that shows it.
Two tasks in one process, because they have exactly the same lifetime: if the
web server is gone the browser has nothing to show, and if the browser is gone
the child is looking at an empty desktop.

It has no privilege. Everything it enforces, it enforces by asking the daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

import uvicorn

from biokids_core.config import Config, set_config
from biokids_core.ipc import AsyncDaemonClient
from biokids_shell.app import ShellState, create_app
from biokids_shell.client import ShellClient
from biokids_shell.dimmer import BedtimeDimmer
from biokids_shell.kiosk import KioskSupervisor, keep_fullscreen, launcher_port
from biokids_shell.relay import SessionRelay, WindowRaiser, voice_socket_path
from biokids_shell.windows import (
    RULES_SUBDIR,
    AppBar,
    WindowRules,
    apply_window_manager_settings,
)

SHUTDOWN_GRACE_SECONDS = 3
"""How long the launcher's server waits for open connections when it stops. The
page's event stream never closes on its own, and without a bound the server
waited for it forever - so a logout, or a shutdown, waited for systemd to kill
the unit (D-160)."""

BRIGHTNESS_RESTORE_SECONDS = 2.0
"""How long putting the brightness back may take, at start and at stop (D-214)."""

log = logging.getLogger("biokids.shell")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="biokids-shell",
        description="BioKids child launcher. Runs as the child, from a user unit.",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--no-kiosk",
        action="store_true",
        help="serve the launcher without starting a browser, for development",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("BIOKIDS_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error"],
    )
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    set_config(config)
    # Each child's own port (D-188): two children logged in at once each have a
    # launcher, and nftables lets only this child's processes reach this one.
    port = args.port or launcher_port(config)

    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.shell.host,
            port=port,
            log_level=args.log_level,
            access_log=False,
            # No access log: it would be a record of which tiles a child pressed
            # and when, kept somewhere nobody asked for it to be kept.
            server_header=False,
            timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
        )
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop_event.set)

    kiosk = KioskSupervisor(config)
    rules = WindowRules(config.paths.share_dir / RULES_SUBDIR)
    appbar = AppBar()
    shell: ShellState = app.state.shell
    # The evening's dimming (D-214). Not on a developer's own desktop; and a
    # brightness the last session left dimmed is put back before anything else.
    dimmer = None if args.no_kiosk else BedtimeDimmer()
    if dimmer is not None:
        await _restore_brightness(dimmer)
    relay = SessionRelay(
        ShellClient(config.paths.shell_socket),
        voice=AsyncDaemonClient(voice_socket_path(config)),
        raiser=None if args.no_kiosk else WindowRaiser(),
        on_spoken=shell.remember_spoken,
        dimmer=dimmer,
    )
    tasks = [
        asyncio.create_task(server.serve(), name="launcher"),
        asyncio.create_task(relay.run(stop_event), name="relay"),
    ]
    if not args.no_kiosk:
        # Before any app can open: the big close button, and every app window
        # filling the screen so that the button is always in the same corner
        # (D-163). Neither belongs on a developer's own desktop, hence the guard.
        await apply_window_manager_settings()
        tasks.append(asyncio.create_task(rules.supervise(stop_event), name="window-rules"))
        # The app bar: home, the app's volume and a close that works (D-197).
        # It takes the title bar's close button over once it is on screen.
        tasks.append(asyncio.create_task(appbar.supervise(stop_event), name="app-bar"))
        # Give the launcher a moment to bind before pointing a browser at it;
        # Chromium's own error page is not something a four-year-old should see.
        await asyncio.sleep(0.5)
        tasks.append(asyncio.create_task(kiosk.supervise(stop_event), name="kiosk"))
        tasks.append(asyncio.create_task(keep_fullscreen(stop_event), name="kiosk-fit"))

    await stop_event.wait()

    await kiosk.stop()
    await rules.stop()
    await appbar.stop()
    server.should_exit = True
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if dimmer is not None and dimmer.dimmed:
        # Whoever uses the machine next should not find it dim; if the session
        # is going too fast for this, the next one puts it back.
        await _restore_brightness(dimmer)
    return 0


async def _restore_brightness(dimmer: BedtimeDimmer) -> None:
    """Put the brightness back, within a bound: never what holds a logout up."""
    try:
        await asyncio.wait_for(dimmer.restore(), BRIGHTNESS_RESTORE_SECONDS)
    except Exception as exc:
        log.info("the brightness was not put back: %s", exc)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
