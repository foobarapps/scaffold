"""
An extremely simple DI container (in <100 lines of code) with auto-wiring based on type hints.
"""

import inspect
from collections.abc import Awaitable, Callable
from typing import cast

# Since `type` does not accept abstract classes, we have to use `Callable` as well (although it's not ideal), see https://github.com/python/mypy/issues/4717
type Dependency[T] = type[T] | Callable[..., T]
type Provider[T] = type[T] | Callable[["Container"], T]


class MissingDependencyError(RuntimeError):
    pass


def _describe(dependency: object) -> str:
    return getattr(dependency, "__qualname__", None) or repr(dependency)


class Container:
    def __init__(self) -> None:
        self.providers: dict[Dependency[object], Provider[object]] = {}
        self.singletons: dict[Dependency[object], object] = {}
        self.init_functions: list[Callable[[Container], Awaitable[None]]] = []
        self.shutdown_functions: list[Callable[[Container], Awaitable[None]]] = []

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

    def add_shutdown_function(
        self,
        shutdown_function: Callable[["Container"], Awaitable[None]],
    ) -> None:
        self.shutdown_functions.append(shutdown_function)

    async def init(self) -> None:
        for init_function in self.init_functions:
            await init_function(self)

    async def shutdown(self) -> None:
        """Run the shutdown functions, last registered first, the reverse of how things were set up."""
        for shutdown_function in reversed(self.shutdown_functions):
            await shutdown_function(self)

    def override[T](self, cls: Dependency[T], provider: Provider[T]) -> None:
        """
        Replace the provider of a registered dependency, keeping its lifetime.

        Meant for tests that swap a real adapter for a fake. A singleton that was already
        created is dropped, so the next resolve builds it from the new provider. Anything
        that already holds the old instance keeps it, so override before resolving.
        """
        if cls not in self.providers:
            message = f"Cannot override {_describe(cls)}, it is not registered"
            raise KeyError(message)

        self.providers[cls] = provider
        if cls in self.singletons:
            self.singletons[cls] = None

    def validate(self) -> None:
        """
        Check that every class the container builds itself can get its constructor arguments.

        Call it at the end of bootstrap, so that a missing registration fails at startup
        instead of on the first request that needs it. Providers that are functions are
        not called and so not checked.
        """
        for cls, provider in self.providers.items():
            if isinstance(provider, type):
                self._validate_class(provider, (_describe(cls),))

    def _validate_class(self, cls: type[object], path: tuple[str, ...]) -> None:
        for name, param in inspect.signature(cls.__init__).parameters.items():
            if name == "self" or param.annotation == inspect.Parameter.empty:
                continue
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue

            dependency = param.annotation
            dependency_path = (*path, _describe(dependency))
            if dependency in self.providers:
                continue
            if not isinstance(dependency, type) or inspect.isabstract(dependency):
                message = f"Nothing is registered for {' -> '.join(dependency_path)}"
                raise MissingDependencyError(message)
            if _describe(dependency) in path:
                message = f"Circular dependency: {' -> '.join(dependency_path)}"
                raise MissingDependencyError(message)
            self._validate_class(dependency, dependency_path)

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

        if isinstance(cls, type) and not inspect.isabstract(cls):
            return self._instantiate(cast(type[T], cls))

        message = f"Nothing is registered for {_describe(cls)}"
        raise MissingDependencyError(message)

    def _instantiate[T](self, cls: type[T]) -> T:
        constructor_signature = inspect.signature(cls.__init__)
        dependencies = {}

        for name, param in constructor_signature.parameters.items():
            # TODO is there a better way to ignore the "self" param?
            if name == "self" or param.annotation == inspect.Parameter.empty:
                continue  # Skip parameters that are not type-annotated or are 'self'
            if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            dependencies[name] = self[param.annotation]

        return cls(**dependencies)

    def __getitem__[T](self, cls: Dependency[T]) -> T:
        return self.resolve(cls)

    def get_factory[T](self, cls: Dependency[T]) -> Callable[[], T]:
        def factory() -> T:
            return self[cls]

        return factory
