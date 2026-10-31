"""Volume and brightness, the only two settings a child gets (D-98).

There is no settings application in the child's session. These two live as
controls in the launcher because a child who cannot turn the sound down will
turn the computer off instead, and because a screen that is too bright at 19:00
is a real complaint a five-year-old cannot otherwise fix.

Everything else — display, network, keyboard, appearance — stays hidden. Each
control tries the tools most likely to be present and gives up quietly: a
machine with no `brightnessctl` should show a slider that does nothing rather
than a launcher that will not start.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
from collections.abc import Sequence

log = logging.getLogger(__name__)


async def _run(argv: Sequence[str]) -> bool:
    if shutil.which(argv[0]) is None:
        return False
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
    except OSError:
        log.debug("%s failed", argv[0], exc_info=True)
        return False
    return process.returncode == 0


def clamp(value: int, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, value))


async def set_volume(percent: int) -> bool:
    """PipeWire first, then PulseAudio, then ALSA. One of them is always there."""
    percent = clamp(percent)
    candidates: list[list[str]] = [
        ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", f"{percent / 100:.2f}"],
        ["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"],
        ["amixer", "-q", "sset", "Master", f"{percent}%"],
    ]
    for argv in candidates:
        if await _run(argv):
            return True
    log.info("no volume tool available; the slider is decorative on this machine")
    return False


VOLUME_READERS: tuple[tuple[tuple[str, ...], re.Pattern[str], float], ...] = (
    (
        ("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"),
        re.compile(r"Volume:\s*([0-9.]+)"),
        100.0,
    ),
    (("pactl", "get-sink-volume", "@DEFAULT_SINK@"), re.compile(r"(\d+)%"), 1.0),
    (("amixer", "sget", "Master"), re.compile(r"\[(\d+)%\]"), 1.0),
)
"""Where the volume is read from, in `set_volume`'s order, and how to read it."""
READ_TIMEOUT = 2.0
"""Seconds a volume tool gets before the home screen gives up on it."""


async def get_volume() -> int | None:
    """The sink's volume as a percentage, so the slider starts where the sound is.

    It started at 70 whatever the sound was: a child who had turned it down
    and came back to the home screen moved the slider a little and got a blast.
    None when no tool answers; the slider then keeps its default.
    """
    for argv, pattern, scale in VOLUME_READERS:
        out = await _output(argv)
        if out is None:
            continue
        match = pattern.search(out)
        if match is not None:
            return clamp(round(float(match.group(1)) * scale))
    return None


async def _output(argv: Sequence[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(process.communicate(), READ_TIMEOUT)
    except (OSError, TimeoutError):
        return None
    if process.returncode != 0:
        return None
    return out.decode("utf-8", "replace")


_brightness_lock = asyncio.Lock()
_brightness_wanted: int | None = None
_brightness_method: str = ""
"""`backlight` or `xrandr` once one has worked, so the other is not tried again."""
_brightness_output: str | None = None


async def set_brightness(percent: int) -> bool:
    """A laptop backlight if there is one, otherwise a software gamma ramp.

    `xrandr --brightness` does not touch the backlight; it fades the output,
    which on a desktop monitor is the only thing available and is exactly what a
    child means by "too bright".

    One change at a time, and only the latest (D-254). A slider dragged on a
    real desktop sent a dozen changes a second; each ran xrandr, and an xrandr
    that probes the monitors (without `--current`) holds the whole X server -
    taps and drawing - for tens to hundreds of milliseconds. The taps then came
    late, all at once. Requests that wait here while another is applied are
    answered by it: the level they asked for is already behind the latest one.
    """
    global _brightness_wanted
    _brightness_wanted = clamp(percent, low=20)
    async with _brightness_lock:
        level = _take_brightness_wanted()
        if level is None:
            return True
        return await _apply_brightness(level)


def _take_brightness_wanted() -> int | None:
    """The latest level asked for, once: None when a request that waited
    before this one has applied it already. A function, so mypy does not carry
    the assignment above across the `await` that lets the others run."""
    global _brightness_wanted
    level, _brightness_wanted = _brightness_wanted, None
    return level


async def _apply_brightness(percent: int) -> bool:
    global _brightness_method, _brightness_output
    if _brightness_method != "xrandr" and await _run(
        ["brightnessctl", "--quiet", "set", f"{percent}%"]
    ):
        _brightness_method = "backlight"
        return True
    for attempt in range(2):
        if _brightness_output is None or attempt:
            # A monitor plugged in or out since: ask again, once.
            _brightness_output = await _primary_output()
        if _brightness_output and await _run(
            [
                "xrandr",
                "--current",
                "--output",
                _brightness_output,
                "--brightness",
                f"{percent / 100:.2f}",
            ]
        ):
            _brightness_method = "xrandr"
            return True
    log.info("no brightness control available on this machine")
    return False


_XRANDR_BRIGHTNESS = re.compile(r"Brightness:\s*([0-9.]+)")


async def get_brightness() -> int | None:
    """The brightness as a percentage, read the way `set_brightness` writes it.

    The backlight first, then the output's software brightness. None when
    neither answers. Asked once, before the evening dims the screen (D-214), so
    that what is put back the next time is the child's own level rather than a
    guess.
    """
    out = await _output(["brightnessctl", "-m"])
    if out is not None:
        # "intel_backlight,backlight,1200,50%,2400": the field with the sign.
        for field in out.splitlines()[0].split(",") if out.strip() else []:
            if field.endswith("%") and field[:-1].isdigit():
                return clamp(int(field[:-1]), low=20)
    out = await _output(["xrandr", "--verbose", "--current"])
    if out is not None:
        match = _XRANDR_BRIGHTNESS.search(out)
        if match is not None:
            return clamp(round(float(match.group(1)) * 100), low=20)
    return None


async def _primary_output() -> str | None:
    """The connected display `xrandr` would change. None if X is not there."""
    if shutil.which("xrandr") is None:
        return None
    try:
        # --current: the outputs as X knows them, without probing every
        # connector again, which stalls the X server (D-254).
        process = await asyncio.create_subprocess_exec(
            "xrandr",
            "--current",
            "--listactivemonitors",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await process.communicate()
    except OSError:
        return None
    for line in out.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if parts and parts[0].rstrip(":").isdigit():
            return parts[-1]
    return None


LEVELS_WAIT = 1.0
"""The longest the home screen waits for the first reading of the levels."""


class Levels:
    """The sound and the light as the home screen shows them (D-254).

    Every "back home" read both with a few subprocesses, one after the other,
    each allowed two seconds: on a real machine a sound tool slow to answer
    held the home screen with it, and the tap that asked for it seemed lost.
    Now the last level seen or set is shown at once and read again behind the
    page for the next time; only the first home screen of a session waits, and
    not for long.
    """

    def __init__(self) -> None:
        self.known: dict[str, int] = {}
        self._reading: asyncio.Task[None] | None = None

    async def _read(self) -> None:
        volume, brightness = await asyncio.gather(get_volume(), get_brightness())
        if volume is not None:
            self.known["volume"] = volume
        if brightness is not None:
            self.known["brightness"] = brightness

    async def read(self) -> tuple[int | None, int | None]:
        if self._reading is None or self._reading.done():
            self._reading = asyncio.create_task(self._read())
        if not self.known:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._reading), LEVELS_WAIT)
        return self.known.get("volume"), self.known.get("brightness")

    def set(self, name: str, value: int) -> None:
        self.known[name] = value
