"""Helpers for tests of a web app."""

from contextlib import AbstractAsyncContextManager
from typing import Protocol

from quart.sessions import SessionMixin


# Quart leaves a part of the signature of session_transaction untyped
class _HasSessionTransaction(Protocol):
    def session_transaction(self) -> AbstractAsyncContextManager[SessionMixin]: ...


def session_transaction(client: _HasSessionTransaction) -> AbstractAsyncContextManager[SessionMixin]:
    """The session of a test client, to read or change: `async with session_transaction(client) as session:`."""
    return client.session_transaction()
