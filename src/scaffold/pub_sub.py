import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)


class PostgresPubSubService:
    def __init__(
        self,
        connection_pool: AsyncConnectionPool,
        database_channel: str = "pub_sub_messages",
        reconnect_delay: float = 1.0,
    ) -> None:
        """
        `reconnect_delay` is how long the listener waits before listening again after its
        connection failed.
        """
        self._connection_pool = connection_pool
        self._database_channel = database_channel
        self._reconnect_delay = reconnect_delay
        self._subscribers: dict[str, list[asyncio.Queue[str]]] = {}
        self._listener_task: asyncio.Task[None] | None = None

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
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.setdefault(channel_name, []).append(queue)

        # Start the listener task if it's not already running. A listener that is being
        # cancelled counts as not running: the last subscriber's cleanup is still tearing it
        # down, and without a fresh one this subscriber would never receive anything.
        listener = self._listener_task
        if listener is None or listener.done() or listener.cancelling():
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
            listener = self._listener_task
            if not any(self._subscribers.values()) and listener:
                listener.cancel()
                try:
                    await listener
                except asyncio.CancelledError:
                    pass  # Listener task has been cancelled
                # A subscriber may have started a new listener while this one was being
                # awaited. Only forget the task cancelled here, or the new one would be
                # orphaned, holding its pooled connection for good.
                if self._listener_task is listener:
                    self._listener_task = None

    async def _listen(self) -> None:
        # The one listener serves every subscriber in the process, so it must not end while
        # they wait: if its connection fails, it listens again on a new one. Messages
        # published in the meantime are lost, as NOTIFY is not stored.
        while True:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("The pub/sub listener failed, listening again")
            await asyncio.sleep(self._reconnect_delay)

    async def _listen_once(self) -> None:
        async with self._connection_pool.connection() as conn:
            await conn.execute(
                sql.SQL("LISTEN {database_channel}").format(
                    database_channel=sql.Identifier(self._database_channel),
                ),
            )
            await conn.commit()

            async for notification in conn.notifies():
                self._dispatch(notification.payload)

    def _dispatch(self, payload: str) -> None:
        # Any session on the database can NOTIFY this channel, so a payload is not
        # necessarily one `publish()` built. Drop anything malformed one message at a time:
        # an exception here would end the listener for every subscriber.
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Ignoring a pub/sub notification that is not valid JSON")
            return

        if not isinstance(data, dict):
            logger.warning("Ignoring a pub/sub notification that is not a JSON object")
            return

        channel_name = data.get("channel_name")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        message = data.get("message")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(channel_name, str) or not isinstance(message, str):
            logger.warning("Ignoring a pub/sub notification without string fields")
            return

        # An empty channel name or message is not something `publish()` sends either
        if not channel_name or not message:
            return

        for queue in list(self._subscribers.get(channel_name, [])):
            queue.put_nowait(message)


class AsyncioPubSubService:
    def __init__(self) -> None:
        self.channels: dict[str, set[asyncio.Queue[str]]] = {}

    async def publish(self, channel_name: str, payload: str) -> None:
        if channel_name not in self.channels:
            return

        for queue in self.channels[channel_name]:
            await queue.put(payload)

    async def subscribe(self, channel_name: str) -> AsyncGenerator[str, None]:
        if channel_name not in self.channels:
            self.channels[channel_name] = set()

        queue: asyncio.Queue[str] = asyncio.Queue()
        self.channels[channel_name].add(queue)

        try:
            while True:
                yield await queue.get()

        finally:
            self.channels[channel_name].remove(queue)
            if not self.channels[channel_name]:
                del self.channels[channel_name]
