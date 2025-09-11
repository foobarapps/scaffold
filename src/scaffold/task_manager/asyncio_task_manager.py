import asyncio
from collections.abc import Callable, Coroutine
from typing import Any


class AsyncioTaskManager:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def run_task[**P](
        self,
        func: Callable[P, Coroutine[Any, Any, None]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        task = asyncio.create_task(func(*args, **kwargs))
        task.add_done_callback(self._tasks.discard)
        self._tasks.add(task)
