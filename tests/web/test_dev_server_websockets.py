import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from wsproto import WSConnection
from wsproto.connection import ConnectionType
from wsproto.events import AcceptConnection, CloseConnection, RejectConnection, Request

from scaffold.web.dev_server import handle_connection
from scaffold.web.typing import (
    ASGIApp,
    ASGIReceiveCallable,
    ASGISendCallable,
    Scope,
    WebsocketAcceptEvent,
    WebsocketCloseEvent,
)


async def refusing_app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
    await send(WebsocketCloseEvent(type="websocket.close", code=1000))


async def accept_then_close_app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
    await send(WebsocketAcceptEvent(type="websocket.accept", subprotocol=None, headers=[]))
    await send(WebsocketCloseEvent(type="websocket.close", code=1000))


@asynccontextmanager
async def serve(app: ASGIApp) -> AsyncGenerator[int]:
    async def on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_connection(reader, writer, app, {}, "127.0.0.1", 0)

    server = await asyncio.start_server(on_connection, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1]


async def open_websocket(port: int) -> list[object]:
    """Every wsproto event the client sees until the server closes the connection."""
    client = WSConnection(ConnectionType.CLIENT)
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(client.send(Request(host="127.0.0.1", target="/ws")))
    await writer.drain()

    events: list[object] = []
    while data := await asyncio.wait_for(reader.read(4096), timeout=5):
        client.receive_data(data)
        events.extend(client.events())
        if any(isinstance(event, RejectConnection | CloseConnection) for event in events):
            break

    writer.close()
    return events


@pytest.mark.asyncio
async def test_close_before_accept_rejects_the_handshake_with_403() -> None:
    async with serve(refusing_app) as port:
        events = await open_websocket(port)

    rejections = [event for event in events if isinstance(event, RejectConnection)]
    assert len(rejections) == 1
    assert rejections[0].status_code == 403


@pytest.mark.asyncio
async def test_close_after_accept_sends_a_close_frame() -> None:
    async with serve(accept_then_close_app) as port:
        events = await open_websocket(port)

    assert isinstance(events[0], AcceptConnection)
    closes = [event for event in events if isinstance(event, CloseConnection)]
    assert len(closes) == 1
    assert closes[0].code == 1000
