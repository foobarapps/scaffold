import asyncio
import contextlib
import dataclasses
import datetime
import logging
import traceback
import uuid
from collections.abc import AsyncGenerator, Callable, Mapping
from typing import Any, override

import pydantic
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from scaffold.uuid7 import uuid7

from . import migrations
from ._wakeup import Wakeup, wait_for
from .task import DEFAULT_QUEUE, HandlerProtocol, TaskInfo

logger = logging.getLogger(__name__)

# A lease is renewed this many times within one visibility timeout, so that a renewal or two
# can fail without the task being handed to another worker.
LEASE_RENEWALS_PER_TIMEOUT = 4


@dataclasses.dataclass(frozen=True)
class _Delivery[T]:
    info: TaskInfo
    task: T


class TaskRunTimeExceededError(TimeoutError):
    """A handler ran for longer than the queue's `max_run_time` and was cancelled."""

    def __init__(self, task_id: uuid.UUID, max_run_time: float | None) -> None:
        super().__init__(f"Task {task_id} ran for over {max_run_time} seconds and was cancelled")


class PostgresTaskQueue[T]:
    """
    A task queue kept in a Postgres table.

    Delivery is at least once. A task handed to a worker is leased for its visibility timeout
    and the lease is renewed while the handler runs. If the worker dies, the lease runs out
    and the task is handed out again. A handler that raises is retried with an exponential
    backoff, up to `max_attempts` runs, and then dead-lettered: recorded in the failure table
    and taken out of rotation. A task that cannot be deserialized is dead-lettered at once,
    since trying again cannot help.

    Tasks go to named queues, `default` unless said otherwise. `consume()` works one queue
    with a fixed number of consumers and stops gracefully; `handle_tasks()` runs tasks from
    any queue as soon as it gets them, a bounded number at a time.
    """

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
        max_deliveries: int | None = None,
        poll_interval: float = 1.0,
        strict_serialization: bool = False,
        on_task_start: Callable[[TaskInfo], None] | None = None,
        max_run_time: float | None = None,
    ) -> None:
        """
        `max_attempts` is how many times a handler may run and fail before the task is
        dead-lettered; a task can override it when it is enqueued.

        `max_deliveries` bounds how many times a task is handed out at all, which is what
        stops a task that kills its worker every time, before the handler can fail. Without
        it such a task is handed out forever.

        `strict_serialization` refuses a task whose values do not match its declared types,
        instead of storing something that comes back different (a UUID in a field declared
        as a plain object comes back as a string, for example).

        `on_task_start` is called with the task's info right before its handler runs, e.g.
        to tell an error tracker who the task was enqueued for.

        `max_run_time` is how many seconds a handler may run. Past that it is taken to be
        hung: it is cancelled and the task counts as failed, so it is retried or dead-lettered
        like after any other failure. Without it a handler may run for as long as it likes,
        and under `consume()` its lease is renewed all the while.
        """
        self._handler_factories: dict[type[T], Callable[[], HandlerProtocol[T]]] = {}
        self._connection_pool = connection_pool
        self._schema_name = schema_name
        self._table_name = table_name
        self._notify_channel_name = notify_channel_name
        self._max_attempts = max_attempts
        self._retry_backoff = retry_backoff
        self._max_retry_backoff = max_retry_backoff
        self._max_deliveries = max_deliveries
        self._poll_interval = poll_interval
        self._strict_serialization = strict_serialization
        self._on_task_start = on_task_start
        self._max_run_time = max_run_time
        self._handed_out: dict[uuid.UUID, TaskInfo] = {}

    async def init(self) -> None:
        """Open the pool and refuse to start against a schema older than this build needs.

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
        # A newer schema is fine, migrations only add to it. An older one lacks what this code needs.
        if version < migrations.LATEST_VERSION:
            raise migrations.SchemaVersionMismatchError(
                version,
                migrations.LATEST_VERSION,
            )

    async def enqueue(
        self,
        task: T,
        visibility_timeout: int = 30,
        run_at: datetime.datetime | None = None,
        *,
        queue: str = DEFAULT_QUEUE,
        max_attempts: int | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        """
        Enqueue a task, optionally deferring it until `run_at` (defaults to now).

        `max_attempts` overrides the queue's for this task: 1 means a handler that raises is
        not run again, for work that is not safe to repeat. `metadata` travels with the task
        and is given to `on_task_start`.
        """
        # TODO check that the task is a data class
        # dataclasses.is_dataclass(task)

        # TODO cache type adapters?
        warnings = "error" if self._strict_serialization else True
        data = Json(
            task,
            dumps=lambda obj: pydantic.TypeAdapter(task.__class__).dump_json(obj, warnings=warnings),
        )

        # A process that only enqueues, such as a web app, never calls init(). Opening an
        # open pool does nothing.
        await self._connection_pool.open()

        async with self._connection_pool.connection() as conn:
            stmt = sql.SQL("""\
            INSERT INTO {table} (
                id, queue, class_name, module_name, data, metadata,
                enqueued_at, run_at, visibility_timeout, max_attempts
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, NOW()), %s, %s)
            """).format(table=self._full_table_identifier)

            await conn.execute(
                stmt,
                (
                    str(uuid7()),
                    queue,
                    task.__class__.__name__,
                    task.__class__.__module__,
                    data,
                    Json(dict(metadata)) if metadata is not None else None,
                    datetime.datetime.now(datetime.UTC),
                    run_at,
                    visibility_timeout,
                    max_attempts,
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
        # What `handle_tasks()` knows about the delivery. Somebody calling this directly has
        # only the id, so the queue cannot tell their delivery from a later one.
        info = self._handed_out.pop(task_id, None) or TaskInfo(
            id=task_id,
            queue=DEFAULT_QUEUE,
            delivery=None,
            attempts=0,
            visibility_timeout=0,
        )
        await self._run(_Delivery(info, task), renew_lease=False)

    async def _run(
        self,
        delivery: _Delivery[T],
        on_progress: Callable[[], None] | None = None,
        *,
        renew_lease: bool = True,
    ) -> None:
        """Run the handler, under a lease that is renewed until it finishes if asked to. Never raises."""
        info = delivery.info
        handler_done = asyncio.Event()
        renewer = (
            asyncio.create_task(self._renew_lease(info, handler_done, on_progress), name=f"lease-{info.id}")
            if renew_lease
            else None
        )

        error: Exception | None = None
        run_time: asyncio.Timeout | None = None
        try:
            if self._on_task_start is not None:
                self._on_task_start(info)
            handler = self._handler_factories[type(delivery.task)]()
            # TODO check if whether the handler is a coroutine function
            async with asyncio.timeout(self._max_run_time) as run_time:
                await handler.handle_task(delivery.task)
        except Exception as exc:  # noqa: BLE001
            error = exc
            if isinstance(exc, TimeoutError) and run_time is not None and run_time.expired():
                # The handler was cancelled, so its consumer is free again and the task
                # does not run twice at once. From here it is a failure like any other.
                error = TaskRunTimeExceededError(info.id, self._max_run_time)
        finally:
            # Stopped before the outcome is recorded: a failed task gives its lease up, and
            # a renewal after that would report the lease as lost.
            handler_done.set()
            if renewer is not None:
                # The renewer catches what it can, this is for whatever it could not
                with contextlib.suppress(Exception):
                    await renewer

        try:
            if error is None:
                owned = await self.ack(info.id, delivery=info.delivery)
            else:
                logger.error(
                    "Task %s failed",
                    info.id,
                    exc_info=error,
                )
                owned = await self._fail_task(info, error)
            if not owned:
                logger.warning("Task %s finished after its lease was lost; it may run or have run again", info.id)
        except Exception:
            # Recording the outcome failed (an unreachable database, say). Leave the task
            # unacknowledged so it is redelivered once the visibility timeout lapses,
            # rather than letting this escape into the caller's task group.
            logger.exception("Could not record the outcome of task %s", info.id)

    async def _renew_lease(
        self,
        info: TaskInfo,
        handler_done: asyncio.Event,
        on_progress: Callable[[], None] | None,
    ) -> None:
        interval = max(info.visibility_timeout / LEASE_RENEWALS_PER_TIMEOUT, 0.05)
        while not await wait_for(handler_done, interval):
            try:
                if not await self.extend_lease(info):
                    logger.warning("Lost the lease on task %s; another worker may pick it up", info.id)
                    return
                if on_progress is not None:
                    on_progress()
            except Exception:
                # Neither a database hiccup nor a broken progress callback may end the renewals
                logger.exception("Could not extend the lease on task %s", info.id)

    async def extend_lease(self, info: TaskInfo) -> bool:
        """Start the task's visibility timeout over. Returns False if the lease was lost."""
        async with self._connection_pool.connection() as conn:
            cursor = await conn.execute(
                sql.SQL("""\
                UPDATE {table}
                SET dequeued_at = NOW()
                WHERE
                    id = %s
                    AND (%s::integer IS NULL OR deliveries = %s)
                    AND acknowledged_at IS NULL
                    AND dequeued_at IS NOT NULL
                """).format(table=self._full_table_identifier),
                (info.id, info.delivery, info.delivery),
            )
            await conn.commit()
            return cursor.rowcount == 1

    async def _fail_task(self, info: TaskInfo, error: Exception) -> bool:
        """
        Schedule the next attempt with an exponential backoff, or dead-letter the task.

        Returns False if this delivery no longer owns the task.
        """
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
                    AND (%s::integer IS NULL OR deliveries = %s)
                RETURNING id, attempts, max_attempts, class_name, module_name, data, enqueued_at
                """).format(table=self._full_table_identifier),
                (self._retry_backoff, self._max_retry_backoff, info.id, info.delivery, info.delivery),
            )

            row = await cursor.fetchone()

            if row is None:
                # Already acknowledged, gone, or handed to another worker -- nothing left to retry here.
                return False

            max_attempts = row["max_attempts"] if row["max_attempts"] is not None else self._max_attempts

            if row["attempts"] < max_attempts:
                await conn.commit()
                return True

            # Out of attempts: record the payload, the attempt count and the last error so
            # the failure is visible, then acknowledge the task to take it out of rotation.
            await self._dead_letter(conn, row, "".join(traceback.format_exception(error)))
            await conn.commit()

            logger.error(
                "Task %s dead-lettered after %s attempts",
                info.id,
                row["attempts"],
            )
            return True

    async def _dead_letter(self, conn: AsyncConnection[Any], row: Mapping[str, Any], error: str) -> None:
        await conn.execute(
            sql.SQL("""\
            INSERT INTO {failure_table} (
                id, task_id, class_name, module_name, data, attempts, last_error, enqueued_at, failed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """).format(failure_table=self._full_failure_table_identifier),
            (
                str(uuid7()),
                row["id"],
                row["class_name"],
                row["module_name"],
                Json(row["data"]),
                row["attempts"],
                error,
                row["enqueued_at"],
            ),
        )
        await conn.execute(
            sql.SQL(
                "UPDATE {table} SET acknowledged_at = NOW() WHERE id = %s",
            ).format(table=self._full_table_identifier),
            (row["id"],),
        )

    async def handle_tasks(self, queue: str | None = None, *, concurrency: int | None = 10) -> None:
        """
        Run tasks as soon as they can be had, from one queue or, by default, from all of them.

        This is the queue as it worked before `consume()`: no lease renewal, so a task that
        runs longer than its visibility timeout is handed out again. Tasks go through
        `handle_task()`, which a subclass may override.

        At most `concurrency` tasks run at a time, and a task is only leased once one of them
        has finished. Without a limit, a burst of due tasks is leased all at once and every
        handler competes for the same connection pool, so tasks fail on pool timeouts that
        are charged to their attempts. None removes the limit.
        """
        if concurrency is not None and concurrency < 1:
            message = "concurrency must be at least 1, or None for no limit"
            raise ValueError(message)
        slots = asyncio.Semaphore(concurrency) if concurrency is not None else None

        async def handle_in_slot(delivery: _Delivery[T]) -> None:
            try:
                await self.handle_task(delivery.info.id, delivery.task)
            finally:
                if slots is not None:
                    slots.release()

        async with asyncio.TaskGroup() as tg:
            deliveries = aiter(self._deliveries(queue))
            while True:
                # Take a slot before leasing the next task, so the worker never holds more
                # tasks than it is running.
                if slots is not None:
                    await slots.acquire()
                try:
                    delivery = await anext(deliveries)
                except BaseException:
                    if slots is not None:
                        slots.release()
                    raise
                # Kept for `handle_task()` to pick up, which only gets the id and the task
                self._handed_out[delivery.info.id] = delivery.info
                tg.create_task(handle_in_slot(delivery))

    async def consume(
        self,
        queue: str = DEFAULT_QUEUE,
        *,
        stop: asyncio.Event,
        concurrency: int = 1,
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        """
        Handle the tasks of one queue with `concurrency` consumers until `stop` is set.

        At most `concurrency` tasks run at a time. Tasks in flight are finished before this
        returns. `on_progress` is called with the consumer's number whenever that consumer
        is demonstrably alive: on every turn of its loop and on every lease renewal while a
        long task runs, so a health check can tell a stuck worker from a busy one.

        Handlers share the event loop, so they must not block it: a blocked loop stops the
        lease renewals and the progress reports of every consumer.
        """
        wakeup = Wakeup()
        listener = asyncio.create_task(self._listen(wakeup, stop), name=f"{queue}-listener")
        consumers = [
            asyncio.create_task(
                self._consume_loop(consumer_id, queue, stop, wakeup, on_progress),
                name=f"{queue}-consumer-{consumer_id}",
            )
            for consumer_id in range(concurrency)
        ]

        stopped = asyncio.create_task(stop.wait(), name=f"{queue}-stop")
        try:
            # A consumer only returns once `stop` is set. One that ends before that has
            # died, and waiting on `stop` alone would leave a worker that looks alive but
            # no longer works its queue.
            await asyncio.wait([stopped, *consumers], return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_was_set = stop.is_set()
            stop.set()
            stopped.cancel()
            await wakeup.signal()
            results = await asyncio.gather(*consumers, return_exceptions=True)
            listener.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener

        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise errors[0]
        if not stop_was_set:
            message = f"A consumer of the {queue} queue stopped unexpectedly"
            raise RuntimeError(message)

    async def _listen(self, wakeup: Wakeup, stop: asyncio.Event) -> None:
        listen = sql.SQL("LISTEN {channel_name}").format(channel_name=sql.Identifier(self._notify_channel_name))
        unlisten = sql.SQL("UNLISTEN {channel_name}").format(channel_name=sql.Identifier(self._notify_channel_name))
        while not stop.is_set():
            try:
                async with self._connection_pool.connection() as conn:
                    await conn.execute(listen)
                    await conn.commit()
                    try:
                        # Tasks may have been enqueued while nobody was listening.
                        await wakeup.signal()
                        while not stop.is_set():
                            async for _ in conn.notifies(timeout=1.0):
                                await wakeup.signal()
                    finally:
                        # The connection goes back to the pool, which would otherwise keep listening
                        with contextlib.suppress(Exception):
                            await conn.execute(unlisten)
                            await conn.commit()
            except Exception:
                logger.exception("The task queue listener failed, reconnecting")
                await wait_for(stop, self._poll_interval)

    async def _consume_loop(
        self,
        consumer_id: int,
        queue: str,
        stop: asyncio.Event,
        wakeup: Wakeup,
        on_progress: Callable[[int], None] | None,
    ) -> None:
        def report_progress() -> None:
            if on_progress is None:
                return
            try:
                on_progress(consumer_id)
            except Exception:
                # A broken callback must not take the consumer down with it
                logger.exception("The progress callback of the %s queue failed", queue)

        while not stop.is_set():
            report_progress()

            seen_sequence = wakeup.sequence
            try:
                delivery = await self._get_task(queue)
            except Exception:
                logger.exception("Dequeuing from %s failed", queue)
                await wait_for(stop, self._poll_interval)
                continue

            if delivery is None:
                # The timeout also covers retries and redeliveries, which send no notification.
                await wakeup.wait(seen_sequence, self._poll_interval)
                continue

            await self._run(delivery, report_progress)

    async def _deliveries(self, queue: str | None) -> AsyncGenerator[_Delivery[T]]:
        async with self._connection_pool.connection() as listen_conn:
            await listen_conn.execute(
                sql.SQL("LISTEN {channel_name}").format(
                    channel_name=sql.Identifier(self._notify_channel_name),
                ),
            )
            await listen_conn.commit()

            while True:
                delivery = await self._get_task(queue)
                if delivery:
                    yield delivery
                else:
                    # We are doing long polling as well because there's no notification when a message times out
                    async for _ in listen_conn.notifies(timeout=1):
                        break

    async def _get_task(self, queue: str | None = None) -> _Delivery[T] | None:
        """Lease the next task that is due, from one queue or from any."""
        while True:
            async with self._connection_pool.connection() as conn:
                cursor = conn.cursor(row_factory=dict_row)
                cursor = await cursor.execute(
                    sql.SQL("""\
                    UPDATE {table}
                    SET dequeued_at = NOW(), deliveries = deliveries + 1
                    WHERE id = (
                        SELECT
                            id
                        FROM
                            {table}
                        WHERE
                            acknowledged_at IS NULL
                            AND run_at <= NOW()
                            AND (dequeued_at IS NULL OR dequeued_at < NOW() - make_interval(secs => visibility_timeout))
                            AND (%s::varchar IS NULL OR queue = %s)
                        ORDER BY
                            run_at,
                            enqueued_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT
                            1
                    )
                    RETURNING
                        id, queue, class_name, module_name, data, metadata,
                        deliveries, attempts, visibility_timeout, enqueued_at
                    """).format(table=self._full_table_identifier),
                    (queue, queue),
                )

                row = await cursor.fetchone()

                if row is None:
                    return None

                if self._max_deliveries is not None and row["deliveries"] > self._max_deliveries:
                    logger.error(
                        "Task %s was delivered %d times without completing, dead-lettering it",
                        row["id"],
                        self._max_deliveries,
                    )
                    await self._dead_letter(
                        conn,
                        row,
                        f"Delivered {self._max_deliveries} times without completing; "
                        "the worker probably crashed or lost the lease while running it",
                    )
                    await conn.commit()
                    continue

                try:
                    task_class = self._registered_task_class(row["module_name"], row["class_name"])
                    task = pydantic.TypeAdapter(task_class).validate_python(row["data"])
                except Exception:
                    logger.exception("Task %s cannot be deserialized, dead-lettering it", row["id"])
                    await self._dead_letter(conn, row, traceback.format_exc())
                    await conn.commit()
                    continue

                await conn.commit()

                return _Delivery(
                    TaskInfo(
                        id=row["id"],
                        queue=row["queue"],
                        delivery=row["deliveries"],
                        attempts=row["attempts"],
                        visibility_timeout=row["visibility_timeout"],
                        metadata=row["metadata"],
                    ),
                    task,
                )

    async def ack(self, task_id: uuid.UUID, *, delivery: int | None = None) -> bool:
        """
        Mark the task done. Returns False if it was already done or, when `delivery` is
        given, if that delivery no longer owns it.
        """
        async with self._connection_pool.connection() as conn:
            cursor = await conn.execute(
                sql.SQL("""\
                UPDATE {table}
                SET acknowledged_at = NOW()
                WHERE id = %s AND acknowledged_at IS NULL AND (%s::integer IS NULL OR deliveries = %s)
                """).format(
                    table=self._full_table_identifier,
                ),
                (task_id, delivery, delivery),
            )
            await conn.commit()
            return cursor.rowcount == 1

    def _registered_task_class(self, module_name: str, class_name: str) -> type[T]:
        """
        The registered task type a row names.

        Only registered types may be materialised. Resolving the row's names with importlib
        would let whoever can write to the table import any module the worker can reach and,
        through pydantic's validation of a function's arguments, call any function in it.
        """
        for task_class in self._handler_factories:
            if task_class.__module__ == module_name and task_class.__name__ == class_name:
                return task_class
        message = f"No handler is registered for task type {module_name}.{class_name}"
        raise LookupError(message)

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
