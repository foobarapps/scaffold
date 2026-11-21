import asyncio
import contextlib
import socket
from collections.abc import AsyncGenerator

import pytest

from scaffold.web import dev_server
from scaffold.web.typing import ASGIReceiveCallable, ASGISendCallable, Scope


async def echo_app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    while True:
        message = await receive()
        if message["type"] != "http.request" or not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"2")]})
    await send({"type": "http.response.body", "body": b"ok", "more_body": False})


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.asynccontextmanager
async def running_server() -> AsyncGenerator[tuple[int, asyncio.Task[None]]]:
    port = free_port()
    server = asyncio.create_task(dev_server.serve(echo_app, "127.0.0.1", port))
    try:
        for _ in range(100):
            with contextlib.suppress(OSError):
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                break
            await asyncio.sleep(0.05)
        yield port, server
    finally:
        server.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await server


async def get_status_line(port: int) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
    data = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    return data.split(b"\r\n", 1)[0]


async def send_raw(port: int, payload: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    data = await asyncio.wait_for(reader.read(200), timeout=5)
    writer.close()
    return data


# Every connection runs in one task group, so an error escaping one client's connection used
# to cancel all the others and end the server.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        # An upgrade request without Sec-WebSocket-Key / Sec-WebSocket-Version.
        b"GET /ws HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n",
        # A chunked body with an illegal chunk header, read while the app consumes the body.
        b"POST / HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\nZZ\r\nabc\r\n0\r\n\r\n",
    ],
)
async def test_a_malformed_request_does_not_stop_the_server(payload: bytes) -> None:
    async with running_server() as (port, server):
        await send_raw(port, payload)
        await asyncio.sleep(0.2)

        assert not server.done()
        assert await get_status_line(port) == b"HTTP/1.1 200 "


@pytest.mark.asyncio
async def test_an_upgrade_without_a_websocket_key_is_refused_with_400() -> None:
    async with running_server() as (port, _):
        response = await send_raw(
            port,
            b"GET /ws HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n",
        )

    assert response.startswith(b"HTTP/1.1 400")
