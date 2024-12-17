import asyncio
from collections.abc import Callable
from typing import Any


class AsyncioTaskManager:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()

    def run_task(
        self,
        func: Callable,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        task = asyncio.create_task(func(*args, **kwargs))
        task.add_done_callback(self._tasks.discard)
        self._tasks.add(task)
