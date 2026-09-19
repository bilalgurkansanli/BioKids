"""Clocks.

Time accounting never touches the wall clock. A child who opens the settings app
and moves the date forward three hours must gain nothing, and an NTP correction
must not silently hand out or take away an hour of screen time. So:

* **`CLOCK_BOOTTIME`** measures how much time has passed. It is monotonic and it
  keeps running across suspend, so it cannot be moved by anyone.
* **`CLOCK_MONOTONIC`** measures how much of that time the machine was *awake*.
  The difference between the two deltas is exactly how long the machine was
  suspended — which is how `TimeLedger` implements T2.4 without having to ask
  logind for a number it would then have to trust.
* **The wall clock** is used for two things and no others: deciding which
  calendar day's quota applies, and evaluating time windows. A jump larger than
  ten minutes is written to the audit log and raised to the parent; it never
  changes the ledger.

`boot_id` comes from `/proc/sys/kernel/random/boot_id`. A `boot_id` we have not
seen before means a new epoch: `BOOTTIME` has restarted from zero and the totals
have to be read back from the database rather than inferred from the clock.
"""

from __future__ import annotations

import datetime as dt
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, cast

BOOT_ID_PATH: Final = Path("/proc/sys/kernel/random/boot_id")

_CLOCK_BOOTTIME: Final[int | None] = getattr(time, "CLOCK_BOOTTIME", None)
"""None on any non-Linux development host. Production is Linux (D-12).

Reached through `getattr` rather than directly because typeshed only declares
`time.CLOCK_BOOTTIME` on Linux, and `mypy --strict` runs on the Windows host
this project is developed from.
"""


@dataclass(frozen=True, slots=True)
class ClockReading:
    """One consistent sample of all three clocks.

    Taken together, because reading them at three different moments is how
    off-by-a-tick bugs get in.
    """

    boot: float
    """Seconds since boot, including suspend (`CLOCK_BOOTTIME`)."""
    monotonic: float
    """Seconds since boot, excluding suspend (`CLOCK_MONOTONIC`)."""
    wall: dt.datetime
    """Local wall-clock time, naive, in the machine's timezone."""
    boot_id: str

    def awake_since(self, previous: ClockReading) -> float:
        """Seconds the machine was awake between `previous` and this reading.

        Across a reboot this is meaningless — both counters restarted — so the
        caller must compare `boot_id` first. `TimeLedger` does.
        """
        boot_delta = self.boot - previous.boot
        monotonic_delta = self.monotonic - previous.monotonic
        suspended = max(0.0, boot_delta - monotonic_delta)
        return max(0.0, boot_delta - suspended)

    def wall_delta(self, previous: ClockReading) -> float:
        return (self.wall - previous.wall).total_seconds()


class Clock(Protocol):
    """What the ledger needs from a clock. Implemented for real and for tests."""

    def read(self) -> ClockReading: ...


class SystemClock:
    """The real thing."""

    def __init__(self, boot_id_path: Path = BOOT_ID_PATH) -> None:
        self._boot_id_path = boot_id_path
        self._boot_id = self._read_boot_id()

    def read(self) -> ClockReading:
        return ClockReading(
            boot=self.boottime(),
            monotonic=time.monotonic(),
            wall=dt.datetime.now().replace(microsecond=0),
            boot_id=self._boot_id,
        )

    @staticmethod
    def boottime() -> float:
        if _CLOCK_BOOTTIME is not None:
            clock_gettime = cast(
                "Callable[[int], float]", time.clock_gettime  # type: ignore[attr-defined]
            )
            return clock_gettime(_CLOCK_BOOTTIME)
        # A development host without CLOCK_BOOTTIME (Windows, macOS). Suspend is
        # then indistinguishable from awake time, which is fine for writing code
        # and is never the case on a machine BioKids actually protects.
        return time.monotonic()

    def _read_boot_id(self) -> str:
        try:
            return self._boot_id_path.read_text(encoding="ascii").strip()
        except OSError:
            # No /proc: synthesise a per-process id so a new run looks like a new
            # boot, which is the safe interpretation.
            return f"no-procfs-{uuid.uuid4()}"

    @property
    def boot_id(self) -> str:
        return self._boot_id


class FakeClock:
    """A clock the tests drive by hand.

    `dev/fakeclock.py` uses this to run a three-hour session in three seconds.
    Every advance is explicit, which is the only way the ledger's behaviour
    around suspend, reboot and clock tampering can be asserted rather than hoped
    for.
    """

    def __init__(
        self,
        *,
        boot: float = 0.0,
        monotonic: float = 0.0,
        wall: dt.datetime | None = None,
        boot_id: str = "boot-0",
    ) -> None:
        self.boot = boot
        self.monotonic = monotonic
        self.wall = wall or dt.datetime(2026, 9, 21, 16, 0, 0)
        self.boot_id = boot_id

    def read(self) -> ClockReading:
        return ClockReading(
            boot=self.boot,
            monotonic=self.monotonic,
            wall=self.wall,
            boot_id=self.boot_id,
        )

    # ---------------------------------------------------------- movements

    def advance(self, seconds: float) -> None:
        """Normal passage of time: every clock moves together."""
        self.boot += seconds
        self.monotonic += seconds
        self.wall += dt.timedelta(seconds=seconds)

    def suspend(self, seconds: float) -> None:
        """The lid closed. BOOTTIME and the wall clock move; MONOTONIC does not."""
        self.boot += seconds
        self.wall += dt.timedelta(seconds=seconds)

    def jump_wall(self, seconds: float) -> None:
        """Someone changed the system clock. Nothing else moves."""
        self.wall += dt.timedelta(seconds=seconds)

    def reboot(self, *, off_for: float = 0.0, boot_id: str | None = None) -> None:
        """The machine was off for `off_for` seconds and came back.

        Both counters restart at zero and `boot_id` changes, which is exactly
        what the ledger must notice.
        """
        self.wall += dt.timedelta(seconds=off_for)
        self.boot = 0.0
        self.monotonic = 0.0
        self.boot_id = boot_id or f"boot-{uuid.uuid4().hex[:8]}"


def read_boot_id(path: Path = BOOT_ID_PATH) -> str | None:
    """The current boot id, or None when `/proc` is not available."""
    try:
        return path.read_text(encoding="ascii").strip()
    except OSError:
        return None


def is_linux() -> bool:
    return os.name == "posix" and _CLOCK_BOOTTIME is not None
