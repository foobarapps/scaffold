import asyncio

import click
from click.testing import CliRunner

from scaffold.cli import create_cli, find_unawaited_commands, pass_container, run_async
from scaffold.di import Container


class Greeter:
    def greet(self) -> str:
        return "hello"


def make_cli(bootstrap_calls: list[int] | None = None) -> click.Group:
    def bootstrap() -> Container:
        if bootstrap_calls is not None:
            bootstrap_calls.append(1)
        container = Container()
        container.add_singleton(Greeter, Greeter)
        return container

    cli = create_cli(bootstrap)

    @cli.command("greet")
    @click.option("--name", default="world")
    @pass_container
    @run_async
    async def greet(container: Container, name: str) -> None:
        await asyncio.sleep(0)
        click.echo(f"{container[Greeter].greet()} {name}")

    @cli.group("users")
    def users() -> None:
        pass

    @users.command("count")
    @pass_container
    def count(container: Container) -> None:
        click.echo(type(container[Greeter]).__name__)

    return cli


def test_command_gets_the_container_and_runs_as_a_coroutine() -> None:
    result = CliRunner().invoke(make_cli(), ["greet", "--name", "there"])

    assert result.exit_code == 0, result.output
    assert result.output == "hello there\n"


def test_command_in_a_subgroup_gets_the_container() -> None:
    result = CliRunner().invoke(make_cli(), ["users", "count"])

    assert result.output == "Greeter\n"


def test_help_does_not_bootstrap() -> None:
    bootstrap_calls: list[int] = []
    cli = make_cli(bootstrap_calls)

    for args in (["--help"], ["greet", "--help"], ["users", "--help"], []):
        CliRunner().invoke(cli, args)

    assert bootstrap_calls == []


def test_container_is_built_once_per_invocation() -> None:
    bootstrap_calls: list[int] = []

    CliRunner().invoke(make_cli(bootstrap_calls), ["greet"])

    assert bootstrap_calls == [1]


def test_a_test_can_pass_its_own_container() -> None:
    class FakeGreeter(Greeter):
        def greet(self) -> str:
            return "ahoy"

    container = Container()
    container.add_singleton(Greeter, FakeGreeter)
    bootstrap_calls: list[int] = []

    result = CliRunner().invoke(make_cli(bootstrap_calls), ["greet"], obj=container)

    assert result.output == "ahoy world\n"
    assert bootstrap_calls == []


def test_run_async_returns_the_result_of_the_coroutine() -> None:
    @run_async
    async def double(value: int) -> int:
        await asyncio.sleep(0)
        return value * 2

    assert double(21) == 42


def test_command_missing_run_async_is_found() -> None:
    cli = make_cli()

    @cli.command("forgotten")
    async def forgotten() -> None:
        pass

    assert find_unawaited_commands(cli) == ["forgotten"]


def test_pass_container_outside_of_create_cli_is_refused() -> None:
    @click.command()
    @pass_container
    def lonely(container: Container) -> None:
        pass

    result = CliRunner().invoke(lonely)

    assert isinstance(result.exception, RuntimeError)
