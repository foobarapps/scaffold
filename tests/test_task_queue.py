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
from pydantic_core import PydanticSerializationError

from scaffold.task_queue import GenericFakeTaskQueue, PostgresTaskQueue, TaskInfo, migrations

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
        assert [row["version"] for row in versions] == list(range(1, migrations.LATEST_VERSION + 1))

        # Re-running is a no-op rather than an error.
        assert await migrations.upgrade(pool, schema_name=schema_name) == migrations.LATEST_VERSION


@pytest.mark.asyncio
async def test_upgrade_stamps_and_migrates_a_pre_versioning_schema(
    postgres_dsn: str,
    schema_name: str,
) -> None:
    """A database created by the old `init()` has the v1 table but no version table."""
    enqueued_at = datetime.datetime(2026, 1, 1, 12, 0, 0)

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
    assert [row["version"] for row in versions] == list(
        range(2, migrations.LATEST_VERSION + 1),
    ), "migration 1 must not be replayed"


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
    assert [row["version"] for row in versions] == list(range(1, migrations.LATEST_VERSION + 1))


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


@contextlib.asynccontextmanager
async def consuming(
    queue: PostgresTaskQueue[ExampleTask],
    queue_name: str = "default",
    *,
    concurrency: int = 1,
    on_progress: Callable[[int], None] | None = None,
) -> AsyncGenerator[asyncio.Event]:
    """Runs `consume()` in the background and stops it gracefully on the way out."""
    stop = asyncio.Event()
    consumer = asyncio.create_task(
        queue.consume(queue_name, stop=stop, concurrency=concurrency, on_progress=on_progress),
    )
    try:
        yield stop
    finally:
        stop.set()
        await asyncio.wait_for(consumer, timeout=30)


@pytest.mark.asyncio
async def test_consume_handles_and_acknowledges_tasks(
    queue_factory: QueueFactory,
    postgres_dsn: str,
    schema_name: str,
) -> None:
    queue = await queue_factory()
    handled: list[str] = []

    async def handle(task: ExampleTask) -> None:
        handled.append(task.payload)

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("before"))

    async with consuming(queue):
        await queue.enqueue(ExampleTask("during"))
        await eventually(lambda: sorted(handled) == ["before", "during"])

    rows = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT acknowledged_at, deliveries FROM {task}").format(task=table(schema_name)),
    )
    assert all(row["acknowledged_at"] is not None for row in rows)
    assert [row["deliveries"] for row in rows] == [1, 1]


@pytest.mark.asyncio
async def test_queues_are_isolated(queue_factory: QueueFactory) -> None:
    queue = await queue_factory()
    handled: list[str] = []

    async def handle(task: ExampleTask) -> None:
        handled.append(task.payload)

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("slow"), queue="low-priority")
    await queue.enqueue(ExampleTask("fast"))

    async with consuming(queue, "default"):
        await eventually(lambda: handled == ["fast"])
        await asyncio.sleep(0.3)
        assert handled == ["fast"]

    async with consuming(queue, "low-priority"):
        await eventually(lambda: handled == ["fast", "slow"])


@pytest.mark.asyncio
async def test_consume_never_runs_more_tasks_at_once_than_its_concurrency(queue_factory: QueueFactory) -> None:
    queue = await queue_factory()
    running = 0
    most_running = 0
    done = 0

    async def handle(task: ExampleTask) -> None:
        nonlocal running, most_running, done
        running += 1
        most_running = max(most_running, running)
        await asyncio.sleep(0.1)
        running -= 1
        done += 1

    queue.register(ExampleTask, handler_factory(handle))
    for number in range(6):
        await queue.enqueue(ExampleTask(str(number)))

    async with consuming(queue, concurrency=2):
        await eventually(lambda: done == 6)

    assert most_running == 2


@pytest.mark.asyncio
async def test_consume_finishes_the_task_in_flight_before_it_stops(queue_factory: QueueFactory) -> None:
    queue = await queue_factory()
    started = asyncio.Event()
    finished: list[str] = []

    async def handle(task: ExampleTask) -> None:
        started.set()
        await asyncio.sleep(0.3)
        finished.append(task.payload)

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("in flight"))

    async with consuming(queue):
        await asyncio.wait_for(started.wait(), timeout=30)

    assert finished == ["in flight"]


@pytest.mark.asyncio
async def test_lease_is_renewed_while_a_handler_outlives_the_visibility_timeout(queue_factory: QueueFactory) -> None:
    queue = await queue_factory()
    runs = 0
    done = asyncio.Event()

    async def handle(task: ExampleTask) -> None:
        nonlocal runs
        runs += 1
        # Three times the visibility timeout: without a renewal the second consumer would take it
        await asyncio.sleep(3)
        done.set()

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("long"), visibility_timeout=1)

    async with consuming(queue, concurrency=2):
        await asyncio.wait_for(done.wait(), timeout=30)

    assert runs == 1


@pytest.mark.asyncio
async def test_progress_is_reported_while_idle_and_while_a_long_task_runs(queue_factory: QueueFactory) -> None:
    queue = await queue_factory(poll_interval=0.1)
    progress: list[int] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(task: ExampleTask) -> None:
        started.set()
        await release.wait()

    queue.register(ExampleTask, handler_factory(handle))

    async with consuming(queue, on_progress=progress.append):
        await eventually(lambda: len(progress) >= 2, message="an idle consumer reports progress")

        await queue.enqueue(ExampleTask("long"), visibility_timeout=1)
        await asyncio.wait_for(started.wait(), timeout=30)
        reported_before = len(progress)
        await eventually(
            lambda: len(progress) >= reported_before + 2,
            message="a consumer running a long task reports progress on lease renewals",
        )
        release.set()

    assert set(progress) == {0}


@pytest.mark.asyncio
async def test_task_can_limit_its_own_attempts(
    queue_factory: QueueFactory,
    postgres_dsn: str,
    schema_name: str,
) -> None:
    queue = await queue_factory(max_attempts=5, retry_backoff=0.01)
    runs = 0

    async def handle(task: ExampleTask) -> None:
        nonlocal runs
        runs += 1
        message = "not safe to repeat"
        raise RuntimeError(message)

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("once"), max_attempts=1)

    async def failures() -> list[dict[str, Any]]:
        return await fetch_all(
            postgres_dsn,
            sql.SQL("SELECT attempts, last_error FROM {failure}").format(failure=table(schema_name, "task_failure")),
        )

    async def dead_lettered() -> bool:
        return len(await failures()) == 1

    async with consuming(queue):
        await eventually(dead_lettered)
        await asyncio.sleep(0.3)

    assert runs == 1
    recorded = await failures()
    assert recorded[0]["attempts"] == 1
    assert "not safe to repeat" in recorded[0]["last_error"]


@pytest.mark.asyncio
async def test_task_delivered_too_many_times_is_dead_lettered(
    queue_factory: QueueFactory,
    postgres_dsn: str,
    schema_name: str,
) -> None:
    queue = await queue_factory(max_deliveries=2)
    await queue.enqueue(ExampleTask("kills its worker"), visibility_timeout=0)

    # Two workers take the task and die before they can report anything
    assert await queue._get_task() is not None  # noqa: SLF001
    assert await queue._get_task() is not None  # noqa: SLF001
    assert await queue._get_task() is None  # noqa: SLF001

    failures = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT last_error FROM {failure}").format(failure=table(schema_name, "task_failure")),
    )
    assert len(failures) == 1
    assert "Delivered 2 times without completing" in failures[0]["last_error"]


@pytest.mark.asyncio
async def test_task_that_cannot_be_deserialized_is_dead_lettered_and_skipped(
    queue_factory: QueueFactory,
    postgres_dsn: str,
    schema_name: str,
) -> None:
    queue = await queue_factory()
    handled: list[str] = []

    async def handle(task: ExampleTask) -> None:
        handled.append(task.payload)

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("broken"))
    async with await psycopg.AsyncConnection.connect(postgres_dsn) as conn:
        await conn.execute(
            sql.SQL("UPDATE {task} SET class_name = 'NoSuchTask'").format(task=table(schema_name)),
        )
    await queue.enqueue(ExampleTask("fine"))

    async with consuming(queue):
        await eventually(lambda: handled == ["fine"])

    failures = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT class_name, last_error FROM {failure}").format(failure=table(schema_name, "task_failure")),
    )
    assert [failure["class_name"] for failure in failures] == ["NoSuchTask"]
    assert "AttributeError" in failures[0]["last_error"]


@pytest.mark.asyncio
async def test_stale_delivery_cannot_acknowledge_or_renew_after_redelivery(queue_factory: QueueFactory) -> None:
    queue = await queue_factory()
    await queue.enqueue(ExampleTask("contended"), visibility_timeout=0)

    first = await queue._get_task()  # noqa: SLF001
    second = await queue._get_task()  # noqa: SLF001
    assert first is not None
    assert second is not None
    assert (first.info.delivery, second.info.delivery) == (1, 2)

    assert not await queue.extend_lease(first.info)
    assert not await queue.ack(first.info.id, delivery=first.info.delivery)
    assert await queue.extend_lease(second.info)
    assert await queue.ack(second.info.id, delivery=second.info.delivery)


@pytest.mark.asyncio
async def test_metadata_travels_with_the_task_to_on_task_start(queue_factory: QueueFactory) -> None:
    started: list[TaskInfo] = []
    queue = await queue_factory(on_task_start=started.append)
    handled = asyncio.Event()

    async def handle(task: ExampleTask) -> None:
        handled.set()

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("for a user"), queue="reports", metadata={"user_id": "42"})

    async with consuming(queue, "reports"):
        await asyncio.wait_for(handled.wait(), timeout=30)

    assert len(started) == 1
    assert started[0].queue == "reports"
    assert started[0].delivery == 1
    assert started[0].metadata == {"user_id": "42"}


@dataclasses.dataclass
class CountTask:
    count: int


@pytest.mark.asyncio
async def test_strict_serialization_refuses_a_value_that_does_not_match_its_declared_type(
    postgres_dsn: str,
    schema_name: str,
) -> None:
    async with make_pool(postgres_dsn) as pool:
        await migrations.upgrade(pool, schema_name=schema_name)
        queue = PostgresTaskQueue[CountTask](pool, schema_name=schema_name, strict_serialization=True)

        with pytest.raises(PydanticSerializationError):
            await queue.enqueue(CountTask(count="three"))  # type: ignore[arg-type]

        await queue.enqueue(CountTask(count=3))


@pytest.mark.asyncio
async def test_enqueue_opens_the_pool_itself(postgres_dsn: str, schema_name: str) -> None:
    async with make_pool(postgres_dsn) as migration_pool:
        await migrations.upgrade(migration_pool, schema_name=schema_name)

    pool = make_pool(postgres_dsn)
    try:
        # No init(): a process that only enqueues does not have to check the schema version
        await PostgresTaskQueue[ExampleTask](pool, schema_name=schema_name).enqueue(ExampleTask("from the web app"))
    finally:
        await pool.close()

    rows = await fetch_all(postgres_dsn, sql.SQL("SELECT queue FROM {task}").format(task=table(schema_name)))
    assert [row["queue"] for row in rows] == ["default"]


@pytest.mark.asyncio
async def test_broken_progress_callback_does_not_stop_the_consumer(queue_factory: QueueFactory) -> None:
    queue = await queue_factory(poll_interval=0.1)
    handled: list[str] = []
    calls = 0

    def on_progress(consumer_id: int) -> None:
        nonlocal calls
        calls += 1
        message = "the health check is broken"
        raise RuntimeError(message)

    async def handle(task: ExampleTask) -> None:
        # Long enough for a lease renewal, which reports progress too
        await asyncio.sleep(0.6)
        handled.append(task.payload)

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("first"), visibility_timeout=1)
    await queue.enqueue(ExampleTask("second"), visibility_timeout=1)

    async with consuming(queue, on_progress=on_progress):
        await eventually(lambda: handled == ["first", "second"])

    assert calls > 0


@pytest.mark.asyncio
async def test_consume_fails_when_a_consumer_dies(queue_factory: QueueFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = await queue_factory()

    async def die(*args: object, **kwargs: object) -> None:
        message = "the consumer died"
        raise RuntimeError(message)

    monkeypatch.setattr(queue, "_run", die)
    queue.register(ExampleTask, handler_factory(die))
    await queue.enqueue(ExampleTask("fatal"))

    # Not stopped by anyone: the process has to find out, so that it can be restarted
    with pytest.raises(RuntimeError, match="the consumer died"):
        await asyncio.wait_for(queue.consume(stop=asyncio.Event(), concurrency=2), timeout=30)


@pytest.mark.asyncio
async def test_hung_handler_loses_its_lease_after_the_maximum_run_time(queue_factory: QueueFactory) -> None:
    queue = await queue_factory(max_run_time=1.0, poll_interval=0.1)
    runs = 0
    second_run = asyncio.Event()
    release = asyncio.Event()

    async def handle(task: ExampleTask) -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            # Hangs for good, as far as the queue can tell
            await release.wait()
        else:
            second_run.set()

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("hangs"), visibility_timeout=1, max_attempts=1)

    async with consuming(queue, concurrency=2):
        await asyncio.wait_for(second_run.wait(), timeout=30)
        release.set()

    assert runs == 2


@pytest.mark.asyncio
async def test_handle_tasks_does_not_renew_the_lease(queue_factory: QueueFactory) -> None:
    queue = await queue_factory()
    runs = 0
    second_run = asyncio.Event()

    async def handle(task: ExampleTask) -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            await asyncio.sleep(3)
        else:
            second_run.set()

    queue.register(ExampleTask, handler_factory(handle))
    await queue.enqueue(ExampleTask("outlives its visibility timeout"), visibility_timeout=1)

    async with running_worker(queue):
        # Handed out again once the visibility timeout is over, as before consume() existed
        await asyncio.wait_for(second_run.wait(), timeout=30)


class CountingQueue(PostgresTaskQueue[ExampleTask]):
    handled_ids: list[uuid.UUID]

    async def handle_task(self, task_id: uuid.UUID, task: ExampleTask) -> None:
        self.handled_ids.append(task_id)
        await super().handle_task(task_id, task)


@pytest.mark.asyncio
async def test_handle_tasks_goes_through_an_overridden_handle_task(postgres_dsn: str, schema_name: str) -> None:
    async with make_pool(postgres_dsn) as pool:
        await migrations.upgrade(pool, schema_name=schema_name)
        queue = CountingQueue(pool, schema_name=schema_name, notify_channel_name=f"n_{uuid.uuid4().hex}")
        queue.handled_ids = []
        handled: list[str] = []

        async def handle(task: ExampleTask) -> None:
            handled.append(task.payload)

        queue.register(ExampleTask, handler_factory(handle))
        await queue.init()
        await queue.enqueue(ExampleTask("through the override"))

        async with running_worker(queue):
            await eventually(lambda: handled == ["through the override"])

        assert len(queue.handled_ids) == 1


@pytest.mark.asyncio
async def test_init_accepts_a_schema_newer_than_this_build(postgres_dsn: str, schema_name: str) -> None:
    async with make_pool(postgres_dsn) as pool:
        await migrations.upgrade(pool, schema_name=schema_name)
        async with pool.connection() as conn:
            await conn.execute(
                sql.SQL("INSERT INTO {version} (version, applied_at) VALUES (%s, NOW())").format(
                    version=table(schema_name, "task_schema_version"),
                ),
                (migrations.LATEST_VERSION + 1,),
            )

        # A later release only adds to the schema, so this build can still work with it,
        # which is what makes rolling a deploy back possible.
        await PostgresTaskQueue[ExampleTask](pool, schema_name=schema_name).init()


@pytest.mark.asyncio
async def test_upgrade_keeps_the_index_that_serves_every_queue_at_once(postgres_dsn: str, schema_name: str) -> None:
    async with make_pool(postgres_dsn) as pool:
        await migrations.upgrade(pool, schema_name=schema_name)

    indexes = await fetch_all(
        postgres_dsn,
        sql.SQL("SELECT indexname FROM pg_indexes WHERE schemaname = {schema}").format(schema=sql.Literal(schema_name)),
    )
    assert {"task_pending_idx", "task_queue_pending_idx"} <= {row["indexname"] for row in indexes}


def test_fake_queue_takes_what_the_real_one_takes() -> None:
    queue = GenericFakeTaskQueue[ExampleTask]()
    task = ExampleTask("for a user")

    queue.enqueue(task, queue="reports", max_attempts=1, metadata={"user_id": "42"})

    assert queue.queue == [task]
    assert (queue.enqueued[0].queue, queue.enqueued[0].max_attempts) == ("reports", 1)
    assert queue.enqueued[0].metadata == {"user_id": "42"}
    assert inspect.signature(queue.enqueue).parameters.keys() == inspect.signature(
        PostgresTaskQueue.enqueue
    ).parameters.keys() - {"self"}
