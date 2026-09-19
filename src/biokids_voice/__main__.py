"""`biokids-voice` — the systemd user unit's entry point.

Nothing here decides anything; it reads the config, opens the socket and waits
for `SIGTERM`. The shutdown path matters more than the startup path: systemd
sends `SIGTERM` when the child logs out, and a service that is killed in the
middle of a line has to put the audio it ducked back before it goes, or the
next person to use the machine finds their music at a third of its volume with
no explanation.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from biokids_core import __version__
from biokids_core.config import Config, get_config, set_config
from biokids_core.errors import BioKidsError
from biokids_voice.service import default_socket_path, run_service

LOG = logging.getLogger("biokids_voice")

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="biokids-voice",
        description="Zio and Sio, the BioKids text-to-speech companions.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to config.toml (default: /etc/biokids/config.toml)",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        default=None,
        help="unix socket to listen on (default: $XDG_RUNTIME_DIR/biokids-voice.sock)",
    )
    parser.add_argument(
        "--no-audio",
        action="store_true",
        help="show cards but play nothing, for a machine with no sound card",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity (default: INFO)",
    )
    parser.add_argument("--version", action="version", version=f"biokids {__version__}")
    return parser.parse_args(argv)


def load_config(path: Path | None) -> Config:
    config = Config.load(path) if path is not None else get_config()
    set_config(config)
    return config


async def serve(config: Config, socket_path: Path, *, audio: bool) -> None:
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    await run_service(config, socket_path=socket_path, audio=audio, stop=stop)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """`SIGTERM` and `SIGINT` both mean "finish up", never "die now"."""
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        received = getattr(signal, name, None)
        if received is None:  # pragma: no cover - not every platform has both
            continue
        # Windows has no add_signal_handler; this project is developed there and
        # runs on Linux (D-12).
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(received, stop.set)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format=LOG_FORMAT)

    try:
        config = load_config(args.config)
    except BioKidsError as exc:
        LOG.error("configuration is unusable: %s", exc)
        return 2

    if not config.voice.enabled:
        LOG.info("voice is disabled in the config; cards will appear without audio")

    socket_path = args.socket or default_socket_path(config)
    try:
        asyncio.run(serve(config, socket_path, audio=not args.no_audio))
    except KeyboardInterrupt:  # pragma: no cover - interactive runs only
        return 0
    except BioKidsError as exc:
        LOG.error("biokids-voice stopped: %s", exc)
        return 1
    LOG.info("biokids-voice stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
