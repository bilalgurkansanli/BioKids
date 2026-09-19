"""The event bus.

Fans events out to the panel's server-sent events, the child launcher's
countdown ring, the voice service and, later, the AI worker. Four consumers with
very different failure modes, so the contract is deliberately blunt:

**A slow subscriber is dropped, not waited for.** Each subscriber has a bounded
queue. When it fills, the oldest event is discarded and a counter is bumped.
Blocking the daemon's tick because a browser tab stopped reading would turn a
cosmetic problem into an enforcement problem.

**Events are facts, not commands.** Nothing a subscriber does can change what the
daemon decided. The panel showing a stale counter is a display bug; it can never
be a supervision bug.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from biokids_core.ipc import Event, EventKind

log = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 64


@dataclass
class Subscriber:
    queue: asyncio.Queue[Event]
    child_id: int | None = None
    dropped: int = 0
    label: str = ""

    def wants(self, event: Event) -> bool:
        """A subscriber filtered to one child still receives daemon-wide events."""
        if self.child_id is None:
            return True
        return event.child_id is None or event.child_id == self.child_id


@dataclass
class EventBus:
    subscribers: list[Subscriber] = field(default_factory=list)
    queue_size: int = DEFAULT_QUEUE_SIZE
    history: list[Event] = field(default_factory=list)
    history_size: int = 50
    """A short replay buffer, so a panel that reconnects mid-countdown does not
    show an empty screen until the next tick."""

    def subscribe(self, child_id: int | None = None, label: str = "") -> Subscriber:
        subscriber = Subscriber(
            queue=asyncio.Queue(maxsize=self.queue_size),
            child_id=child_id,
            label=label,
        )
        self.subscribers.append(subscriber)
        log.debug("subscriber %s attached (child=%s)", label or id(subscriber), child_id)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        if subscriber in self.subscribers:
            self.subscribers.remove(subscriber)
            if subscriber.dropped:
                log.info(
                    "subscriber %s detached after dropping %s events",
                    subscriber.label or id(subscriber),
                    subscriber.dropped,
                )

    def publish(
        self,
        kind: EventKind | str,
        *,
        child_id: int | None = None,
        **payload: Any,
    ) -> Event:
        event = Event(
            kind=str(kind),
            child_id=child_id,
            payload=payload,
            ts=dt.datetime.now().isoformat(timespec="seconds"),
        )
        self._remember(event)
        for subscriber in self.subscribers:
            if not subscriber.wants(event):
                continue
            self._offer(subscriber, event)
        return event

    def _offer(self, subscriber: Subscriber, event: Event) -> None:
        try:
            subscriber.queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest and take the newest: for a countdown, the latest
            # number is the only one that matters.
            with_drop = subscriber.queue
            with contextlib.suppress(asyncio.QueueEmpty):  # full then empty is a race
                with_drop.get_nowait()
                with_drop.task_done()
            subscriber.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                with_drop.put_nowait(event)

    def _remember(self, event: Event) -> None:
        self.history.append(event)
        if len(self.history) > self.history_size:
            del self.history[: len(self.history) - self.history_size]

    def replay(self, child_id: int | None = None) -> list[Event]:
        if child_id is None:
            return list(self.history)
        return [e for e in self.history if e.child_id in (None, child_id)]

    async def stream(self, subscriber: Subscriber) -> AsyncIterator[Event]:
        while True:
            event = await subscriber.queue.get()
            subscriber.queue.task_done()
            yield event
