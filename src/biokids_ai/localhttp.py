"""Just enough HTTP to talk to llama-server and whisper-server on this machine.

Both run as children of the assistant service, which has no network at all
(`PrivateNetwork=yes`): llama-server on a Unix socket, whisper-server on
127.0.0.1 inside the service's own empty network namespace. Nothing here can
reach another machine, which is the point; a general HTTP library would bring
proxies, redirects and certificate stores to a conversation that has none.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, cast

DEFAULT_TIMEOUT_S: Final = 120.0


class HttpError(Exception):
    """The local server could not be reached, or answered with an error."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """`unix:/run/biokids-ai/llm.sock` or `http://127.0.0.1:8178`."""

    address: str

    async def open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.address.startswith("unix:"):
            # typeshed declares the unix helpers on POSIX only, and mypy runs on
            # the Windows development host. Production is Linux (D-12).
            open_unix = cast(
                "Callable[..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]",
                getattr(asyncio, "open_unix_connection"),  # noqa: B009
            )
            return await open_unix(self.address.removeprefix("unix:"), limit=1 << 22)
        host, _, port = self.address.removeprefix("http://").partition(":")
        return await asyncio.open_connection(host, int(port or 80), limit=1 << 22)

    @property
    def host_header(self) -> str:
        return (
            "localhost"
            if self.address.startswith("unix:")
            else self.address.removeprefix("http://")
        )


async def request(
    endpoint: Endpoint,
    method: str,
    path: str,
    body: bytes = b"",
    *,
    content_type: str = "application/json",
    wait_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[int, bytes]:
    """One request, one response, then the connection is closed."""
    try:
        return await asyncio.wait_for(
            _exchange(endpoint, method, path, body, content_type), timeout=wait_s
        )
    except TimeoutError as exc:
        raise HttpError(f"{method} {path}: no answer within {wait_s:.0f}s") from exc
    except OSError as exc:
        raise HttpError(f"{method} {path}: {exc}") from exc


async def _exchange(
    endpoint: Endpoint, method: str, path: str, body: bytes, content_type: str
) -> tuple[int, bytes]:
    reader, writer = await endpoint.open()
    try:
        head = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {endpoint.host_header}\r\n"
            "Connection: close\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        )
        writer.write(head.encode("ascii") + body)
        await writer.drain()
        raw = await reader.read()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
    return parse_response(raw)


def parse_response(raw: bytes) -> tuple[int, bytes]:
    head, sep, body = raw.partition(b"\r\n\r\n")
    if not sep:
        raise HttpError("the local server closed the connection without an answer")
    lines = head.decode("iso-8859-1").split("\r\n")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError) as exc:
        raise HttpError(f"not an HTTP answer: {lines[0][:80]!r}") from exc
    headers = {
        name.strip().lower(): value.strip()
        for name, _, value in (line.partition(":") for line in lines[1:])
    }
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(body)
    return status, body


def _dechunk(body: bytes) -> bytes:
    out = bytearray()
    while body:
        size_line, _, rest = body.partition(b"\r\n")
        size = int(size_line.split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out += rest[:size]
        body = rest[size + 2 :]
    return bytes(out)


async def post_json(
    endpoint: Endpoint,
    path: str,
    payload: dict[str, Any],
    *,
    wait_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    status, body = await request(
        endpoint, "POST", path, json.dumps(payload).encode("utf-8"), wait_s=wait_s
    )
    if status != 200:
        raise HttpError(f"POST {path}: {status} {body[:200].decode('utf-8', 'replace')}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise HttpError(f"POST {path}: not JSON") from exc
    if not isinstance(data, dict):
        raise HttpError(f"POST {path}: not a JSON object")
    return data


async def post_multipart(
    endpoint: Endpoint,
    path: str,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content: bytes,
    *,
    wait_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any]:
    boundary = f"biokids-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        disposition = f'Content-Disposition: form-data; name="{name}"'
        parts.append(f"--{boundary}\r\n{disposition}\r\n\r\n{value}\r\n".encode())
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode()
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    status, body = await request(
        endpoint,
        "POST",
        path,
        b"".join(parts),
        content_type=f"multipart/form-data; boundary={boundary}",
        wait_s=wait_s,
    )
    if status != 200:
        raise HttpError(f"POST {path}: {status} {body[:200].decode('utf-8', 'replace')}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise HttpError(f"POST {path}: not JSON") from exc
    return data if isinstance(data, dict) else {}


async def is_healthy(endpoint: Endpoint, path: str = "/health") -> bool:
    try:
        status, _ = await request(endpoint, "GET", path, wait_s=3)
    except HttpError:
        return False
    return status == 200
