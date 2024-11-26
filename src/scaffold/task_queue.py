import asyncio
import datetime
import importlib
import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Protocol, override, runtime_checkable

import pydantic
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from scaffold.uuid7 import uuid7


@runtime_checkable
class HandlerProtocol[T](Protocol):
    async def handle_task(self, task: T) -> None: ...


class PostgresTaskQueue[T, H: HandlerProtocol]:
    @override
    def __init_subclass__(cls) -> None:
        # TODO Check that the runtime type of the `T` type param is the same as the type hint of `H.handle_task` `task` param.
        # Ideally, we would like to do something like `GenericFakeTaskQueue[T, H: HandlerProtocol[T]]`
        # to check it statically but that's currently not possible. Now, the `HandlerProtocol`` is not parametrized so it's
        # equivalent to `HandlerProtocol[Any]``.
        pass

    def __init__(
        self,
        connection_pool: AsyncConnectionPool,
        schema_name: str = "public",
        table_name: str = "task",
        notify_channel_name: str = "task_queue_notifications",
    ) -> None:
        self._handler_factories: dict[type[T], Callable[[], H]] = {}
        self._connection_pool = connection_pool
        self._schema_name = schema_name
        self._table_name = table_name
        self._notify_channel_name = notify_channel_name

    async def init(self) -> None:
        await self._connection_pool.open()
        async with self._connection_pool.connection() as conn:
            # TODO add queue name
            stmt = sql.SQL(
                """
            CREATE TABLE IF NOT EXISTS {table} (
                id UUID PRIMARY KEY,
                class_name VARCHAR NOT NULL,
                module_name VARCHAR NOT NULL,
                data JSONB NOT NULL,
                enqueued_at TIMESTAMP NOT NULL,
                dequeued_at TIMESTAMP,
                acknowledged_at TIMESTAMP,
                visibility_timeout INTEGER NOT NULL
            )
            """,
            ).format(table=self._full_table_identifier)
            # TODO create table task_failure
            await conn.execute(stmt)

    async def enqueue(
        self,
        task: T,
        visibility_timeout: int = 30,
    ) -> None:
        async with self._connection_pool.connection() as conn:
            stmt = sql.SQL(
                """\
            INSERT INTO {table} (id, class_name, module_name, data, enqueued_at, visibility_timeout)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            ).format(table=self._full_table_identifier)

            class_name = task.__class__.__name__
            module_name = task.__class__.__module__

            # TODO check that the task is a data class
            # dataclasses.is_dataclass(task)

            # TODO cache type adapters?
            data = Json(
                task,
                dumps=lambda obj: pydantic.TypeAdapter(task.__class__).dump_json(obj),
            )

            await conn.execute(
                stmt,
                (
                    str(uuid7()),
                    class_name,
                    module_name,
                    data,
                    datetime.datetime.utcnow(),
                    visibility_timeout,
                ),
            )
            await conn.execute(
                sql.SQL("NOTIFY {channel_name}").format(
                    channel_name=sql.Identifier(self._notify_channel_name),
                ),
            )
            await conn.commit()

    async def handle_task(self, task_id: uuid.UUID, task: T) -> None:
        # TODO handle exceptions
        handler = self._handler_factories[type(task)]()
        # TODO check if whether the handler is a coroutine function
        await handler.handle_task(task)
        await self.ack(task_id)

    async def handle_tasks(self) -> None:
        async with asyncio.TaskGroup() as tg:
            async for task_data in self._tasks:
                task_id = task_data["id"]
                # TODO cache this
                task_module = importlib.import_module(task_data["module_name"])
                task_class = getattr(task_module, task_data["class_name"])
                task = pydantic.TypeAdapter(task_class).validate_python(
                    task_data["data"],
                )
                tg.create_task(self.handle_task(task_id, task))

    @property
    async def _tasks(self) -> AsyncGenerator[dict]:
        async with self._connection_pool.connection() as listen_conn:
            await listen_conn.execute(
                sql.SQL("LISTEN {channel_name}").format(
                    channel_name=sql.Identifier(self._notify_channel_name),
                ),
            )
            await listen_conn.commit()

            while True:
                task = await self._get_task()
                if task:
                    yield task
                else:
                    # We are doing long polling as well because there's no notification when a message times out
                    async for _ in listen_conn.notifies(timeout=1):
                        break

    async def _get_task(self) -> dict | None:
        async with self._connection_pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            cursor = await cursor.execute(
                sql.SQL(
                    """\
                UPDATE {table}
                SET dequeued_at = NOW()
                WHERE id = (
                    SELECT
                        id
                    FROM
                        {table}
                    WHERE
                        acknowledged_at IS NULL
                        AND (dequeued_at IS NULL OR dequeued_at < NOW() - make_interval(secs => visibility_timeout))
                    ORDER BY
                        enqueued_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT
                        1
                )
                RETURNING id, class_name, module_name, data
                """,
                ).format(table=self._full_table_identifier),
            )

            return await cursor.fetchone()

    async def ack(self, task_id: uuid.UUID) -> None:
        async with self._connection_pool.connection() as conn:
            await conn.execute(
                sql.SQL(
                    "UPDATE {table} SET acknowledged_at = NOW() WHERE id = %s",
                ).format(
                    table=self._full_table_identifier,
                ),
                (task_id,),
            )
            await conn.commit()

    def register(self, task_type: type[T], handler_factory: Callable[[], H]) -> None:
        self._handler_factories[task_type] = handler_factory

    @property
    def _full_table_identifier(self) -> sql.Identifier:
        return sql.Identifier(self._schema_name, self._table_name)


class GenericFakeTaskQueue[T, H: HandlerProtocol]:
    def __init__(
        self,
    ) -> None:
        self.handler_factories: dict[type[T], Callable[[], H]] = {}
        self.queue: list[T] = []

    def enqueue(self, task: T) -> None:
        self.queue.append(task)

    def register(self, task_type: type[T], handler_factory: Callable[[], H]) -> None:
        self.handler_factories[task_type] = handler_factory

    async def run(self) -> None:
        for task in self.queue:
            handler = self.handler_factories[type(task)]()
            await handler.handle_task(task)
