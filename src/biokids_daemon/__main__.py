"""`biokids-daemon` — the entry point.

Starts the supervisor and the socket server, then ticks until systemd says stop.
`SIGTERM` persists the ledger before exiting, so a reboot never costs more than
the usual thirty seconds.
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

from biokids_core.clock import GuardedClock, SystemClock, is_linux
from biokids_core.config import Config, set_config
from biokids_core.db.session import Database
from biokids_daemon import notify
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer
from biokids_daemon.server import DaemonServer
from biokids_daemon.sessions import (
    FakeSessionSource,
    LogindSessionSource,
    SessionSource,
    SessionWatcher,
)
from biokids_daemon.shell_server import ShellServer
from biokids_daemon.supervisor import Supervisor, run_forever

log = logging.getLogger("biokids.daemon")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="biokids-daemon",
        description="BioKids enforcement daemon. Runs as root, from systemd.",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument(
        "--log-level",
        default=os.environ.get("BIOKIDS_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    parser.add_argument(
        "--no-logind",
        action="store_true",
        help="run without logind, for development on a machine that has none",
    )
    parser.add_argument(
        "--once", action="store_true", help="run a single tick and exit; for smoke tests"
    )
    return parser.parse_args(argv)


def build_session_source(use_logind: bool) -> SessionSource:
    if use_logind and is_linux():
        return LogindSessionSource()
    log.warning(
        "running without logind: no session is being watched and nothing will be "
        "enforced. This is a development mode and must never be how BioKids runs "
        "on a machine with a child on it."
    )
    return FakeSessionSource()


async def main_async(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    set_config(config)

    for directory in (config.paths.data_dir, config.paths.log_dir, config.paths.run_dir):
        directory.mkdir(parents=True, exist_ok=True)

    database = Database(config)
    bus = EventBus()
    watcher = SessionWatcher(build_session_source(not args.no_logind))
    enforcer = Enforcer(config)
    supervisor = Supervisor(
        database=database,
        bus=bus,
        watcher=watcher,
        enforcer=enforcer,
        # Guarded: the wall time the daemon uses cannot be moved under it
        # without NTP's agreement (D-172).
        clock=GuardedClock(
            SystemClock(), threshold_seconds=config.daemon.clock_jump_threshold_seconds
        ),
        config=config,
    )
    server = DaemonServer(supervisor, bus, config)
    shell_server = ShellServer(supervisor, bus, config)

    await supervisor.start()
    await server.start()
    await shell_server.start()
    # Type=notify: systemd counts the daemon as started - and starts pinging
    # for the watchdog - only once it is actually supervising.
    notify.ready()

    if args.once:
        await supervisor.tick()
        await shell_server.stop()
        await server.stop()
        await supervisor.stop()
        return 0

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop_event.set)

    try:
        await run_forever(supervisor, stop_event)
    finally:
        notify.stopping()
        await shell_server.stop()
        await server.stop()
        await supervisor.stop()
        database.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:  # pragma: no cover
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
