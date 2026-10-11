"""The app bar's control channel: "bring this app back", asked of the bar itself (D-208).

A tap on the tile of an app that is already open shows that app's window again
(D-197). Until D-208 the launcher did that by running the bar's code once more,
`python3 -m biokids_shell.appbar --bring-back <unit>`: a new Python that loaded
GTK and libwnck from nothing for every tap. On the owner's VM, busy starting a
KDE game, that took 1.5 s at best; at worst it ran out of its five seconds, the
launcher took "could not look" for "not running" and started a second copy, then
a third, of an app that was still opening.

The bar is already running, with libwnck loaded and every window known. So it
listens on a socket of its own, `$XDG_RUNTIME_DIR/biokids-appbar.sock`, mode
0600 in a directory only the child can enter, and answers one line with one
line:

    bring-back biokids-app-palapeli-3.scope 81234567890123\\n   ->   0\\n

The number is when the launcher asked, `time.monotonic_ns()`: CLOCK_MONOTONIC is
one clock for every process on the machine, so the bar can tell how old the tap
is however long the line waited to be read. A bar that was frozen reads it
late, and a window jumping to the front long after the tap would be a surprise,
not an answer (`STALE_AFTER_S`). A line without the number is as old as when it
was read.

The answer is the helper's exit code, so both ways read the same: 0 shown,
`EXIT_NOT_FOUND` no window, `EXIT_BAD_UNIT` not an app of this child's,
`EXIT_UNAVAILABLE` could not look. The window is found and shown on the bar's
GTK main loop, the only thread that touches libwnck; the socket is served by a
thread of its own that hands the request over and waits for the answer.

Nothing but a `biokids-app-*.scope` is ever named, and the bar shows only the
windows of such a scope under this child's own `user@<uid>.service`
(`ui.bring_back`). A connection from another user is refused before its line is
read. The launcher asks with a short timeout and falls back to the helper only
when no bar is listening (`ask_bar` raises `BarNotRunning`); a bar that is there
but slow is "could not look", which never starts a second copy.

This module needs no GTK: the launcher imports it, and the tests run it anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import stat
import struct
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Final, NamedTuple, cast

from biokids_shell.appbar import (
    EXIT_BAD_UNIT,
    EXIT_NOT_FOUND,
    EXIT_UNAVAILABLE,
    is_app_scope,
    scope_unit,
)

log = logging.getLogger(__name__)

SOCKET_NAME: Final = "biokids-appbar.sock"
BRING_BACK: Final = "bring-back"
"""The one request there is."""

MAX_LINE_BYTES: Final = 256
"""A request is a verb and a unit name; anything longer is not one."""
READ_TIMEOUT_S: Final = 1.0
"""How long a connection has to say its line before the bar stops listening to it."""
MAIN_LOOP_WAIT_S: Final = 4.0
"""How long the socket's thread waits for the main loop to have looked."""
STALE_AFTER_S: Final = 10.0
"""A request the main loop reaches later than this after it was sent is not
acted on: a window jumping to the front long after the tap would be a
surprise, not an answer."""
MAX_STAMP_DIGITS: Final = 20
"""`time.monotonic_ns()` as a decimal: an unsigned 64-bit number at most."""
ANSWERS: Final = frozenset({0, EXIT_NOT_FOUND, EXIT_BAD_UNIT, EXIT_UNAVAILABLE})

_AF_UNIX: int | None = getattr(socket, "AF_UNIX", None)
"""Linux has it; the Windows development host does not (D-12)."""


# ------------------------------------------------------------------- where


def socket_path(
    environ: Mapping[str, str] | None = None, uid: int | None = None
) -> Path | None:
    """`$XDG_RUNTIME_DIR/biokids-appbar.sock`; None where there is no runtime directory.

    systemd sets the variable for a user unit, and the bar is started by the
    launcher's, so both name the same place. Without it, `/run/user/<uid>`
    when that exists. Never anywhere shared: a socket there could be another
    user's.
    """
    environ = os.environ if environ is None else environ
    runtime = environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return Path(runtime) / SOCKET_NAME
    if uid is None:
        getuid = getattr(os, "getuid", None)
        uid = getuid() if getuid is not None else None
    if uid is None:
        return None
    fallback = Path(f"/run/user/{uid}")
    return fallback / SOCKET_NAME if fallback.is_dir() else None


# ---------------------------------------------------------------- the lines


class Request(NamedTuple):
    """A request as the bar reads it."""

    scope: str
    sent_ns: int | None
    """When the launcher sent it, `time.monotonic_ns()`; None from a line without it."""


def request_line(unit: str, sent_ns: int | None = None) -> bytes:
    """What the launcher sends to bring `unit` back, stamped with when it is
    sent (now, unless `sent_ns` says otherwise). Raises ValueError for
    anything that is not a BioKids app's scope."""
    scope = scope_unit(unit.strip())
    if not is_app_scope(scope):
        raise ValueError(f"{unit!r} is not a BioKids app")
    stamp = time.monotonic_ns() if sent_ns is None else sent_ns
    return f"{BRING_BACK} {scope} {stamp}\n".encode("ascii")


def parse_request(line: bytes) -> Request | None:
    """The app scope a request names, and when it was sent if it says; None for
    anything that is not a request."""
    if len(line) > MAX_LINE_BYTES:
        return None
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        return None
    parts = text.rstrip("\r\n").split(" ")
    if len(parts) not in (2, 3) or parts[0] != BRING_BACK:
        return None
    sent_ns: int | None = None
    if len(parts) == 3:
        stamp = parts[2]
        if not (stamp.isdigit() and len(stamp) <= MAX_STAMP_DIGITS):
            return None
        sent_ns = int(stamp)
    scope = scope_unit(parts[1])
    return Request(scope, sent_ns) if is_app_scope(scope) else None


def answer_line(code: int) -> bytes:
    """One of the helper's exit codes, as a line; anything else is "could not look"."""
    return f"{code if code in ANSWERS else EXIT_UNAVAILABLE}\n".encode("ascii")


def parse_answer(line: bytes) -> int | None:
    """The bar's answer, or None when it is not one."""
    text = line.strip()
    if not text.isdigit() or len(text) > 3:
        return None
    code = int(text)
    return code if code in ANSWERS else None


# ---------------------------------------------------------- the launcher's


class BarNotRunning(Exception):
    """Nothing listens: the bar is not running, or not on this machine's kind of socket."""


Connection = tuple[asyncio.StreamReader, asyncio.StreamWriter]
Opener = Callable[[str], Awaitable[Connection]]


def _unix_opener() -> Opener | None:
    opener = getattr(asyncio, "open_unix_connection", None)
    # typeshed declares open_unix_connection only on POSIX, and mypy runs on
    # the Windows development host. Production is Linux (D-12).
    return cast("Opener", opener) if opener is not None else None


async def ask_bar(
    path: Path | None,
    unit: str,
    within: float,
    *,
    open_connection: Opener | None = None,
) -> int | None:
    """Ask the running bar to bring `unit` back; its answer, or None when it did
    not give one `within` seconds.

    Raises `BarNotRunning` when there is no bar to ask - no socket, or nobody
    listening on it - which is when the launcher runs the helper instead.
    """
    try:
        request = request_line(unit)
    except ValueError:
        return EXIT_BAD_UNIT
    opener = open_connection or _unix_opener()
    if path is None or opener is None:
        raise BarNotRunning("no control socket")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    try:
        reader, writer = await asyncio.wait_for(opener(str(path)), within)
    except TimeoutError:
        # A bar so busy it does not even take the connection is there, and
        # may still bring the window back when it gets to it.
        log.info("the app bar did not take the connection within %s s", within)
        return None
    except OSError as exc:
        raise BarNotRunning(str(exc)) from exc
    try:
        writer.write(request)
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), max(0.0, deadline - loop.time()))
    except TimeoutError:
        log.info("the app bar did not answer within %s s", within)
        return None
    except (OSError, ValueError) as exc:
        log.info("the app bar's answer could not be read: %s", exc)
        return None
    finally:
        writer.close()
        with contextlib.suppress(OSError, RuntimeError):
            await writer.wait_closed()
    answer = parse_answer(line)
    if answer is None:
        log.warning("the app bar answered %r, which is not an answer", line[:40])
    return answer


# --------------------------------------------------------------- the bar's

BringBack = Callable[[str], int]
"""Show the windows of this app scope again: an exit code. Runs on the main loop."""
CallSoon = Callable[[Callable[[], bool]], Any]
"""Have the main loop run this once (`GLib.idle_add`); it returns False."""


def peer_uid(connection: socket.socket) -> int | None:
    """The user at the other end of a Unix socket, where the kernel says (Linux)."""
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None
    try:
        data = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", data)
    except (OSError, struct.error):
        return None
    return int(uid)


def read_line(connection: socket.socket) -> bytes:
    """The request, up to its newline, MAX_LINE_BYTES at most."""
    data = b""
    while b"\n" not in data and len(data) <= MAX_LINE_BYTES:
        chunk = connection.recv(MAX_LINE_BYTES + 1 - len(data))
        if not chunk:
            break
        data += chunk
    return data.split(b"\n", 1)[0] if b"\n" in data else data


class ControlServer:
    """The bar's end: a thread that answers each request with the main loop's help."""

    def __init__(
        self,
        path: Path,
        bring_back: BringBack,
        call_soon: CallSoon,
        *,
        uid: int | None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.bring_back = bring_back
        self.call_soon = call_soon
        self.uid = uid
        self.clock = clock
        self._listener: socket.socket | None = None
        self._inode: int | None = None
        self._thread: threading.Thread | None = None
        self._closed = threading.Event()

    # ------------------------------------------------------------ answers

    def from_this_child(self, peer: int | None) -> bool:
        """The kernel's word on who connected; where it cannot say, the 0600
        socket in the child's own 0700 directory already did."""
        return self.uid is None or peer is None or peer == self.uid

    def answer(self, line: bytes, peer: int | None) -> bytes:
        """The answer to one request: checked here, acted on by the main loop."""
        if not self.from_this_child(peer):
            log.warning("a request from user %s was refused", peer)
            return answer_line(EXIT_BAD_UNIT)
        read_at = self.clock()
        request = parse_request(line)
        if request is None:
            log.warning("a request that names no app was refused: %r", line[:60])
            return answer_line(EXIT_BAD_UNIT)
        # The launcher's stamp, never later than now: a stamp from the future
        # would keep a request fresh for ever.
        sent = read_at if request.sent_ns is None else min(read_at, request.sent_ns / 1e9)
        scope = request.scope
        code = self.on_main_loop(lambda: self.bring_back(scope), sent=sent)
        return answer_line(EXIT_UNAVAILABLE if code is None else code)

    def on_main_loop(self, work: Callable[[], int], *, sent: float | None = None) -> int | None:
        """Run `work` on the main loop and wait for it; None if it did not run in time.

        Not at all when the request is older than `STALE_AFTER_S` by the time
        the main loop gets to it, counted from `sent` (this clock's seconds;
        now when not given): a request that waited while the bar was frozen
        is as old as the tap, not as the moment the bar read it.
        """
        received = self.clock() if sent is None else sent
        if self.clock() - received > STALE_AFTER_S:
            log.info("a bring-back was read too long after it was sent; not acted on")
            return EXIT_UNAVAILABLE
        finished = threading.Event()
        result: list[int] = []

        def run() -> bool:
            try:
                if self.clock() - received > STALE_AFTER_S:
                    log.info("a bring-back reached the main loop too late; not acted on")
                    result.append(EXIT_UNAVAILABLE)
                else:
                    result.append(work())
            except Exception:  # a failure here must not take the bar down
                log.exception("bringing an app back failed")
                result.append(EXIT_UNAVAILABLE)
            finally:
                finished.set()
            return False

        self.call_soon(run)
        if not finished.wait(MAIN_LOOP_WAIT_S):
            log.warning("the app bar's main loop did not look within %s s", MAIN_LOOP_WAIT_S)
            return None
        return result[0] if result else EXIT_UNAVAILABLE

    # ------------------------------------------------------------- socket

    def start(self) -> bool:
        """Listen. False, and the launcher keeps using the helper, when it cannot."""
        if _AF_UNIX is None or not _clear_stale(self.path):
            return False
        listener = socket.socket(_AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            # The runtime directory is 0700 and the child's own; 0600 as well,
            # as every BioKids socket in it is (the voice service's too).
            self.path.chmod(0o600)
            listener.listen(8)
            self._inode = self.path.stat().st_ino
        except OSError as exc:
            log.warning("the app bar cannot listen at %s: %s", self.path, exc)
            listener.close()
            return False
        self._listener = listener
        self._thread = threading.Thread(target=self._serve, name="appbar-control", daemon=True)
        self._thread.start()
        log.info("the app bar listens at %s", self.path)
        return True

    def _serve(self) -> None:
        listener = self._listener
        while listener is not None and not self._closed.is_set():
            try:
                connection, _ = listener.accept()
            except OSError as exc:
                if self._closed.is_set():
                    return
                log.debug("accept failed: %s", exc)
                time.sleep(0.1)
                continue
            with connection:
                connection.settimeout(READ_TIMEOUT_S)
                peer = peer_uid(connection)
                try:
                    if not self.from_this_child(peer):
                        connection.sendall(self.answer(b"", peer))  # not even read
                        continue
                    connection.sendall(self.answer(read_line(connection), peer))
                except OSError as exc:  # gone, or too slow to say its line
                    log.debug("a control connection ended early: %s", exc)

    def close(self) -> None:
        """Stop listening and take the socket away, if it is still this bar's."""
        self._closed.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)
            listener.close()
        if self._inode is not None:
            with contextlib.suppress(OSError):
                if self.path.stat().st_ino == self._inode:
                    self.path.unlink()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


def _clear_stale(path: Path) -> bool:
    """A socket left by a bar that was killed is removed; anything else is left alone."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("%s cannot be looked at: %s", path, exc)
        return False
    if not stat.S_ISSOCK(mode):
        log.warning("%s exists and is not a socket; the app bar will not listen there", path)
        return False
    try:
        path.unlink()
    except OSError as exc:
        log.warning("the old socket at %s cannot be removed: %s", path, exc)
        return False
    return True


def serve(
    bring_back: BringBack, call_soon: CallSoon, *, uid: int | None
) -> ControlServer | None:
    """Start the bar's control channel; None when there is nowhere to put it."""
    if _AF_UNIX is None:
        return None
    path = socket_path(uid=uid)
    if path is None:
        log.warning("no runtime directory; a tile brings an open app back the slow way")
        return None
    server = ControlServer(path, bring_back, call_soon, uid=uid)
    return server if server.start() else None
