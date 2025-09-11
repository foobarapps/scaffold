from collections.abc import Callable, Coroutine
from typing import Any

from quart import current_app


class QuartTaskManager:
    def run_task[**P](
        self,
        func: Callable[P, Coroutine[Any, Any, None]],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> None:
        current_app.add_background_task(func, *args, **kwargs)  # type: ignore[attr-defined]
