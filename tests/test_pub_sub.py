import asyncio
import contextlib
import json
from collections.abc import AsyncGenerator, AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from psycopg_pool import AsyncConnectionPool

from scaffold.pub_sub import PostgresPubSubService


class FakeConnection:
    def __init__(self, notifications: "asyncio.Queue[str]") -> None:
        self._notifications = notifications

    async def execute(self, *args: object) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def notifies(self) -> AsyncIterator[SimpleNamespace]:
        while True:
            yield SimpleNamespace(payload=await self._notifications.get())


class FakePool:
    """Stands in for psycopg_pool: every connection sees the same stream of NOTIFY payloads.

    Leaving a connection takes an event-loop turn, as returning one to a real pool does, which
    is what opens the window between cancelling a listener and it finishing.
    """

    def __init__(self) -> None:
        self.notifications: asyncio.Queue[str] = asyncio.Queue()
        self.open_connections = 0

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncGenerator[FakeConnection]:
        self.open_connections += 1
        try:
            yield FakeConnection(self.notifications)
        finally:
            await asyncio.sleep(0)
            self.open_connections -= 1

    def notify(self, payload: str) -> None:
        self.notifications.put_nowait(payload)


def make_service(pool: FakePool) -> PostgresPubSubService:
    return PostgresPubSubService(cast(AsyncConnectionPool[Any], pool))


async def collect(service: PostgresPubSubService, channel: str, into: list[str]) -> None:
    async for message in service.subscribe(channel):
        into.append(message)


def message(channel: str, text: str) -> str:
    return json.dumps({"channel_name": channel, "message": text})


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "poison",
    ["1", "[1]", "null", '"x"', '{"channel_name": ["l"], "message": "m"}', "not json"],
)
async def test_a_malformed_notification_does_not_stop_delivery(poison: str) -> None:
    pool = FakePool()
    service = make_service(pool)
    received: list[str] = []
    subscriber = asyncio.create_task(collect(service, "room", received))
    await settle()

    pool.notify(message("room", "before"))
    pool.notify(poison)
    pool.notify(message("room", "after"))
    await settle()

    assert received == ["before", "after"]
    subscriber.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await subscriber


@pytest.mark.asyncio
@pytest.mark.parametrize("yields_before_resubscribing", range(6))
async def test_resubscribing_during_teardown_keeps_exactly_one_listener(
    yields_before_resubscribing: int,
) -> None:
    pool = FakePool()
    service = make_service(pool)

    first = asyncio.create_task(collect(service, "a", []))
    await settle()
    first.cancel()
    for _ in range(yields_before_resubscribing):
        await asyncio.sleep(0)

    received_b: list[str] = []
    received_c: list[str] = []
    second = asyncio.create_task(collect(service, "b", received_b))
    await settle()
    third = asyncio.create_task(collect(service, "b", received_c))
    await settle()

    pool.notify(message("b", "m"))
    await settle()

    # Every subscriber gets each message exactly once: no subscriber is stranded without a
    # listener, and no orphaned second listener delivers duplicates.
    assert received_b == ["m"]
    assert received_c == ["m"]

    for task in (first, second, third):
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    await settle()

    # With every subscriber gone, no listener is left holding a pooled connection.
    assert pool.open_connections == 0
