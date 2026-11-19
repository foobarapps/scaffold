import asyncio

import pytest
from hypercorn.typing import ASGIReceiveCallable, ASGISendCallable, Scope

from scaffold.web.dev_server import lifespan


@pytest.mark.asyncio
async def test_startup_is_delivered_once_and_shutdown_only_when_the_server_stops() -> None:
    received: list[str] = []
    asked_again = asyncio.Event()

    async def app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        assert scope["type"] == "lifespan"
        received.append((await receive())["type"])
        # Asks for the next event before startup is over, as a middleware that fans the
        # events out to several apps does. It must wait for the shutdown, not get a
        # second startup.
        next_event = asyncio.ensure_future(receive())
        asked_again.set()
        await send({"type": "lifespan.startup.complete"})
        received.append((await next_event)["type"])
        await send({"type": "lifespan.shutdown.complete"})

    async with lifespan(app, {}):  # pyright: ignore[reportArgumentType]
        await asyncio.wait_for(asked_again.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert received == ["lifespan.startup"]

    assert received == ["lifespan.startup", "lifespan.shutdown"]
