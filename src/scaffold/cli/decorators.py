from collections.abc import Callable
from typing import Any


class Command:
    def __init__(
        self,
        func: Callable,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        self.func = func
        self.name = name if name is not None else func.__name__
        self.help = description
        self.arguments: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return self.func(*args, **kwargs)


def command(
    name: str | None = None,
    description: str | None = None,
) -> Callable[[Callable | Command], Command]:
    def decorator(f: Callable | Command) -> Command:
        if isinstance(f, Command):
            if name:
                f.name = name
            if description:
                f.help = description
            return f

        return Command(f, name, description)

    return decorator


def argument(
    *args: Any,  # noqa: ANN401
    **kwargs: Any,  # noqa: ANN401
) -> Callable[[Callable | Command], Command]:
    def decorator(f: Callable | Command) -> Command:
        if not isinstance(f, Command):
            f = Command(f)

        f.arguments.append((args, kwargs))
        return f

    return decorator
