import dataclasses
import uuid
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

DEFAULT_QUEUE = "default"


@runtime_checkable
class HandlerProtocol[T](Protocol):
    async def handle_task(self, task: T) -> None: ...


@dataclasses.dataclass(frozen=True)
class TaskInfo:
    """What the queue knows about a task it handed to a worker, apart from the task itself."""

    id: uuid.UUID
    queue: str
    # Which delivery this is, counted from 1. It also identifies the delivery: once the task
    # has been handed out again, this one can no longer acknowledge it or renew its lease.
    # None when the delivery is not known, in which case that check is skipped.
    delivery: int | None
    # How many times a handler has failed on the task so far.
    attempts: int
    visibility_timeout: int
    metadata: Mapping[str, str] | None = None
