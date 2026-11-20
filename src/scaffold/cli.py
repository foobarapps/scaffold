"""
Command line interface of an app, built on click. Needs the `cli` extra: `scaffold-framework[cli]`.

    cli = create_cli(bootstrap)

    @cli.command("send-reminders")
    @pass_container
    @run_async
    async def send_reminders(container: Container) -> None:
        await container[ReminderService].send()

    if __name__ == "__main__":
        cli()

The container is built once per invocation, by the `bootstrap` function the app passes
in, and only when a command actually runs, so `--help` needs no configuration. A test
passes its own container: `CliRunner().invoke(cli, ["send-reminders"], obj=container)`.
"""

import asyncio
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Concatenate

import click

from .di import Container


def run_async[**P, R](f: Callable[P, Coroutine[object, object, R]]) -> Callable[P, R]:
    """
    Let a click command be written as a coroutine.

    Click ignores what a callback returns, so a coroutine function used as a command
    without this would be called, never awaited, and silently do nothing.
    """

    @wraps(f)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return asyncio.run(f(*args, **kwargs))

    return wrapper


class _ContainerGroup(click.Group):
    def __init__(
        self,
        bootstrap: Callable[[], Container],
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(name=name, help=description)
        self.bootstrap = bootstrap


def create_cli(
    bootstrap: Callable[[], Container],
    *,
    name: str | None = None,
    description: str | None = None,
) -> click.Group:
    """Create the root command group of an app. Add commands and subgroups to it with its decorators."""
    return _ContainerGroup(bootstrap, name=name, description=description)


def pass_container[**P, R](f: Callable[Concatenate[Container, P], R]) -> Callable[P, R]:
    """Pass the app's container to a command as its first argument, building it on first use."""

    @wraps(f)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        root = click.get_current_context().find_root()
        if root.obj is None:
            if not isinstance(root.command, _ContainerGroup):
                message = "pass_container needs a command group made by create_cli, or a container passed as obj"
                raise RuntimeError(message)
            root.obj = root.command.bootstrap()
        if not isinstance(root.obj, Container):
            message = f"Expected a Container as the click context object, got {type(root.obj).__name__}"
            raise TypeError(message)
        return f(root.obj, *args, **kwargs)

    return wrapper
