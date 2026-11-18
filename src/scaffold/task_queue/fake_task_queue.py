import dataclasses
import datetime
from collections.abc import Callable, Mapping

from .task import DEFAULT_QUEUE, HandlerProtocol


@dataclasses.dataclass(frozen=True)
class EnqueuedTask[T]:
    """A task as it was given to `GenericFakeTaskQueue.enqueue`, with everything it was enqueued with."""

    task: T
    visibility_timeout: int = 30
    run_at: datetime.datetime | None = None
    queue: str = DEFAULT_QUEUE
    max_attempts: int | None = None
    metadata: Mapping[str, str] | None = None


class GenericFakeTaskQueue[T]:
    def __init__(
        self,
    ) -> None:
        self.handler_factories: dict[type[T], Callable[[], HandlerProtocol[T]]] = {}
        self.queue: list[T] = []
        # The same tasks as `queue`, with the arguments they were enqueued with
        self.enqueued: list[EnqueuedTask[T]] = []

    def enqueue(
        self,
        task: T,
        visibility_timeout: int = 30,
        run_at: datetime.datetime | None = None,
        *,
        queue: str = DEFAULT_QUEUE,
        max_attempts: int | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        self.queue.append(task)
        self.enqueued.append(
            EnqueuedTask(
                task,
                visibility_timeout=visibility_timeout,
                run_at=run_at,
                queue=queue,
                max_attempts=max_attempts,
                metadata=metadata,
            ),
        )

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
