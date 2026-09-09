import uuid
from collections.abc import Iterator

import pytest
from testcontainers.community.postgres import PostgresContainer


@pytest.fixture(scope="session")
def postgres_dsn() -> Iterator[str]:
    """A libpq connection string pointing at a throwaway Postgres, shared by the whole run."""
    with PostgresContainer("postgres:16-alpine", driver=None) as container:
        yield container.get_connection_url()


@pytest.fixture
def schema_name() -> str:
    """A schema unique to one test, so tests sharing the container cannot see each other's rows."""
    return f"test_{uuid.uuid4().hex}"
