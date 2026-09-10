import asyncio
import contextlib
import dataclasses
import datetime
import inspect
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

import psycopg
import pytest
import pytest_asyncio
from psycopg import AsyncConnection, sql
from psycopg.rows import TupleRow, dict_row
from psycopg_pool import AsyncConnectionPool

from scaffold.task_queue import PostgresTaskQueue, migrations

# The queue round-trips a task through Postgres by module and class name, so this has to be
# a module-level type that `importlib.import_module` can find again.


@dataclasses.dataclass
class ExampleTask:
    payload: str


class QueueFactory(Protocol):
    async def __call__(self, **kwargs: Any) -> PostgresTaskQueue[ExampleTask]: ...  # noqa: ANN401


@pytest_asyncio.fixture
async def queue_factory(
    postgres_dsn: str,
    schema_name: str,
) -> AsyncIterator[QueueFactory]:
    """Builds queues that all share this test's schema, and closes their pools afterwards."""
    pools: list[AsyncConnectionPool[AsyncConnection[TupleRow]]] = []
    notify_channel_name = f"task_queue_notifications_{uuid.uuid4().hex}"

    async def make(**kwargs: Any) -> PostgresTaskQueue[ExampleTask]:  # noqa: ANN401
        pool = make_pool(postgres_dsn)
        pools.append(pool)
        queue = PostgresTaskQueue[ExampleTask](
            pool,
            schema_name=schema_name,
            notify_channel_name=notify_channel_name,
            **kwargs,
        )
        await pool.open()
        await migrations.upgrade(pool, schema_name=schema_name)
        await queue.init()
        return queue

    try:
        yield make
    finally:
        for pool in pools:
            await pool.close()


@contextlib.asynccontextmanager
async def running_worker(
    queue: PostgresTaskQueue[ExampleTask],
) -> AsyncGenerator[asyncio.Task[None]]:
    """Runs `handle_tasks()` in the background and tears it down on the way out."""
    worker = asyncio.create_task(queue.handle_tasks())
    try:
        yield worker
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker


async def eventually(
    condition: Callable[[], bool | Awaitable[bool]],
    *,
    timeout_seconds: float = 30.0,
    message: str = "the condition was not met in time",
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while True:
        result = condition()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return
        if loop.time() >= deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.05)


def handler_factory(
    handle: Callable[[ExampleTask], Awaitable[None]],
) -> Callable[[], Any]:
    """Wraps a plain coroutine function as the per-task handler object the queue expects."""

    class Handler:
        async def handle_task(self, task: ExampleTask) -> None:
            await handle(task)

    return Handler


async def fetch_all(dsn: str, statement: sql.Composed) -> list[dict[str, Any]]:
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        cursor = conn.cursor(row_factory=dict_row)
        cursor = await cursor.execute(statement)
        return await cursor.fetchall()


def make_pool(dsn: str) -> AsyncConnectionPool[AsyncConnection[TupleRow]]:
    """psycopg_pool is generic and invariant, so the row type has to be named explicitly."""
    return AsyncConnectionPool[AsyncConnection[TupleRow]](dsn, open=False)


def table(schema_name: str, name: str = "task") -> sql.Identifier:
    return sql.Identifier(schema_name, name)


@pytest.mark.asyncio
async def test_raising_handler_does_not_stop_siblings_or_end_the_loop(
    queue_factory: QueueFactory,
) -> None:
    handled: list[str] = []

    async def handle(task: ExampleTask) -> None:
        if task.payload == "bad":
            message = "boom"
            raise RuntimeError(message)
        handled.append(task.payload)

    # One attempt only, so the poison task dead-letters instead of retrying through the test.
    queue = await queue_factory(max_attempts=1)
    queue.register(ExampleTask, handler_factory(handle))

    await queue.enqueue(ExampleTask(payload="bad"))
    for index in range(5):
        await queue.enqueue(ExampleTask(payload=f"good-{index}"))

    async with running_worker(queue) as worker:
        await eventually(
            lambda: len(handled) == 5,
            message=f"the sibling tasks were not all handled, got {handled}",
        )
        assert sorted(handled) == [f"good-{index}" for index in range(5)]

        # The consume loop must still be alive: a task enqueued after the failure is picked up.
        assert not worker.done()
        await queue.enqueue(ExampleTask(payload="after-the-failure"))
        await eventually(
            lambda: "after-the-failure" in handled,
            message="the consume loop stopped after a handler raised",
        )
        assert not worker.done()


@pytest.mark.asyncio
async def test_failed_task_is_retried_only_after_its_backoff(
    queue_factory: QueueFactory,
) -> None:
    backoff = 3.0
    attempted_at: list[float] = []

    async def handle(task: ExampleTask) -> None:
        attempted_at.append(asyncio.get_running_loop().time())
        message = "boom"
        raise RuntimeError(message)

    queue = await queue_factory(max_attempts=5, retry_backoff=backoff)
    queue.register(ExampleTask, handler_factory(handle))

    # The visibility timeout is deliberately much longer than the backoff, so a retry that
    # arrives on time proves `run_at` drove it rather than the task simply timing out.
    await queue.enqueue(ExampleTask(payload="always-fails"), visibility_timeout=600)

    async with running_worker(queue):
        await eventually(
            lambda: len(attempted_at) >= 2,
            message="the failed task was never retried",
        )

    gap = attempted_at[1] - attempted_at[0]
    # A little slack below for clock skew between the loop and the server; above for the
    # one-second poll interval that decides how soon a due task is noticed.
    assert gap >= backoff - 0.5, f"retried after {gap:.2f}s, before the {backoff}s backoff elapsed"
    assert gap <= backoff + 5, f"retried after {gap:.2f}s, far later than the {backoff}s backoff"


@pytest.mark.asyncio
async def test_task_is_dead_lettered_after_max_attempts(
    queue_factory: QueueFactory,
    postgres_dsn: str,
    schema_name: str,
) -> None:
    max_attempts = 3
    attempts: list[str] = []

    async def handle(task: ExampleTask) -> None:
        attempts.append(task.payload)
        message = "smtp is down"
        raise RuntimeError(message)

    queue = await queue_factory(max_attempts=max_attempts, retry_backoff=0.05)
    queue.register(ExampleTask, handler_factory(handle))

    await queue.enqueue(ExampleTask(payload="doomed"))

    async def is_dead_lettered() -> bool:
        rows = await fetch_all(
            postgres_dsn,
            sql.SQL("SELECT * FROM {failures}").format(
                failures=table(schema_name, "task_failure"),
            ),
        )
        return bool(rows)

    async with running_worker(queue):
        await eventually(is_dead_lettered, message="the task was never dead-lettered")
        # Give any further redelivery a chance to show up before asserting the count.
        await asyncio.sleep(1.0)

    assert len(attempts) == max_attempts

    failures = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT * FROM {failures}").format(
            failures=table(schema_name, "task_failure"),
        ),
    )
    assert len(failures) == 1
    failure = failures[0]
    assert failure["attempts"] == max_attempts
    assert failure["data"] == {"payload": "doomed"}
    assert failure["class_name"] == "ExampleTask"
    assert failure["module_name"] == ExampleTask.__module__
    assert "smtp is down" in failure["last_error"]
    assert "RuntimeError" in failure["last_error"]

    # A dead-lettered task is acknowledged, so it is out of rotation rather than looping forever.
    tasks = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT * FROM {tasks}").format(tasks=table(schema_name)),
    )
    assert len(tasks) == 1
    assert tasks[0]["acknowledged_at"] is not None
    assert tasks[0]["attempts"] == max_attempts


@pytest.mark.asyncio
async def test_concurrent_workers_never_handle_the_same_task(
    queue_factory: QueueFactory,
) -> None:
    task_count = 40
    handled: list[tuple[str, str]] = []

    def handle_as(worker_name: str) -> Callable[[ExampleTask], Awaitable[None]]:
        async def handle(task: ExampleTask) -> None:
            handled.append((worker_name, task.payload))
            # Hold the task briefly to widen the window in which the other worker could
            # pick the same row up.
            await asyncio.sleep(0.05)

        return handle

    # Two independent queues, each with its own connection pool, over one shared schema.
    queue_a = await queue_factory()
    queue_b = await queue_factory()
    queue_a.register(ExampleTask, handler_factory(handle_as("a")))
    queue_b.register(ExampleTask, handler_factory(handle_as("b")))

    for index in range(task_count):
        await queue_a.enqueue(
            ExampleTask(payload=f"task-{index}"),
            visibility_timeout=600,
        )

    async with running_worker(queue_a), running_worker(queue_b):
        await eventually(
            lambda: len(handled) >= task_count,
            message=f"only {len(handled)} of {task_count} tasks were handled",
        )
        # Let any duplicate delivery surface before asserting there was none.
        await asyncio.sleep(1.0)

    payloads = [payload for _, payload in handled]
    assert sorted(payloads) == sorted(f"task-{index}" for index in range(task_count))
    assert len(set(payloads)) == task_count, "a task was handled more than once"

    # Sanity check that the work really was shared, otherwise this proves little.
    workers_used = {worker_name for worker_name, _ in handled}
    assert workers_used == {"a", "b"}


@pytest.mark.asyncio
async def test_scheduled_task_is_not_dequeued_before_its_run_at(
    queue_factory: QueueFactory,
) -> None:
    handled: list[str] = []

    async def handle(task: ExampleTask) -> None:
        handled.append(task.payload)

    queue = await queue_factory()
    queue.register(ExampleTask, handler_factory(handle))

    now = datetime.datetime.now(datetime.UTC)
    await queue.enqueue(
        ExampleTask(payload="later"),
        run_at=now + datetime.timedelta(seconds=3),
    )
    await queue.enqueue(ExampleTask(payload="now"))

    async with running_worker(queue):
        await eventually(
            lambda: "now" in handled,
            message="the due task was not handled",
        )
        assert "later" not in handled, "a task scheduled for the future was handled early"
        await eventually(
            lambda: "later" in handled,
            message="the scheduled task was never handled once it came due",
        )


@pytest.mark.asyncio
async def test_upgrade_installs_the_schema_and_records_the_version(
    postgres_dsn: str,
    schema_name: str,
) -> None:
    async with make_pool(postgres_dsn) as pool:
        await pool.open()
        assert await migrations.current_version(pool, schema_name=schema_name) == 0

        assert await migrations.upgrade(pool, schema_name=schema_name) == migrations.LATEST_VERSION
        assert await migrations.current_version(pool, schema_name=schema_name) == migrations.LATEST_VERSION

        # Every migration is recorded, not just the latest.
        versions = await fetch_all(
            postgres_dsn,
            sql.SQL("SELECT version FROM {version} ORDER BY version").format(
                version=table(schema_name, "task_schema_version"),
            ),
        )
        assert [row["version"] for row in versions] == [1, migrations.LATEST_VERSION]

        # Re-running is a no-op rather than an error.
        assert await migrations.upgrade(pool, schema_name=schema_name) == migrations.LATEST_VERSION


@pytest.mark.asyncio
async def test_upgrade_stamps_and_migrates_a_pre_versioning_schema(
    postgres_dsn: str,
    schema_name: str,
) -> None:
    """A database created by the old `init()` has the v1 table but no version table."""
    enqueued_at = datetime.datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001

    async with await psycopg.AsyncConnection.connect(postgres_dsn) as conn:
        await conn.execute(
            sql.SQL("CREATE SCHEMA {schema}").format(
                schema=sql.Identifier(schema_name),
            ),
        )
        await conn.execute(
            sql.SQL("""\
            CREATE TABLE {tasks} (
                id UUID PRIMARY KEY,
                class_name VARCHAR NOT NULL,
                module_name VARCHAR NOT NULL,
                data JSONB NOT NULL,
                enqueued_at TIMESTAMP NOT NULL,
                dequeued_at TIMESTAMP,
                acknowledged_at TIMESTAMP,
                visibility_timeout INTEGER NOT NULL
            )
            """).format(tasks=table(schema_name)),
        )
        await conn.execute(
            sql.SQL("""\
            INSERT INTO {tasks} (id, class_name, module_name, data, enqueued_at, visibility_timeout)
            VALUES (%s, %s, %s, %s, %s, %s)
            """).format(tasks=table(schema_name)),
            (
                str(uuid.uuid4()),
                "ExampleTask",
                ExampleTask.__module__,
                '{"payload": "from-the-old-schema"}',
                enqueued_at,
                600,
            ),
        )
        await conn.commit()

    async with make_pool(postgres_dsn) as pool:
        await pool.open()
        # Detected as the baseline rather than reported as an empty database.
        assert await migrations.current_version(pool, schema_name=schema_name) == 1
        await migrations.upgrade(pool, schema_name=schema_name)

    rows = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT run_at, attempts FROM {tasks}").format(
            tasks=table(schema_name),
        ),
    )
    assert rows[0]["run_at"] == enqueued_at, "run_at was not backfilled from enqueued_at"
    assert rows[0]["attempts"] == 0

    versions = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT version FROM {version} ORDER BY version").format(
            version=table(schema_name, "task_schema_version"),
        ),
    )
    assert [row["version"] for row in versions] == [2], "migration 1 must not be replayed"


@pytest.mark.asyncio
async def test_concurrent_upgrades_do_not_race(
    postgres_dsn: str,
    schema_name: str,
) -> None:
    """The advisory lock has to serialise deployments starting at the same moment.

    Without it, concurrent `CREATE SCHEMA IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` can
    fail on Postgres' catalogue unique indexes, which those statements do not guard.
    """
    worker_count = 8
    pools = [make_pool(postgres_dsn) for _ in range(worker_count)]
    try:
        await asyncio.gather(*(pool.open() for pool in pools))
        results = await asyncio.gather(
            *(migrations.upgrade(pool, schema_name=schema_name) for pool in pools),
        )
    finally:
        await asyncio.gather(*(pool.close() for pool in pools))

    assert results == [migrations.LATEST_VERSION] * worker_count

    # Each migration was applied exactly once, despite every worker racing to apply them.
    versions = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT version FROM {version} ORDER BY version").format(
            version=table(schema_name, "task_schema_version"),
        ),
    )
    assert [row["version"] for row in versions] == [1, migrations.LATEST_VERSION]


@pytest.mark.asyncio
async def test_init_refuses_to_start_against_an_unmigrated_schema(
    postgres_dsn: str,
    schema_name: str,
) -> None:
    async with make_pool(postgres_dsn) as pool:
        queue = PostgresTaskQueue[ExampleTask](pool, schema_name=schema_name)

        with pytest.raises(migrations.SchemaVersionMismatchError) as excinfo:
            await queue.init()

        assert excinfo.value.found == 0
        assert excinfo.value.expected == migrations.LATEST_VERSION
        assert "upgrade" in str(excinfo.value)


def _run_cli(dsn: str, *args: str) -> str:
    """Invokes the CLI the way a deployment script would."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "scaffold.task_queue", *args],
        env={**os.environ, "DATABASE_URL": dsn, "PYTHONPATH": "src"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.asyncio
async def test_cli_upgrade_installs_the_schema(
    postgres_dsn: str,
    schema_name: str,
) -> None:
    output = _run_cli(postgres_dsn, "--schema", schema_name, "upgrade")

    assert str(migrations.LATEST_VERSION) in output

    async with make_pool(postgres_dsn) as pool:
        queue = PostgresTaskQueue[ExampleTask](pool, schema_name=schema_name)
        await queue.init()  # Raises unless the CLI produced the version this build expects.
