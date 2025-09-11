"""
An extremely simple DI container (in <100 lines of code) with auto-wiring based on type hints.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import cast

# Since `type` does not accept abstract classes, we have to use `Callable` as well (although it's not ideal), see https://github.com/python/mypy/issues/4717
type Dependency[T] = type[T] | Callable[..., T]
type Provider[T] = type[T] | Callable[["Container"], T]


class Container:
    def __init__(self) -> None:
        self.providers: dict[Dependency[object], Provider[object]] = {}
        self.singletons: dict[Dependency[object], object] = {}
        self.init_functions: list[Callable[[Container], Awaitable[None]]] = []

    def add_singleton[T](self, cls: Dependency[T], provider: Provider[T]) -> None:
        self.providers[cls] = provider
        self.singletons[cls] = None

    def add_transient[T](self, cls: Dependency[T], provider: Provider[T]) -> None:
        self.providers[cls] = provider

    def add_init_function(
        self,
        init_function: Callable[["Container"], Awaitable[None]],
    ) -> None:
        self.init_functions.append(init_function)

    async def init(self) -> None:
        for init_function in self.init_functions:
            await init_function(self)

    def resolve[T](self, cls: Dependency[T]) -> T:
        # TODO add runtime type checking so that it fails during bootstrap

        instance: T

        if cls in self.providers:
            provider = self.providers[cls]

            if cls in self.singletons and self.singletons[cls] is not None:
                return cast(T, self.singletons[cls])

            if isinstance(provider, type):
                instance = self._instantiate(cast(type[T], provider))

            else:
                instance = cast(T, provider(self))

            if cls in self.singletons:
                self.singletons[cls] = instance

            return instance

        if isinstance(cls, type):
            return self._instantiate(cast(type[T], cls))

        raise RuntimeError

    def _instantiate[T](self, cls: type[T]) -> T:
        constructor_signature = inspect.signature(cls.__init__)
        dependencies = {}

        for name, param in constructor_signature.parameters.items():
            # TODO is there a better way to ignore the "self" param?
            if name == "self" or param.annotation == inspect.Parameter.empty:
                continue  # Skip parameters that are not type-annotated or are 'self'
            dependencies[name] = self[param.annotation]

        return cls(**dependencies)

    def __getitem__[T](self, cls: Dependency[T]) -> T:
        return self.resolve(cls)

    def get_factory[T](self, cls: Dependency[T]) -> Callable[[], T]:
        def factory() -> T:
            return self[cls]

        return factory
