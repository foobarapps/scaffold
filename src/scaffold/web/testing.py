"""Helpers for tests of a web app."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from quart.sessions import SessionMixin


# Quart leaves a part of the signature of session_transaction untyped
class _HasSessionTransaction(Protocol):
    def session_transaction(
        self,
        path: str = "/",
        *,
        headers: dict[str, str] | None = None,
    ) -> AbstractAsyncContextManager[SessionMixin]: ...


def session_transaction(
    client: _HasSessionTransaction,
    path: str = "/",
    *,
    headers: dict[str, str] | None = None,
) -> AbstractAsyncContextManager[SessionMixin]:
    """
    The session of a test client, to read or change: `async with session_transaction(client) as session:`.

    `path` and `headers` are those of the request the session is opened for, e.g. a path
    under a cookie path of its own.
    """
    return client.session_transaction(path, headers=headers)
