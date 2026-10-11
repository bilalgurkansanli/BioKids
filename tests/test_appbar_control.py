"""D-208: the launcher asks the running app bar, not a new Python, to bring an app back.

A second tap on an open app's tile used to start `python3 -m biokids_shell.appbar
--bring-back`, which loaded GTK from nothing each time: 1.5 s on the owner's VM,
and on a busy one a timeout the launcher took for "not running". The bar
already has libwnck loaded, so it answers on a socket of its own. These pin the
protocol - one line in, stamped with when it was sent so that a bar that read it
late does not act on an old tap, the helper's exit code out, nothing but this
child's own app scopes ever named - the bar's side without GTK (the work is
handed to a stand-in for the main loop), and the launcher's side against a stand-in
connection, so all of it runs on the Windows host too. A real socket is used on
Linux only.
"""

from __future__ import annotations

import asyncio
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from biokids_shell.appbar import EXIT_BAD_UNIT, EXIT_NOT_FOUND, EXIT_UNAVAILABLE, control

GAME = "biokids-app-frozen-bubble-3.scope"
UID = 1001

linux_only = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or sys.platform == "win32",
    reason="unix sockets are a Linux thing (D-12)",
)


# ------------------------------------------------------------------ the lines


def test_a_request_names_an_apps_scope_and_when_it_was_sent() -> None:
    assert control.request_line("biokids-app-frozen-bubble-3", sent_ns=81234567890123) == (
        b"bring-back biokids-app-frozen-bubble-3.scope 81234567890123\n"
    )
    before = time.monotonic_ns()
    verb, scope, stamp = control.request_line(GAME).decode().rstrip("\n").split(" ")
    assert (verb, scope) == ("bring-back", GAME)
    assert before <= int(stamp) <= time.monotonic_ns(), "stamped with CLOCK_MONOTONIC, now"


@pytest.mark.parametrize(
    "unit", ["biokids-shell", "biokids-voice.service", "", "biokids-app-x-1.scope; reboot"]
)
def test_nothing_but_an_app_can_be_asked_for(unit: str) -> None:
    with pytest.raises(ValueError, match="not a BioKids app"):
        control.request_line(unit)


def test_the_bar_reads_the_scope_and_the_stamp_from_a_request() -> None:
    assert control.parse_request(f"bring-back {GAME} 81234567890123".encode()) == (
        control.Request(GAME, 81234567890123)
    )
    assert control.parse_request(control.request_line(GAME, sent_ns=7)) == (GAME, 7)
    # Without a stamp, as old as when it is read.
    assert control.parse_request(f"bring-back {GAME}".encode()) == (GAME, None)
    assert control.parse_request(b"bring-back biokids-app-frozen-bubble-3\r") == (GAME, None)


@pytest.mark.parametrize(
    "line",
    [
        b"",
        b"bring-back",
        b"bring-back biokids-shell.service",
        b"stop biokids-app-frozen-bubble-3.scope",
        b"bring-back  biokids-app-frozen-bubble-3.scope",
        b"bring-back biokids-app-frozen-bubble-3.scope extra",
        b"bring-back biokids-app-frozen-bubble-3.scope -5",
        b"bring-back biokids-app-frozen-bubble-3.scope 12a",
        b"bring-back biokids-app-frozen-bubble-3.scope 1 2",
        b"bring-back biokids-app-frozen-bubble-3.scope ",
        b"bring-back biokids-app-frozen-bubble-3.scope " + b"9" * 21,
        b"bring-back 81234567890123 biokids-app-frozen-bubble-3.scope",
        b"bring-back biokids-app-../../x.scope",
        "bring-back biokids-app-çizim-1.scope".encode(),
        b"bring-back biokids-app-" + b"a" * 300 + b".scope",
    ],
)
def test_the_bar_refuses_what_is_not_a_request(line: bytes) -> None:
    assert control.parse_request(line) is None


def test_an_answer_is_one_of_the_helpers_exit_codes() -> None:
    for code in (0, EXIT_NOT_FOUND, EXIT_BAD_UNIT, EXIT_UNAVAILABLE):
        assert control.parse_answer(control.answer_line(code)) == code
    assert control.answer_line(70) == f"{EXIT_UNAVAILABLE}\n".encode()
    for garbage in (b"", b"yes\n", b"-1\n", b"9\n", b"0000000\n", b"\xff\n"):
        assert control.parse_answer(garbage) is None


def test_the_socket_is_in_the_childs_runtime_directory(tmp_path: Path) -> None:
    assert control.socket_path({"XDG_RUNTIME_DIR": "/run/user/1001"}) == Path(
        "/run/user/1001/biokids-appbar.sock"
    )
    assert control.socket_path({}, uid=987654) is None, "never anywhere shared"


# ------------------------------------------------------------- the bar's end


class _MainLoop:
    """`GLib.idle_add`: runs the work at once, later, or never."""

    def __init__(self, *, runs: bool = True) -> None:
        self.runs = runs
        self.waiting: list[Callable[[], bool]] = []

    def __call__(self, work: Callable[[], bool]) -> int:
        if self.runs:
            assert work() is False, "an idle callback that returns True runs forever"
        else:
            self.waiting.append(work)
        return 1


class _Windows:
    """`ui.bring_back`, as the main loop would run it."""

    def __init__(self, answer: int = 0) -> None:
        self.answer = answer
        self.asked: list[str] = []
        self.threads: list[str] = []

    def __call__(self, scope: str) -> int:
        self.asked.append(scope)
        self.threads.append(threading.current_thread().name)
        return self.answer


def _server(
    windows: _Windows, loop: _MainLoop | None = None, **kwargs: Any
) -> control.ControlServer:
    return control.ControlServer(
        Path("unused.sock"), windows, loop or _MainLoop(), uid=UID, **kwargs
    )


def test_the_bar_shows_the_app_and_says_so() -> None:
    windows = _Windows(0)
    answer = _server(windows).answer(f"bring-back {GAME}".encode(), UID)
    assert answer == b"0\n"
    assert windows.asked == [GAME]


def test_an_app_with_no_window_is_said_to_have_none() -> None:
    assert _server(_Windows(EXIT_NOT_FOUND)).answer(f"bring-back {GAME}".encode(), UID) == (
        f"{EXIT_NOT_FOUND}\n".encode()
    )


def test_another_users_request_is_refused() -> None:
    windows = _Windows(0)
    server = _server(windows)
    assert server.answer(f"bring-back {GAME}".encode(), 0) == b"2\n"
    assert windows.asked == []
    assert not server.from_this_child(0)
    assert server.from_this_child(UID)
    assert server.from_this_child(None), "where the kernel cannot say, the 0600 socket did"


def test_a_request_for_what_is_not_an_app_is_refused() -> None:
    windows = _Windows(0)
    assert _server(windows).answer(b"bring-back biokids-shell.service", UID) == b"2\n"
    assert windows.asked == []


def test_a_failure_on_the_main_loop_is_could_not_look() -> None:
    def broken(_scope: str) -> int:
        raise RuntimeError("libwnck went away")

    server = control.ControlServer(Path("x"), broken, _MainLoop(), uid=UID)
    assert server.answer(f"bring-back {GAME}".encode(), UID) == f"{EXIT_UNAVAILABLE}\n".encode()


def test_a_main_loop_that_does_not_get_to_it_is_could_not_look(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(control, "MAIN_LOOP_WAIT_S", 0.05)
    windows = _Windows(0)
    loop = _MainLoop(runs=False)
    server = _server(windows, loop)
    assert server.answer(f"bring-back {GAME}".encode(), UID) == f"{EXIT_UNAVAILABLE}\n".encode()
    # When it does get there, soon after, the window still comes back: the
    # child tapped for it, and a launcher that heard nothing started nothing.
    assert loop.waiting[0]() is False
    assert windows.asked == [GAME]


def _answered_after(
    line: bytes, *, read_at: float, main_loop_after: float
) -> tuple[bytes, _Windows]:
    """The bar's answer to `line`, read at `read_at` on its clock, when its main
    loop gets to the request `main_loop_after` seconds later."""
    now = [read_at]
    windows = _Windows(0)
    loop = _MainLoop(runs=False)
    server = _server(windows, loop, clock=lambda: now[0])
    seen: list[bytes] = []
    thread = threading.Thread(target=lambda: seen.append(server.answer(line, UID)))
    thread.start()
    while not loop.waiting and thread.is_alive():
        thread.join(0.01)
    now[0] += main_loop_after
    if loop.waiting:
        loop.waiting[0]()
    thread.join(5)
    return seen[0], windows


def _stamped(seconds: float) -> bytes:
    """A request the launcher sent at `seconds` on the shared monotonic clock."""
    return control.request_line(GAME, sent_ns=int(seconds * 1e9)).rstrip(b"\n")


def test_a_request_the_main_loop_reaches_much_later_is_dropped() -> None:
    answer, windows = _answered_after(
        f"bring-back {GAME}".encode(), read_at=100.0, main_loop_after=control.STALE_AFTER_S + 1
    )
    assert answer == f"{EXIT_UNAVAILABLE}\n".encode()
    assert windows.asked == [], "a window jumped to the front long after the tap"


def test_a_request_that_waited_for_a_frozen_bar_is_as_old_as_the_tap() -> None:
    """The bar was stopped with the request in its socket's queue. Thawed, it
    read the line and its main loop got to it soon after - but counted from
    the read, a window jumped to the front long after the child tapped. It is
    counted from the launcher's stamp."""
    stale = control.STALE_AFTER_S + 1
    answer, windows = _answered_after(
        _stamped(1000.0 - stale), read_at=1000.0, main_loop_after=0
    )
    assert answer == f"{EXIT_UNAVAILABLE}\n".encode()
    assert windows.asked == []

    # Old at the read already, or only by the time the main loop looks.
    half = control.STALE_AFTER_S / 2 + 1
    answer, windows = _answered_after(
        _stamped(1000.0 - half), read_at=1000.0, main_loop_after=half
    )
    assert answer == f"{EXIT_UNAVAILABLE}\n".encode()
    assert windows.asked == []


def test_a_request_sent_a_moment_ago_is_acted_on() -> None:
    answer, windows = _answered_after(_stamped(998.0), read_at=1000.0, main_loop_after=1.0)
    assert answer == b"0\n"
    assert windows.asked == [GAME]


def test_a_stamp_from_the_future_does_not_keep_a_request_fresh() -> None:
    """No later than the read: a stamp an hour ahead is not an hour of grace."""
    answer, windows = _answered_after(
        _stamped(1000.0 + 3600), read_at=1000.0, main_loop_after=control.STALE_AFTER_S + 1
    )
    assert answer == f"{EXIT_UNAVAILABLE}\n".encode()
    assert windows.asked == []


def test_the_work_is_done_on_the_main_loop_not_the_sockets_thread() -> None:
    """libwnck and GTK are touched from the main loop alone (ui.py)."""
    windows = _Windows(0)
    loop = _MainLoop(runs=False)
    server = _server(windows, loop)
    seen: list[bytes] = []
    worker = threading.Thread(
        target=lambda: seen.append(server.answer(f"bring-back {GAME}".encode(), UID)),
        name="appbar-control",
    )
    worker.start()
    while not loop.waiting:
        worker.join(0.01)
    loop.waiting.pop()()  # the main loop, here the test's own thread
    worker.join(5)
    assert seen == [b"0\n"]
    assert windows.threads == [threading.current_thread().name]


class _Connection:
    """A socket, as `read_line` reads it."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)

    def recv(self, size: int) -> bytes:
        assert size > 0
        return self.chunks.pop(0)[:size] if self.chunks else b""


def test_a_request_is_read_up_to_its_newline() -> None:
    connection: Any = _Connection(b"bring-back biokids-app-", b"x-1.scope\nmore")
    assert control.read_line(connection) == b"bring-back biokids-app-x-1.scope"


def test_an_endless_line_is_not_read_for_ever() -> None:
    connection: Any = _Connection(*(b"a" * 100 for _ in range(10)))
    line = control.read_line(connection)
    assert len(line) <= control.MAX_LINE_BYTES + 1
    assert control.parse_request(line) is None


def test_something_else_at_the_sockets_path_is_left_alone(tmp_path: Path) -> None:
    path = tmp_path / control.SOCKET_NAME
    path.write_text("a parent's file", encoding="utf-8")
    assert control._clear_stale(path) is False
    assert path.read_text(encoding="utf-8") == "a parent's file"
    assert control._clear_stale(tmp_path / "nothing-here.sock") is True


# ----------------------------------------------------------- the launcher's


class _Reader:
    def __init__(self, line: bytes | None, delay: float = 0.0) -> None:
        self.line, self.delay = line, delay

    async def readline(self) -> bytes:
        if self.line is None:
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay)
        return self.line or b""


class _Writer:
    def __init__(self) -> None:
        self.sent = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.sent += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _opener(reader: _Reader, writer: _Writer, opened: list[str]) -> Any:
    async def open_connection(path: str) -> tuple[Any, Any]:
        opened.append(path)
        return reader, writer

    return open_connection


async def test_the_launcher_asks_one_line_and_reads_one(tmp_path: Path) -> None:
    writer, opened = _Writer(), []
    before = time.monotonic_ns()
    answer = await control.ask_bar(
        tmp_path / "bar.sock",
        "biokids-app-frozen-bubble-3",
        1.0,
        open_connection=_opener(_Reader(b"0\n"), writer, opened),
    )
    assert answer == 0
    assert opened == [str(tmp_path / "bar.sock")]
    assert writer.sent.endswith(b"\n") and writer.sent.count(b"\n") == 1
    request = control.parse_request(writer.sent.rstrip(b"\n"))
    assert request is not None and request.scope == GAME
    assert request.sent_ns is not None and before <= request.sent_ns <= time.monotonic_ns()
    assert writer.closed


@pytest.mark.parametrize("error", [FileNotFoundError, ConnectionRefusedError, PermissionError])
async def test_no_bar_listening_is_no_bar(tmp_path: Path, error: type[OSError]) -> None:
    """The bar is not running - crashed, restarting, never started: the helper."""

    async def nobody(_path: str) -> Any:
        raise error("nobody there")

    with pytest.raises(control.BarNotRunning):
        await control.ask_bar(tmp_path / "bar.sock", GAME, 1.0, open_connection=nobody)
    with pytest.raises(control.BarNotRunning):
        await control.ask_bar(None, GAME, 1.0, open_connection=nobody)


async def test_a_bar_too_busy_to_answer_is_no_answer_in_time(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    began = loop.time()
    writer: _Writer = _Writer()
    answer = await control.ask_bar(
        tmp_path / "bar.sock", GAME, 0.2, open_connection=_opener(_Reader(None), writer, [])
    )
    assert answer is None
    assert loop.time() - began < 1.0
    assert writer.closed


async def test_a_bar_too_busy_to_take_the_connection_is_no_answer(tmp_path: Path) -> None:
    async def stuck(_path: str) -> Any:
        await asyncio.Event().wait()

    assert (
        await control.ask_bar(tmp_path / "bar.sock", GAME, 0.1, open_connection=stuck) is None
    )


async def test_an_answer_that_is_not_one_is_no_answer(tmp_path: Path) -> None:
    for line in (b"", b"ok\n", b"42\n"):
        answer = await control.ask_bar(
            tmp_path / "bar.sock",
            GAME,
            1.0,
            open_connection=_opener(_Reader(line), _Writer(), []),
        )
        assert answer is None


async def test_the_launcher_names_nothing_but_an_app(tmp_path: Path) -> None:
    opened: list[str] = []
    answer = await control.ask_bar(
        tmp_path / "bar.sock",
        "biokids-shell",
        1.0,
        open_connection=_opener(_Reader(b"0\n"), _Writer(), opened),
    )
    assert answer == EXIT_BAD_UNIT
    assert opened == []


# --------------------------------------------------------- a real socket


@linux_only
async def test_the_bar_and_the_launcher_over_a_real_socket(tmp_path: Path) -> None:
    import os

    path = tmp_path / control.SOCKET_NAME
    path.write_bytes(b"")  # not a socket: left alone, and no channel
    windows = _Windows(0)
    server = control.ControlServer(path, windows, _MainLoop(), uid=os.getuid())
    assert server.start() is False
    path.unlink()

    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()  # a bar that was killed left its socket behind
    assert server.start() is True
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert await control.ask_bar(path, "biokids-app-frozen-bubble-3", 2.0) == 0
        assert windows.asked == [GAME]
        windows.answer = EXIT_NOT_FOUND
        assert await control.ask_bar(path, GAME, 2.0) == EXIT_NOT_FOUND
    finally:
        server.close()
    assert not path.exists(), "the socket outlived the bar"
    with pytest.raises(control.BarNotRunning):
        await control.ask_bar(path, GAME, 1.0)
