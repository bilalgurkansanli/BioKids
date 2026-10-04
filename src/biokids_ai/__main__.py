"""Entry point for `biokids-ai`, the assistant's service (D-165)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

from biokids_ai.service import AssistantService
from biokids_core.config import Config, set_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="biokids-ai", description=__doc__)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = Config.load(args.config)
    set_config(config)

    async def run() -> None:
        service = AssistantService(config)
        task = asyncio.create_task(service.serve())
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(sig, task.cancel)
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
