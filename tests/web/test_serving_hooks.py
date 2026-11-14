import asyncio
from collections.abc import AsyncGenerator

import pytest
from hypercorn.typing import LifespanScope

from scaffold.web import BaseWebApp, after_serving, before_serving, while_serving


async def run_lifespan(app: BaseWebApp, events: list[str]) -> None:
    """Drive the app through an ASGI startup and shutdown like a server would."""
    incoming: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    await incoming.put({"type": "lifespan.startup"})

    async def receive() -> dict[str, str]:
        return await incoming.get()

    async def send(message: dict[str, str]) -> None:
        events.append(message["type"])
        if message["type"] == "lifespan.startup.complete":
            await incoming.put({"type": "lifespan.shutdown"})

    scope: LifespanScope = {"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}  # pyright: ignore[reportAssignmentType]
    await app(scope, receive, send)


@pytest.mark.asyncio
async def test_while_serving_wraps_serving() -> None:
    events: list[str] = []
    app = BaseWebApp("__main__")

    async def lifetime() -> AsyncGenerator[None]:
        events.append("started")
        yield
        events.append("stopped")

    app.while_serving(lifetime)

    await run_lifespan(app, events)

    assert events == ["started", "lifespan.startup.complete", "stopped", "lifespan.shutdown.complete"]


@pytest.mark.asyncio
async def test_after_serving_runs_at_shutdown() -> None:
    events: list[str] = []
    app = BaseWebApp("__main__")

    async def stop() -> None:
        events.append("stopped")

    app.after_serving(stop)

    await run_lifespan(app, events)

    assert events == ["lifespan.startup.complete", "stopped", "lifespan.shutdown.complete"]


@pytest.mark.asyncio
async def test_serving_decorators_register_app_methods() -> None:
    events: list[str] = []

    class App(BaseWebApp):
        @before_serving
        async def start(self) -> None:
            events.append("before")

        @while_serving
        async def lifetime(self) -> AsyncGenerator[None]:
            events.append("while started")
            yield
            events.append("while stopped")

        @after_serving
        async def stop(self) -> None:
            events.append("after")

    await run_lifespan(App("__main__"), events)

    started = events.index("lifespan.startup.complete")
    assert sorted(events[:started]) == ["before", "while started"]
    assert sorted(events[started + 1 : -1]) == ["after", "while stopped"]
