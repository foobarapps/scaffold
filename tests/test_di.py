import abc

import pytest

from scaffold.di import Container, MissingDependencyError


class Clock(abc.ABC):
    @abc.abstractmethod
    def now(self) -> int:
        raise NotImplementedError


class SystemClock(Clock):
    def now(self) -> int:
        return 1


class FrozenClock(Clock):
    def now(self) -> int:
        return 42


class Greeter:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock


class Service:
    def __init__(self, greeter: Greeter, *args: object, **kwargs: object) -> None:
        self.greeter = greeter


def test_class_is_built_from_its_type_hints() -> None:
    container = Container()
    container.add_singleton(Clock, SystemClock)

    service = container[Service]

    assert isinstance(service.greeter.clock, SystemClock)


def test_singleton_is_built_once_and_transient_every_time() -> None:
    container = Container()
    container.add_singleton(Clock, SystemClock)
    container.add_transient(Greeter, Greeter)

    assert container[Clock] is container[Clock]
    assert container[Greeter] is not container[Greeter]


def test_unregistered_dependency_is_named_in_the_error() -> None:
    container = Container()

    with pytest.raises(MissingDependencyError, match="Clock"):
        container[Greeter]


def test_override_replaces_a_singleton_that_was_already_built() -> None:
    container = Container()
    container.add_singleton(Clock, SystemClock)
    assert container[Clock].now() == 1

    container.override(Clock, lambda c: FrozenClock())

    assert container[Clock].now() == 42
    assert container[Clock] is container[Clock]


def test_override_keeps_a_transient_transient() -> None:
    container = Container()
    container.add_transient(Clock, SystemClock)

    container.override(Clock, FrozenClock)

    assert container[Clock].now() == 42
    assert container[Clock] is not container[Clock]


def test_override_of_an_unregistered_dependency_is_refused() -> None:
    container = Container()

    with pytest.raises(KeyError, match="Clock"):
        container.override(Clock, FrozenClock)


def test_validate_passes_when_everything_can_be_built() -> None:
    container = Container()
    container.add_singleton(Clock, SystemClock)
    container.add_transient(Service, Service)

    container.validate()


def test_validate_names_the_path_to_a_missing_dependency() -> None:
    container = Container()
    container.add_transient(Service, Service)

    with pytest.raises(MissingDependencyError, match="Service -> Greeter -> Clock"):
        container.validate()


@pytest.mark.asyncio
async def test_shutdown_functions_run_in_reverse_order() -> None:
    container = Container()
    calls: list[str] = []

    async def close_database(_: Container) -> None:
        calls.append("database")

    async def close_queue(_: Container) -> None:
        calls.append("queue")

    container.add_shutdown_function(close_database)
    container.add_shutdown_function(close_queue)

    await container.shutdown()

    assert calls == ["queue", "database"]
