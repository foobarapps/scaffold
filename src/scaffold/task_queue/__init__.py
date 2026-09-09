import asyncio
import datetime
import importlib
import logging
import traceback
import uuid
from collections.abc import AsyncGenerator, Callable
from typing import Protocol, override, runtime_checkable

import pydantic
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from scaffold.task_queue import migrations
from scaffold.uuid7 import uuid7

logger = logging.getLogger(__name__)


@runtime_checkable
class HandlerProtocol[T](Protocol):
    async def handle_task(self, task: T) -> None: ...


class PostgresTaskQueue[T]:
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
        max_attempts: int = 5,
        retry_backoff: float = 5.0,
        max_retry_backoff: float = 3600.0,
    ) -> None:
        self._handler_factories: dict[type[T], Callable[[], HandlerProtocol[T]]] = {}
        self._connection_pool = connection_pool
        self._schema_name = schema_name
        self._table_name = table_name
        self._notify_channel_name = notify_channel_name
        self._max_attempts = max_attempts
        self._retry_backoff = retry_backoff
        self._max_retry_backoff = max_retry_backoff

    async def init(self) -> None:
        """Open the pool and refuse to start against a schema this build does not understand.

        Migrations are applied out of band -- see `scaffold.task_queue.migrations`. Doing it
        here instead would mean DDL, and an ACCESS EXCLUSIVE lock on a live queue table, on
        every worker start-up, and would force the runtime role to hold DDL privileges.
        """
        await self._connection_pool.open()
        version = await migrations.current_version(
            self._connection_pool,
            schema_name=self._schema_name,
            table_name=self._table_name,
        )
        if version != migrations.LATEST_VERSION:
            raise migrations.SchemaVersionMismatchError(version, migrations.LATEST_VERSION)

    async def enqueue(
        self,
        task: T,
        visibility_timeout: int = 30,
        run_at: datetime.datetime | None = None,
    ) -> None:
        """Enqueue a task, optionally deferring it until `run_at` (defaults to now)."""
        async with self._connection_pool.connection() as conn:
            stmt = sql.SQL("""\
            INSERT INTO {table} (id, class_name, module_name, data, enqueued_at, run_at, visibility_timeout)
            VALUES (%s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s)
            """).format(table=self._full_table_identifier)

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
                    datetime.datetime.now(datetime.UTC),
                    run_at,
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
        """Run a task's handler, acknowledging it on success and scheduling a retry on failure.

        This never raises. It runs inside the task group in `handle_tasks()`, where an
        escaping exception would cancel every sibling task and end the consume loop --
        one bad task would take down the whole worker.
        """
        error: Exception | None = None
        try:
            handler = self._handler_factories[type(task)]()
            # TODO check if whether the handler is a coroutine function
            await handler.handle_task(task)
        except Exception as exc:  # noqa: BLE001
            error = exc

        try:
            if error is None:
                await self.ack(task_id)
            else:
                logger.error("Task %s failed, scheduling a retry", task_id, exc_info=error)
                await self._fail_task(task_id, error)
        except Exception:
            # Recording the outcome failed (an unreachable database, say). Leave the task
            # unacknowledged so it is redelivered once the visibility timeout lapses,
            # rather than letting this escape into the caller's task group.
            logger.exception("Could not record the outcome of task %s", task_id)

    async def _fail_task(self, task_id: uuid.UUID, error: Exception) -> None:
        """Schedule the next attempt with an exponential backoff, or dead-letter the task."""
        async with self._connection_pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            # `attempts` on the right-hand side is the pre-update value, so the first
            # failure waits `retry_backoff` seconds, the second twice that, and so on.
            # Clearing `dequeued_at` hands the scheduling over to `run_at` entirely --
            # otherwise the visibility timeout, not the backoff, would gate the retry.
            cursor = await cursor.execute(
                sql.SQL("""\
                UPDATE {table}
                SET
                    attempts = attempts + 1,
                    dequeued_at = NULL,
                    run_at = NOW() + make_interval(secs => LEAST(%s * POWER(2, attempts), %s))
                WHERE
                    id = %s
                    AND acknowledged_at IS NULL
                RETURNING attempts, class_name, module_name, data, enqueued_at
                """).format(table=self._full_table_identifier),
                (self._retry_backoff, self._max_retry_backoff, task_id),
            )

            row = await cursor.fetchone()

            if row is None:
                # Already acknowledged, or gone -- there is nothing left to retry.
                return

            if row["attempts"] < self._max_attempts:
                await conn.commit()
                return

            # Out of attempts: record the payload, the attempt count and the last error so
            # the failure is visible, then acknowledge the task to take it out of rotation.
            await conn.execute(
                sql.SQL("""\
                INSERT INTO {failure_table} (
                    id, task_id, class_name, module_name, data, attempts, last_error, enqueued_at, failed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                """).format(failure_table=self._full_failure_table_identifier),
                (
                    str(uuid7()),
                    task_id,
                    row["class_name"],
                    row["module_name"],
                    Json(row["data"]),
                    row["attempts"],
                    "".join(traceback.format_exception(error)),
                    row["enqueued_at"],
                ),
            )
            await conn.execute(
                sql.SQL(
                    "UPDATE {table} SET acknowledged_at = NOW() WHERE id = %s",
                ).format(table=self._full_table_identifier),
                (task_id,),
            )
            await conn.commit()

            logger.error(
                "Task %s dead-lettered after %s attempts",
                task_id,
                row["attempts"],
            )

    async def handle_tasks(self) -> None:
        async with asyncio.TaskGroup() as tg:
            async for task_id, task in self._tasks:
                tg.create_task(self.handle_task(task_id, task))

    @property
    async def _tasks(self) -> AsyncGenerator[tuple[uuid.UUID, T]]:
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

    async def _get_task(self) -> tuple[uuid.UUID, T] | None:
        async with self._connection_pool.connection() as conn:
            cursor = conn.cursor(row_factory=dict_row)
            cursor = await cursor.execute(
                sql.SQL("""\
                UPDATE {table}
                SET dequeued_at = NOW()
                WHERE id = (
                    SELECT
                        id
                    FROM
                        {table}
                    WHERE
                        acknowledged_at IS NULL
                        AND run_at <= NOW()
                        AND (dequeued_at IS NULL OR dequeued_at < NOW() - make_interval(secs => visibility_timeout))
                    ORDER BY
                        run_at,
                        enqueued_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT
                        1
                )
                RETURNING id, class_name, module_name, data
                """).format(table=self._full_table_identifier),
            )

            task_data = await cursor.fetchone()

            if task_data is None:
                return None

            task_id = task_data["id"]
            # TODO cache this
            task_module = importlib.import_module(task_data["module_name"])
            task_class = getattr(task_module, task_data["class_name"])
            task = pydantic.TypeAdapter(task_class).validate_python(
                task_data["data"],
            )

            return task_id, task

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

    def register(
        self,
        task_type: type[T],
        handler_factory: Callable[[], HandlerProtocol[T]],
    ) -> None:
        self._handler_factories[task_type] = handler_factory

    @property
    def _full_table_identifier(self) -> sql.Identifier:
        return sql.Identifier(self._schema_name, self._table_name)

    @property
    def _full_failure_table_identifier(self) -> sql.Identifier:
        return sql.Identifier(self._schema_name, f"{self._table_name}_failure")


class GenericFakeTaskQueue[T]:
    def __init__(
        self,
    ) -> None:
        self.handler_factories: dict[type[T], Callable[[], HandlerProtocol[T]]] = {}
        self.queue: list[T] = []

    def enqueue(self, task: T) -> None:
        self.queue.append(task)

    def register(
        self,
        task_type: type[T],
        handler_factory: Callable[[], HandlerProtocol[T]],
    ) -> None:
        self.handler_factories[task_type] = handler_factory

    async def run(self) -> None:
        for task in self.queue:
            handler = self.handler_factories[type(task)]()
            await handler.handle_task(task)
