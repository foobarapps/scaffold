from collections.abc import Callable
from typing import Any

from quart import current_app


class QuartTaskManager:
    def run_task(
        self,
        func: Callable,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        current_app.add_background_task(func, *args, **kwargs)
