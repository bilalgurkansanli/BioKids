"""`python3 -m biokids_shell.appbar` - the app bar, started by the launcher (D-197).

Two ways to run it:

* with no arguments, the bar itself: it prints `ready` once its window exists
  and runs until the launcher stops it;
* `--bring-back biokids-app-<slug>-<n>`, once: show that app's window again and
  exit. The launcher runs this when a child taps the tile of an app that is
  already open, so the tap brings back the game they sent to the background
  instead of starting a second copy of it.

GTK is imported here, inside `main`, and nowhere else: a machine without it
gets exit code 3 and the title bar's close button, not a crash loop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from types import ModuleType

from biokids_core.config import Config
from biokids_core.i18n import DEFAULT_LOCALE, normalise_locale, translator
from biokids_shell.appbar import (
    EXIT_BAD_UNIT,
    EXIT_UNAVAILABLE,
    READY,
    is_app_scope,
    own_uid,
    scope_unit,
)
from biokids_shell.client import ShellClient

log = logging.getLogger("biokids.appbar")

LOCALE_TIMEOUT_S = 3.0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="biokids-appbar",
        description="BioKids app bar: home, the app's volume and close, over every app.",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument(
        "--bring-back",
        metavar="UNIT",
        default=None,
        help="show the window of this biokids-app scope again, then exit",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("BIOKIDS_LOG_LEVEL", "info"),
        choices=["debug", "info", "warning", "error"],
    )
    return parser.parse_args(argv)


def load_ui() -> ModuleType:
    """The GTK half. Raises ImportError or ValueError where GTK 3, libwnck 3 or
    their introspection data are not installed."""
    from biokids_shell.appbar import ui

    return ui


def child_locale(config: Config) -> str:
    """The child's language, for the names a screen reader gives the buttons.

    The buttons themselves carry no words. Turkish when the daemon cannot say.
    """

    async def ask() -> str:
        client = ShellClient(config.paths.shell_socket, timeout=LOCALE_TIMEOUT_S)
        state = await client.state()
        return normalise_locale(str((state or {}).get("locale", DEFAULT_LOCALE)))

    try:
        return asyncio.run(asyncio.wait_for(ask(), LOCALE_TIMEOUT_S + 1))
    except (TimeoutError, OSError):
        return DEFAULT_LOCALE


def announce_ready() -> None:
    print(READY, flush=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    scope: str | None = None
    if args.bring_back is not None:
        scope = scope_unit(args.bring_back)
        if not is_app_scope(scope):
            log.error("%r is not a BioKids app", args.bring_back)
            return EXIT_BAD_UNIT
    try:
        ui = load_ui()
    except (ImportError, ValueError) as exc:
        log.warning(
            "the app bar needs GTK 3 and libwnck 3 (python3-gi, gir1.2-gtk-3.0, "
            "gir1.2-wnck-3.0): %s",
            exc,
        )
        return EXIT_UNAVAILABLE
    if scope is not None:
        code: int = ui.bring_back(scope, uid=own_uid())
        return code
    config = Config.load(args.config)
    tr = translator(child_locale(config))
    code = ui.run_bar(tr, uid=own_uid(), ready=announce_ready)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
