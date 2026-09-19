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
import logging
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


async def set_brightness(percent: int) -> bool:
    """A laptop backlight if there is one, otherwise a software gamma ramp.

    `xrandr --brightness` does not touch the backlight; it fades the output,
    which on a desktop monitor is the only thing available and is exactly what a
    child means by "too bright".
    """
    percent = clamp(percent, low=20)
    if await _run(["brightnessctl", "--quiet", "set", f"{percent}%"]):
        return True
    output = await _primary_output()
    if output and await _run(
        ["xrandr", "--output", output, "--brightness", f"{percent / 100:.2f}"]
    ):
        return True
    log.info("no brightness control available on this machine")
    return False


async def _primary_output() -> str | None:
    """The connected display `xrandr` would change. None if X is not there."""
    if shutil.which("xrandr") is None:
        return None
    try:
        process = await asyncio.create_subprocess_exec(
            "xrandr",
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
