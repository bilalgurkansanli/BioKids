"""The child's screen, live, for a parent who asked - and the child knows (D-253).

The owner asked to see the child's screen as it is, from the panel and the
phone. CLAUDE.md principles 1 and 2 were "no screen is ever captured" and
"supervision is visible"; the owner changed the first for this and kept the
second whole:

* Only while a parent's panel page is asking. The panel asks for a frame about
  once a second; a watch ends `KEEPALIVE_S` after the last ask, so a closed page
  or a phone put away stops it by itself.
* The child's own launcher takes the pictures, inside the child's session, and
  only while it is told it is being watched. The daemon never connects to the
  child's display.
* Nothing is kept: the latest frame, in this process's memory, replaced by the
  next and dropped when the watch ends. No file, no database row, no history.
* The child sees "👁 Annen ekranını görüyor" on the launcher and on the app bar
  for as long as the watch lasts. The audit log says a parent watched, and when.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

KEEPALIVE_S: Final = 10.0
"""A watch lasts this long after the panel last asked for a frame."""
STALE_FRAME_S: Final = 5.0
"""A watch whose launcher sent nothing for this long is announced again: the
launcher may have restarted and missed the first announcement."""
FRAME_MAX_BYTES: Final = 600_000
"""A frame's JPEG, at most. The launcher sends about 60-150 kB."""
JPEG_MAGIC: Final = b"\xff\xd8\xff"


@dataclass(slots=True)
class Watch:
    by: str
    """Who is watching, as the child sees it: the parent's name in the panel."""
    until: float
    frame: str = ""
    """The latest picture, base64 JPEG; empty until the launcher sends one."""
    at: float = 0.0
    """When `frame` came."""
    announced: float = 0.0
    """When the child's launcher was last told to send pictures."""


class LiveView:
    """Who is being watched, and the one latest picture of each."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._watches: dict[int, Watch] = {}

    def ask(self, child_id: int, by: str) -> tuple[Watch, bool]:
        """A panel asking for a frame: starts the watch or keeps it going.

        True as the second value when the child's launcher must be told to send
        pictures: the watch has just started, or its launcher has been silent.
        """
        now = self._clock()
        watch = self._watches.get(child_id)
        started = watch is None or watch.until < now
        if watch is None or started:
            watch = Watch(by=by, until=0.0)
            self._watches[child_id] = watch
        watch.until = now + KEEPALIVE_S
        if by:
            watch.by = by
        quiet = now - max(watch.at, watch.announced) > STALE_FRAME_S
        announce = started or quiet
        if announce:
            watch.announced = now
        return watch, announce

    def watching(self, child_id: int) -> Watch | None:
        watch = self._watches.get(child_id)
        return watch if watch is not None and watch.until >= self._clock() else None

    def push(self, child_id: int, frame: str) -> bool:
        """A picture from the child's launcher. False when nobody is watching
        any more: the launcher stops taking them. A picture that is not a JPEG
        of a sensible size is dropped, and the watch goes on."""
        watch = self.watching(child_id)
        if watch is None:
            return False
        try:
            raw = base64.b64decode(frame, validate=True)
        except (binascii.Error, ValueError):
            return True
        if 0 < len(raw) <= FRAME_MAX_BYTES and raw.startswith(JPEG_MAGIC):
            watch.frame = frame
            watch.at = self._clock()
        return True

    def age(self, watch: Watch) -> float | None:
        """Seconds since the frame came; None before the first."""
        return self._clock() - watch.at if watch.frame else None

    def stop(self, child_id: int) -> bool:
        """The parent pressed "stop". True when there was a watch to end."""
        return self._watches.pop(child_id, None) is not None

    def expire(self) -> list[int]:
        """End the watches nobody asked about lately, and drop their pictures."""
        now = self._clock()
        ended = [child_id for child_id, watch in self._watches.items() if watch.until < now]
        for child_id in ended:
            del self._watches[child_id]
        return ended
