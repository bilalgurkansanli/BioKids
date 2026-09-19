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
from biokids_shell.app import create_app
from biokids_shell.kiosk import KioskSupervisor

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
    port = args.port or config.shell.port

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(config),
            host=config.shell.host,
            port=port,
            log_level=args.log_level,
            access_log=False,
            # No access log: it would be a record of which tiles a child pressed
            # and when, kept somewhere nobody asked for it to be kept.
            server_header=False,
        )
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop_event.set)

    kiosk = KioskSupervisor(config)
    tasks = [asyncio.create_task(server.serve(), name="launcher")]
    if not args.no_kiosk:
        # Give the launcher a moment to bind before pointing a browser at it;
        # Chromium's own error page is not something a four-year-old should see.
        await asyncio.sleep(0.5)
        tasks.append(asyncio.create_task(kiosk.supervise(stop_event), name="kiosk"))

    await stop_event.wait()

    await kiosk.stop()
    server.should_exit = True
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return 0


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
