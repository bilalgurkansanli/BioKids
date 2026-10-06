"""Telling systemd the daemon is alive (`sd_notify`), without a library.

The unit is `Type=notify` with `WatchdogSec=`. The daemon says `READY=1` once
the supervisor has started, and `WATCHDOG=1` after every tick that completed.
A tick that hangs - a D-Bus call that never returns, a subprocess that never
exits - therefore stops the pings, and systemd kills the process, runs the
lockdown in `ExecStopPost=` and restarts it.

Before this a hung tick looked healthy: the process was alive, systemd saw
`active (running)`, `Restart=` and `OnFailure=` never fired, and the child sat
at a machine that was enforcing nothing (principle 3).

The protocol is one datagram to the socket named in `$NOTIFY_SOCKET`; an
address starting with `@` is in the abstract namespace. Everything here is a
no-op when the variable is unset, which is every run outside systemd.
"""

from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)

_AF_UNIX: int | None = getattr(socket, "AF_UNIX", None)


def notify(state: str) -> bool:
    """Send one state line, such as `READY=1`. True when it was delivered."""
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address or _AF_UNIX is None:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(_AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode("utf-8"))
    except OSError as exc:
        log.warning("could not notify systemd (%s): %s", state, exc)
        return False
    return True


def ready() -> bool:
    return notify("READY=1")


def watchdog() -> bool:
    return notify("WATCHDOG=1")


def stopping() -> bool:
    return notify("STOPPING=1")
