"""The IPC protocol: framing everywhere, and the real transport on Linux.

The framing tests run anywhere. The socket tests are marked `linux_only` and
skipped on the Windows host this project is developed from — but CI runs on
Ubuntu, so the transport that carries every enforcement decision is exercised on
every push rather than only by hand on a VM.

That split matters. The handlers are covered by `test_daemon.py` and
`test_acceptance.py` without a socket; what is only testable here is the part
between them: a client connecting, a frame arriving whole, a slow reader, a
truncated line, a client that disappears mid-stream.
"""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import pytest

from biokids_core.errors import IpcError
from biokids_core.ipc import (
    MAX_LINE_BYTES,
    AsyncDaemonClient,
    DaemonClient,
    Event,
    EventKind,
    Op,
    Request,
    Response,
    ShellOp,
    decode_frame,
    encode_frame,
)

linux_only = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="unix sockets are a Linux thing (D-12)"
)


# ------------------------------------------------------------------ framing


def test_a_frame_is_one_line_with_no_stray_newlines() -> None:
    """The whole protocol rests on one frame being one line."""
    raw = encode_frame({"op": "x", "note": "a\nb\nc"})
    assert raw.count(b"\n") == 1
    assert raw.endswith(b"\n")


def test_a_frame_is_never_pretty_printed() -> None:
    raw = encode_frame({"a": 1, "b": 2})
    assert b", " not in raw
    assert b": " not in raw


def test_turkish_survives_a_round_trip_unescaped() -> None:
    """A child called Gökçe must not arrive as \\u00f6, in a log or anywhere else."""
    raw = encode_frame({"name": "Gökçe Şule Çınar"})
    assert "Gökçe Şule Çınar".encode() in raw
    assert decode_frame(raw)["name"] == "Gökçe Şule Çınar"


@pytest.mark.parametrize(
    "bad",
    [
        b"not json",
        b"[1, 2, 3]",  # valid JSON, wrong shape
        b'"a string"',
        b"123",
        b"",
    ],
)
def test_a_malformed_frame_raises_rather_than_returning_something_odd(bad: bytes) -> None:
    with pytest.raises(IpcError):
        decode_frame(bad)


def test_an_oversized_frame_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(IpcError, match="over the"):
        decode_frame(b"x" * (MAX_LINE_BYTES + 1))


def test_invalid_utf8_is_refused() -> None:
    with pytest.raises(IpcError, match="not valid utf-8"):
        decode_frame(b'{"a": "\xff\xfe"}')


def test_a_request_without_an_op_is_refused() -> None:
    with pytest.raises(IpcError, match="no op"):
        Request.decode(b'{"id": "1"}')


def test_args_must_be_an_object() -> None:
    with pytest.raises(IpcError, match="must be an object"):
        Request.decode(b'{"op": "ping", "args": [1, 2]}')


def test_every_frame_type_round_trips() -> None:
    request = Request(op="grant_time", id="7", args={"child_id": 1, "seconds": 600})
    assert Request.decode(request.encode()) == request

    response = Response(id="7", ok=True, data={"remaining_seconds": 600})
    assert Response.decode(response.encode()) == response

    failure = Response.failure("7", "error.unknown")
    assert Response.decode(failure.encode()) == failure

    event = Event(kind="tick", child_id=1, payload={"remaining_seconds": 42}, ts="now")
    assert Event.decode(event.encode()) == event


def test_an_empty_response_omits_its_empty_fields() -> None:
    """Keeps a tick frame small; the daemon sends one per child per ten seconds."""
    payload = json.loads(Response(id="1", ok=True).encode())
    assert payload == {"id": "1", "ok": True}


def test_the_serialiser_handles_the_types_that_actually_cross_the_socket() -> None:
    import datetime as dt

    from biokids_core.types import Category, SpeechRequest

    raw = encode_frame(
        {
            "category": Category.LEARNING,
            "when": dt.date(2026, 9, 21),
            "speak": SpeechRequest(event="warn_10", vars={"minutes": 10}),
            "seen": {3, 1, 2},
        }
    )
    decoded = decode_frame(raw)
    assert decoded["category"] == "learning"
    assert decoded["when"] == "2026-09-21"
    assert decoded["speak"]["event"] == "warn_10"
    assert decoded["seen"] == [1, 2, 3]


def test_a_decoded_response_defaults_to_not_ok() -> None:
    """A frame that forgot to say it succeeded did not succeed."""
    assert Response.decode(b'{"id": "1"}').ok is False


def test_the_two_vocabularies_do_not_overlap_by_accident() -> None:
    """A privileged op must not share a name with one the child may call.

    They are separate enums on separate sockets, so an overlap would not grant
    anything — but a name meaning two things is how it eventually would.
    """
    shared = {str(op) for op in Op} & {str(op) for op in ShellOp}
    assert shared == {"ping", "subscribe_events", "report_activity"}, (
        "these three are deliberately the same question asked on both sockets; "
        "anything else appearing here needs thinking about"
    )


# --------------------------------------------------------------- the socket


class _Echo:
    """A minimal server speaking the protocol, for testing the client."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.server: Any = None
        self.seen: list[Request] = []
        self.stream_events = 0

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, path=str(self.path))

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                request = Request.decode(line)
                self.seen.append(request)
                writer.write(Response(id=request.id, ok=True, data=request.args).encode())
                await writer.drain()
                if request.op == str(Op.SUBSCRIBE_EVENTS):
                    for index in range(3):
                        writer.write(
                            Event(
                                kind=str(EventKind.TICK), child_id=1, payload={"n": index}
                            ).encode()
                        )
                        await writer.drain()
                        self.stream_events += 1
                    return
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return


@pytest.fixture
async def echo(tmp_path: Path) -> Any:
    server = _Echo(tmp_path / "echo.sock")
    await server.start()
    yield server
    await server.stop()


@linux_only
async def test_the_async_client_reaches_a_real_socket(echo: _Echo) -> None:
    client = AsyncDaemonClient(echo.path)
    response = await client.call(Op.GRANT_TIME, child_id=1, seconds=600)
    assert response.ok
    assert response.data == {"child_id": 1, "seconds": 600}
    assert echo.seen[0].op == "grant_time"


@linux_only
async def test_the_async_client_streams_events_and_skips_the_acknowledgement(
    echo: _Echo,
) -> None:
    client = AsyncDaemonClient(echo.path)
    received = [event async for event in client.stream_events()]
    assert [e.payload["n"] for e in received] == [0, 1, 2]
    assert all(e.kind == "tick" for e in received)


@linux_only
async def test_a_missing_socket_is_an_ipc_error_not_a_crash(tmp_path: Path) -> None:
    client = AsyncDaemonClient(tmp_path / "nothing.sock", timeout=1)
    with pytest.raises(IpcError, match="cannot reach the daemon"):
        await client.call(Op.PING)


@linux_only
async def test_the_blocking_client_reaches_a_real_socket(echo: _Echo) -> None:
    """The panel uses this one, from a request handler, once per action."""
    client = DaemonClient(echo.path, timeout=5)
    response = await asyncio.to_thread(client.call, Op.PING)
    assert response.ok


@linux_only
async def test_available_is_false_rather_than_raising_when_nothing_is_listening(
    tmp_path: Path,
) -> None:
    """The panel calls this to decide whether to show the "not responding" banner."""
    client = DaemonClient(tmp_path / "gone.sock", timeout=1)
    assert await asyncio.to_thread(client.available) is False


@linux_only
async def test_a_frame_split_across_two_writes_still_arrives_whole(
    tmp_path: Path,
) -> None:
    """TCP-style fragmentation on a unix socket is rare and not impossible."""
    path = tmp_path / "split.sock"
    got: list[Request] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        got.append(Request.decode(line))
        writer.write(Response(id="1", ok=True).encode())
        await writer.drain()

    server = await asyncio.start_unix_server(serve, path=str(path))
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
        raw = Request(op="ping", id="1", args={"note": "x" * 4000}).encode()
        writer.write(raw[:100])
        await writer.drain()
        await asyncio.sleep(0.02)
        writer.write(raw[100:])
        await writer.drain()
        answer = await asyncio.wait_for(reader.readline(), timeout=5)
        writer.close()
        await writer.wait_closed()
    finally:
        server.close()
        await server.wait_closed()

    assert Response.decode(answer).ok
    assert got[0].args["note"] == "x" * 4000


@linux_only
async def test_a_client_that_disappears_mid_stream_does_not_take_the_server_with_it(
    echo: _Echo,
) -> None:
    client = AsyncDaemonClient(echo.path)
    stream = client.stream_events()
    await anext(stream)
    await stream.aclose()

    # The server survived, and still answers the next caller.
    response = await AsyncDaemonClient(echo.path).call(Op.PING)
    assert response.ok
