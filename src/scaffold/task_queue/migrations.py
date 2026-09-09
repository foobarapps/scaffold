"""Versioned schema migrations for `PostgresTaskQueue`.

The queue owns its schema and records its version in a table of its own, independent of
whatever migration tool the host application uses for its own tables. Apply pending
migrations from a deployment script::

    DATABASE_URL=... python -m scaffold.task_queue upgrade

Migrations run at deploy time rather than at worker start-up: the DDL takes an ACCESS
EXCLUSIVE lock on a live queue table, so when it happens is the operator's call, and the
runtime database role never needs DDL privileges. `PostgresTaskQueue.init()` only checks
the recorded version and refuses to start against a schema it does not understand.
"""

import argparse
import asyncio
import dataclasses
import os
from collections.abc import Callable, Sequence

from psycopg import AsyncConnection, sql
from psycopg_pool import AsyncConnectionPool


@dataclasses.dataclass(frozen=True)
class _Names:
    """Every identifier a migration might need, derived from the queue's configuration."""

    schema_name: str
    table_name: str

    @property
    def schema(self) -> sql.Identifier:
        return sql.Identifier(self.schema_name)

    @property
    def task(self) -> sql.Identifier:
        return sql.Identifier(self.schema_name, self.table_name)

    @property
    def failure(self) -> sql.Identifier:
        return sql.Identifier(self.schema_name, f"{self.table_name}_failure")

    @property
    def version(self) -> sql.Identifier:
        return sql.Identifier(self.schema_name, self.version_table_name)

    @property
    def version_table_name(self) -> str:
        return f"{self.table_name}_schema_version"

    @property
    def pending_index(self) -> sql.Identifier:
        return sql.Identifier(f"{self.table_name}_pending_idx")


def _create_task_table(names: _Names) -> list[sql.Composed]:
    return [
        sql.SQL("""\
        CREATE TABLE {task} (
            id UUID PRIMARY KEY,
            class_name VARCHAR NOT NULL,
            module_name VARCHAR NOT NULL,
            data JSONB NOT NULL,
            enqueued_at TIMESTAMP NOT NULL,
            dequeued_at TIMESTAMP,
            acknowledged_at TIMESTAMP,
            visibility_timeout INTEGER NOT NULL
        )
        """).format(task=names.task),
    ]


def _add_retries_and_dead_lettering(names: _Names) -> list[sql.Composed]:
    return [
        sql.SQL("ALTER TABLE {task} ADD COLUMN run_at TIMESTAMP").format(task=names.task),
        sql.SQL("UPDATE {task} SET run_at = enqueued_at").format(task=names.task),
        sql.SQL("ALTER TABLE {task} ALTER COLUMN run_at SET NOT NULL").format(task=names.task),
        sql.SQL("ALTER TABLE {task} ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0").format(task=names.task),
        sql.SQL("CREATE INDEX {index} ON {task} (run_at) WHERE acknowledged_at IS NULL").format(
            index=names.pending_index,
            task=names.task,
        ),
        sql.SQL("""\
        CREATE TABLE {failure} (
            id UUID PRIMARY KEY,
            task_id UUID NOT NULL,
            class_name VARCHAR NOT NULL,
            module_name VARCHAR NOT NULL,
            data JSONB NOT NULL,
            attempts INTEGER NOT NULL,
            last_error TEXT NOT NULL,
            enqueued_at TIMESTAMP NOT NULL,
            failed_at TIMESTAMP NOT NULL
        )
        """).format(failure=names.failure),
    ]


# A migration's version is its position in this tuple, so it may only ever be appended to.
MIGRATIONS: tuple[Callable[[_Names], list[sql.Composed]], ...] = (
    _create_task_table,
    _add_retries_and_dead_lettering,
)

LATEST_VERSION = len(MIGRATIONS)

# Version 1 is the shape that shipped before this module existed, when `init()` created the
# table itself. A database holding that table but no version table is taken to be at this
# version rather than having migration 1 replayed over it.
_BASELINE_VERSION = 1


class SchemaVersionMismatchError(RuntimeError):
    """The task queue schema is not at the version this build of the code expects."""

    def __init__(self, found: int, expected: int) -> None:
        self.found = found
        self.expected = expected
        action = "upgrade" if found < expected else "deploy a newer version of the application"
        super().__init__(
            f"The task queue schema is at version {found}, but this build expects {expected}. "
            f"Run `python -m scaffold.task_queue upgrade` to {action}.",
        )


def _advisory_lock(names: _Names) -> sql.Composed:
    # Serialises concurrent upgrades. Without it, two deployments racing on `CREATE SCHEMA
    # IF NOT EXISTS` / `CREATE TABLE IF NOT EXISTS` can fail on Postgres' own catalogue
    # unique indexes, which those statements do not protect against.
    key = f"scaffold.task_queue.{names.schema_name}.{names.table_name}"
    return sql.SQL("SELECT pg_advisory_xact_lock(hashtext({key}))").format(key=sql.Literal(key))


def _table_exists(schema_name: str, table_name: str) -> sql.Composed:
    return sql.SQL("""\
    SELECT EXISTS (
        SELECT 1 FROM pg_catalog.pg_tables WHERE schemaname = {schema} AND tablename = {table}
    )
    """).format(schema=sql.Literal(schema_name), table=sql.Literal(table_name))


async def _scalar(conn: AsyncConnection, statement: sql.Composed) -> object:
    cursor = await conn.execute(statement)
    row = await cursor.fetchone()
    return None if row is None else row[0]


async def _read_version(conn: AsyncConnection, names: _Names) -> int:
    if await _scalar(conn, _table_exists(names.schema_name, names.version_table_name)):
        recorded = await _scalar(
            conn,
            sql.SQL("SELECT COALESCE(MAX(version), 0) FROM {version}").format(version=names.version),
        )
        assert isinstance(recorded, int)
        if recorded > 0:
            return recorded

    # No version recorded. A database that already has the task table predates this module,
    # so take it as the baseline rather than replaying migration 1 over an existing table.
    if await _scalar(conn, _table_exists(names.schema_name, names.table_name)):
        return _BASELINE_VERSION

    return 0


async def current_version(
    connection_pool: AsyncConnectionPool,
    *,
    schema_name: str = "public",
    table_name: str = "task",
) -> int:
    """The schema version recorded in the database, or 0 if the queue is not installed."""
    names = _Names(schema_name=schema_name, table_name=table_name)
    async with connection_pool.connection() as conn:
        return await _read_version(conn, names)


async def upgrade(
    connection_pool: AsyncConnectionPool,
    *,
    schema_name: str = "public",
    table_name: str = "task",
) -> int:
    """Apply every pending migration in a single transaction and return the resulting version."""
    names = _Names(schema_name=schema_name, table_name=table_name)

    async with connection_pool.connection() as conn:
        await conn.execute(_advisory_lock(names))
        await conn.execute(
            sql.SQL("CREATE SCHEMA IF NOT EXISTS {schema}").format(schema=names.schema),
        )
        await conn.execute(
            sql.SQL("""\
            CREATE TABLE IF NOT EXISTS {version} (
                version INTEGER PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL
            )
            """).format(version=names.version),
        )

        version = await _read_version(conn, names)

        for pending, build in enumerate(MIGRATIONS[version:], start=version + 1):
            for statement in build(names):
                await conn.execute(statement)
            await conn.execute(
                sql.SQL("INSERT INTO {version} (version, applied_at) VALUES ({value}, NOW())").format(
                    version=names.version,
                    value=sql.Literal(pending),
                ),
            )

        await conn.commit()

    return max(version, LATEST_VERSION)


async def _run_upgrade(dsn: str, schema_name: str, table_name: str) -> int:
    async with AsyncConnectionPool(dsn, open=False) as pool:
        await pool.open()
        return await upgrade(pool, schema_name=schema_name, table_name=table_name)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m scaffold.task_queue",
        description="Apply pending PostgresTaskQueue schema migrations.",
    )
    parser.add_argument("command", choices=["upgrade"])
    parser.add_argument("--schema", default="public", help="schema holding the queue (default: public)")
    parser.add_argument("--table", default="task", help="name of the task table (default: task)")
    args = parser.parse_args(argv)

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        message = "Set DATABASE_URL to the queue's database connection string."
        raise SystemExit(message)

    version = asyncio.run(_run_upgrade(dsn, args.schema, args.table))
    print(f"Task queue schema is at version {version}.")
