"""Playing a line, and getting out of the way of everything else.

Two jobs, and the second one is the hard one.

**Playback** is a subprocess: `pw-play` on a PipeWire desktop, `paplay` where
the PulseAudio compatibility layer is what is installed, `aplay` as the ALSA
floor. Three commands rather than a library because none of them needs a Python
binding, and because a child's session should not carry an audio stack in
process just to say eleven words a day.

**Ducking** is the interesting part. VOICE.md §6 says lower other audio to 30%
for the duration, then restore — muting a cartoon mid-sentence is jarring in a
way that a moment of quieter cartoon is not. So before a line starts, every
other audio stream's volume is read and remembered, and afterwards it is put
back. "Afterwards" has to mean *always*: if the player is killed, if the task
is cancelled, if the service is asked to stop mid-line, a child must not be
left with a video playing at a third of its volume and no way to explain it.
That is why the restore runs as a shielded task in a `finally` — a cancellation
propagates, and the volumes still go back.

Stream discovery uses `pw-dump`, which is JSON, rather than the human-readable
output of `wpctl status`, which is a tree drawn with box characters and is not
a contract. Volumes are read and written through `wpctl` at both ends so the
numbers are in one scale; mixing `pw-dump`'s raw cubic values with `wpctl`'s
linear ones is the obvious bug here and it would be inaudible until it wasn't.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from shutil import which
from typing import Any, Final, Protocol

from biokids_core.config import Config, get_config

LOG = logging.getLogger(__name__)

PLAYBACK_COMMANDS: Final[tuple[tuple[str, ...], ...]] = (
    ("pw-play",),
    ("paplay",),
    ("aplay", "-q"),
)
"""In order of preference. The first one on PATH wins."""

STREAM_CLASS: Final = "Stream/Output/Audio"
NODE_TYPE: Final = "PipeWire:Interface:Node"
CONTROL_TIMEOUT_S: Final = 3.0
"""A volume control that has not answered in three seconds is not going to."""

_VOLUME = re.compile(r"Volume:\s*([0-9]*\.?[0-9]+)")

Spawn = Callable[..., Awaitable["asyncio.subprocess.Process"]]
"""Injectable so the queue and the ducking can be tested without a sound card."""


# ------------------------------------------------------------ volume control


class VolumeControl(Protocol):
    """The two things ducking needs from an audio server."""

    async def streams(self) -> dict[int, float]: ...

    async def set_volume(self, node_id: int, volume: float) -> None: ...


class NullVolumeControl:
    """No other audio exists. Used headless, and wherever PipeWire is not."""

    async def streams(self) -> dict[int, float]:
        return {}

    async def set_volume(self, node_id: int, volume: float) -> None:
        return None


class PipeWireVolumeControl:
    """Every other application's output stream, through `pw-dump` and `wpctl`."""

    def __init__(self, *, timeout: float = CONTROL_TIMEOUT_S) -> None:
        self._timeout = timeout

    def available(self) -> bool:
        return which("pw-dump") is not None and which("wpctl") is not None

    async def streams(self) -> dict[int, float]:
        if not self.available():
            return {}
        dump = await self._run("pw-dump")
        if dump is None:
            return {}
        volumes: dict[int, float] = {}
        for node_id in _output_stream_ids(dump):
            volume = await self._volume_of(node_id)
            if volume is not None:
                volumes[node_id] = volume
        return volumes

    async def set_volume(self, node_id: int, volume: float) -> None:
        clamped = f"{max(0.0, min(1.5, volume)):.3f}"
        await self._run("wpctl", "set-volume", str(node_id), clamped)

    async def _volume_of(self, node_id: int) -> float | None:
        out = await self._run("wpctl", "get-volume", str(node_id))
        if out is None:
            return None
        match = _VOLUME.search(out)
        return float(match.group(1)) if match else None

    async def _run(self, *argv: str) -> str | None:
        """Never raise at a caller: a missing mixer must not stop a warning."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except (OSError, ValueError, TimeoutError) as exc:
            LOG.debug("%s failed: %s", argv[0], exc)
            return None
        if proc.returncode != 0:
            return None
        return stdout.decode("utf-8", "replace")


def _output_stream_ids(dump: str) -> list[int]:
    """Application playback streams from a `pw-dump`, ignoring everything else."""
    try:
        objects = json.loads(dump)
    except json.JSONDecodeError:
        LOG.debug("pw-dump did not return JSON")
        return []
    if not isinstance(objects, list):
        return []

    found: list[int] = []
    for item in objects:
        if not isinstance(item, dict) or item.get("type") != NODE_TYPE:
            continue
        info = item.get("info")
        props = info.get("props") if isinstance(info, dict) else None
        if not isinstance(props, dict) or props.get("media.class") != STREAM_CLASS:
            continue
        node_id = item.get("id")
        if isinstance(node_id, int):
            found.append(node_id)
    return found


# -------------------------------------------------------------------- duck


class AudioDucker:
    """Lower everything else for the length of a line, then put it back."""

    def __init__(self, control: VolumeControl, duck_percent: int) -> None:
        self._control = control
        self._factor = max(0.0, min(100, duck_percent)) / 100.0
        self._restoring: asyncio.Future[None] | None = None

    @asynccontextmanager
    async def ducked(self) -> AsyncIterator[dict[int, float]]:
        saved = await self._lower()
        try:
            yield saved
        finally:
            # Shielded rather than plain: when the surrounding task is cancelled
            # — the service is stopping, the line was superseded — the restore
            # still runs to completion in the background while the cancellation
            # propagates as it should.
            self._restoring = asyncio.ensure_future(self._raise_back(saved))
            await asyncio.shield(self._restoring)

    async def _lower(self) -> dict[int, float]:
        try:
            saved = await self._control.streams()
        except OSError as exc:  # pragma: no cover - mixer went away mid-call
            LOG.debug("could not read stream volumes: %s", exc)
            return {}
        for node_id, volume in saved.items():
            await self._control.set_volume(node_id, volume * self._factor)
        return saved

    async def _raise_back(self, saved: dict[int, float]) -> None:
        for node_id, volume in saved.items():
            with contextlib.suppress(OSError):
                await self._control.set_volume(node_id, volume)

    async def aclose(self) -> None:
        """Wait for a restore that is still in flight, at shutdown."""
        if self._restoring is not None:
            with contextlib.suppress(asyncio.CancelledError, OSError):
                await self._restoring
            self._restoring = None


# ------------------------------------------------------------------ players


class Player(Protocol):
    """What the service needs in order to make a noise."""

    def available(self) -> bool: ...

    async def play(self, path: Path) -> bool: ...

    async def stop(self) -> None: ...

    async def aclose(self) -> None: ...


class NullPlayer:
    """Playback for a machine with no sound card, and for the test suite.

    It records what it was asked to play and refuses to play two lines at once.
    "Never interrupt" (VOICE.md §6) is a property of the queue, but asserting it
    here means the queue cannot quietly lose it.
    """

    def __init__(self, *, delay: float = 0.0) -> None:
        self.played: list[Path] = []
        self._delay = delay
        self._busy = False

    def available(self) -> bool:
        return True

    async def play(self, path: Path) -> bool:
        if self._busy:
            raise RuntimeError(f"{path} started while another line was still playing")
        self._busy = True
        self.played.append(path)
        try:
            await self._elapse(path)
        finally:
            self._busy = False
        return True

    async def _elapse(self, path: Path) -> None:
        """How long this line "takes". A subclass overrides it to hold the queue."""
        if self._delay:
            await asyncio.sleep(self._delay)

    async def stop(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class SilentPlayer(NullPlayer):
    """A machine with no audio at all, and what `--no-audio` turns this into.

    It answers `False`, which is the same answer a missing `pw-play` gives, so
    the service falls through to the card alone and says `banner`. Reporting
    success would tell the panel that a child heard a warning they did not, and
    a parent reading "spoken" for a line nobody heard is exactly the silent
    failure principle 3 forbids.
    """

    def available(self) -> bool:
        return False

    async def play(self, path: Path) -> bool:
        await super().play(path)
        return False


class PipeWirePlayer:
    """`pw-play` (or the next best thing), with everything else ducked."""

    def __init__(
        self,
        *,
        ducker: AudioDucker | None = None,
        commands: Sequence[tuple[str, ...]] = PLAYBACK_COMMANDS,
        spawn: Spawn | None = None,
    ) -> None:
        self._ducker = ducker or AudioDucker(NullVolumeControl(), 100)
        self._commands = tuple(commands)
        self._spawn = spawn or asyncio.create_subprocess_exec
        self._command: tuple[str, ...] | None = None
        self._resolved = False
        self._process: asyncio.subprocess.Process | None = None

    @classmethod
    def from_config(cls, config: Config | None = None) -> PipeWirePlayer:
        config = config or get_config()
        control = PipeWireVolumeControl()
        if not control.available():
            LOG.info("PipeWire tools not found: lines will play without ducking")
        return cls(
            ducker=AudioDucker(
                control if control.available() else NullVolumeControl(),
                config.voice.duck_percent,
            )
        )

    def available(self) -> bool:
        return self.command() is not None

    def command(self) -> tuple[str, ...] | None:
        """The first playback command on PATH, resolved once."""
        if not self._resolved:
            self._command = next(
                (argv for argv in self._commands if which(argv[0]) is not None), None
            )
            self._resolved = True
            if self._command is None:
                LOG.warning(
                    "none of %s is installed: lines will appear on the card only",
                    ", ".join(argv[0] for argv in self._commands),
                )
        return self._command

    async def play(self, path: Path) -> bool:
        argv = self.command()
        if argv is None:
            return False

        async with self._ducker.ducked():
            try:
                process = await self._spawn(*argv, str(path))
            except OSError as exc:
                LOG.warning("could not start %s: %s", argv[0], exc)
                return False
            self._process = process
            try:
                code = await process.wait()
            finally:
                self._process = None
                _kill(process)
        if code != 0:
            LOG.warning("%s exited %s while playing %s", argv[0], code, path.name)
        return code == 0

    async def stop(self) -> None:
        """Cut a line short. Only the shutdown path uses this; the queue never does."""
        process = self._process
        if process is not None:
            _kill(process)

    async def aclose(self) -> None:
        await self.stop()
        await self._ducker.aclose()


def _kill(process: asyncio.subprocess.Process) -> None:
    """Best effort, and deliberately not awaited.

    This runs on the cancellation path, where every `await` raises. The loop's
    child watcher reaps the process; all we owe it is the signal.
    """
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()


def describe(player: Player) -> dict[str, Any]:
    """A line for the `status` op, so a parent can be told why it is quiet."""
    command = player.command() if isinstance(player, PipeWirePlayer) else None
    return {
        "kind": type(player).__name__,
        "available": player.available(),
        "command": command[0] if command else "",
    }
