"""This screen, for the parent watching it (D-253).

The launcher runs in the child's session, with its display: it takes the
pictures, and only while the daemon says a parent is watching. One about every
`EVERY_S`, made small (`WIDTH_PX` across) and sent to the daemon, which keeps
the latest in memory for the panel and nothing else. The child sees that they
are being watched for as long as this runs (`app.py`, the app bar).
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from collections.abc import Awaitable, Callable
from typing import Final

log = logging.getLogger(__name__)
_told: list[bool] = []
"""Whether a failed picture was logged already: once, not every second."""

EVERY_S: Final = 1.0
"""About one picture a second: enough to see what the child is doing."""
WIDTH_PX: Final = 960
JPEG_QUALITY: Final = 60

Sender = Callable[[str], Awaitable[bool]]


def capture() -> bytes | None:
    """The whole screen as a JPEG, `WIDTH_PX` across at most; None when it
    cannot be taken (no display, or a Pillow without its X11 support)."""
    try:
        from PIL import Image, ImageGrab

        picture = ImageGrab.grab(xdisplay=None)
    except Exception as exc:  # no PIL, no XCB, no display: no picture
        if not _told:
            _told.append(True)
            log.warning("the screen could not be taken: %s", exc)
        return None
    if picture.width > WIDTH_PX:
        height = round(picture.height * WIDTH_PX / picture.width)
        picture = picture.resize((WIDTH_PX, height), Image.Resampling.BILINEAR)
    out = io.BytesIO()
    picture.convert("RGB").save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


class LiveSender:
    """Takes and sends pictures while a parent watches; one task at a time."""

    def __init__(
        self,
        send: Sender,
        take: Callable[[], bytes | None] = capture,
        every: float = EVERY_S,
    ) -> None:
        self.send = send
        self.take = take
        self.every = every
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            log.info("a parent is watching this screen")
            self._task = asyncio.create_task(self._run(), name="live-view")

    def stop(self) -> None:
        if self._task is not None and not self._task.done():
            log.info("nobody is watching this screen any more")
            self._task.cancel()

    async def _run(self) -> None:
        failures = 0
        while True:
            picture = await asyncio.to_thread(self.take)
            if picture is None:
                failures += 1
                if failures >= 3:
                    return
            else:
                failures = 0
                if not await self.send(base64.b64encode(picture).decode("ascii")):
                    return
            await asyncio.sleep(self.every)
