"""`biokids-panel` — the entry point.

Binds to `127.0.0.1` from the config and refuses anything else. That refusal is
the whole of D-33 in one function: the moment a LAN address is possible, someone
forwards a port to it, and a parental-control panel on the open internet is a
worse outcome than no panel at all.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from pathlib import Path

import uvicorn

from biokids_core.config import Config, set_config
from biokids_panel.app import create_app

log = logging.getLogger("biokids.panel")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="biokids-panel",
        description="BioKids parent panel. Runs as root, from systemd.",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument(
        "--host", default=None, help="override the bind address (loopback only)"
    )
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--log-level",
        default=os.environ.get("BIOKIDS_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error"],
    )
    parser.add_argument("--reload", action="store_true", help="development only")
    return parser.parse_args(argv)


def check_loopback(host: str) -> str:
    """Refuse to bind anywhere a router could reach.

    `0.0.0.0` is the one that matters: it is what a well-meaning person types
    when the panel will not open from their phone, and it is exactly the thing
    Tailscale exists to make unnecessary.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host in {"localhost", "localhost4", "localhost6"}:
            return host
        raise SystemExit(
            f"refusing to bind to {host!r}: the panel listens on loopback only. "
            "Use `tailscale serve` for access from a phone (docs/DECISIONS.md D-33)."
        ) from None
    if not address.is_loopback:
        raise SystemExit(
            f"refusing to bind to {host}: the panel listens on loopback only. "
            "Do not port-forward it; use `tailscale serve` instead (D-33)."
        )
    return host


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    config = Config.load(args.config)
    set_config(config)

    host = check_loopback(args.host or config.panel.host)
    port = args.port or config.panel.port

    log.info("panel on http://%s:%s", host, port)
    uvicorn.run(
        create_app(config),
        host=host,
        port=port,
        log_level=args.log_level,
        access_log=False,
        # No access log: it would record which pages a parent opened and when,
        # which is exactly the sort of thing BioKids promises not to keep.
        server_header=False,
        date_header=True,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
