import asyncio
import json
from collections.abc import AsyncGenerator

from psycopg import sql
from psycopg_pool import AsyncConnectionPool


class PostgresPubSubService:
    def __init__(
        self,
        connection_pool: AsyncConnectionPool,
        database_channel: str = "pub_sub_messages",
    ) -> None:
        self._connection_pool = connection_pool
        self._database_channel = database_channel
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._listener_task: asyncio.Task | None = None

    async def init(self) -> None:
        await self._connection_pool.open()

    async def publish(self, channel_name: str, message: str) -> None:
        async with self._connection_pool.connection() as conn:
            payload = json.dumps({"channel_name": channel_name, "message": message})
            await conn.execute(
                sql.SQL("NOTIFY {database_channel}, {payload}").format(
                    database_channel=sql.Identifier(self._database_channel),
                    payload=sql.Literal(payload),
                ),
            )
            await conn.commit()

    async def subscribe(self, channel_name: str) -> AsyncGenerator[str, None]:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(channel_name, []).append(queue)

        # Start the listener task if it's not already running
        if not self._listener_task or self._listener_task.done():
            self._listener_task = asyncio.create_task(self._listen())

        try:
            while True:
                message = await queue.get()
                yield message

        finally:
            # Cleanup when the subscriber is done
            self._subscribers[channel_name].remove(queue)
            if not self._subscribers[channel_name]:
                del self._subscribers[channel_name]

            # Cancel the listener task if no subscribers remain
            if not any(self._subscribers.values()) and self._listener_task:
                self._listener_task.cancel()
                try:
                    await self._listener_task
                except asyncio.CancelledError:
                    pass  # Listener task has been cancelled
                self._listener_task = None

    async def _listen(self) -> None:
        async with self._connection_pool.connection() as conn:
            await conn.execute(
                sql.SQL("LISTEN {database_channel}").format(
                    database_channel=sql.Identifier(self._database_channel),
                ),
            )
            await conn.commit()

            async for notification in conn.notifies():
                payload = notification.payload
                try:
                    # Parse the JSON payload
                    data = json.loads(payload)
                    channel_name = data.get("channel_name")
                    message = data.get("message")

                    if channel_name and message:
                        # Dispatch the message to subscribers of the logical channel
                        subscribers = self._subscribers.get(channel_name, [])
                        for queue in subscribers:
                            await queue.put(message)

                except json.JSONDecodeError:
                    # Handle invalid JSON payloads
                    # TODO log this?
                    continue
