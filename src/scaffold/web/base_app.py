import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import wraps
from graphlib import CycleError, TopologicalSorter
from typing import Any, Concatenate, Protocol, cast

from flask.sansio.app import App
from flask.templating import Environment
from quart import Blueprint, Quart, g, request
from quart.typing import BeforeServingCallable, TestClientProtocol

from .base_controller import BaseController


class Extension[A: App](Protocol):
    def init_app(self, app: A) -> None: ...


class BaseWebApp:
    def __init__(
        self,
        root_package_name: str,
        secret_key: bytes | None = None,
        server_name: str | None = None,
        propagate_exceptions: bool = False,
    ) -> None:
        self.__app = Quart(root_package_name)

        # TODO expose all config vars (https://flask.palletsprojects.com/en/stable/config/) through the constructor
        self.__app.config.update(
            SECRET_KEY=secret_key,
            SERVER_NAME=server_name,
            PROPAGATE_EXCEPTIONS=propagate_exceptions,
        )

        # FIXME hotfix until https://github.com/pallets/quart/issues/383 gets fixed
        if propagate_exceptions:
            self.__app.testing = True

        self.__controller_factories: dict[
            type[BaseController],
            Callable[[], BaseController],
        ] = {}
        self.__endpoint_to_controller_class: dict[str, type[BaseController]] = {}

        self.__app.before_request(self.__create_controller_instance)

        self.__register_app_callbacks()

        self.init()

    def init(self) -> None:
        pass

    def register_extension(self, extension: Extension) -> None:
        extension.init_app(self.__app)

    @property
    def config(self) -> Mapping:
        return self.__app.config

    @property
    def jinja_env(self) -> Environment:
        return self.__app.jinja_env

    def test_client(
        self,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> TestClientProtocol:
        return self.__app.test_client(*args, **kwargs)

    def before_serving(self, before_serving_callable: BeforeServingCallable) -> None:
        self.__app.before_serving(before_serving_callable)

    def register_controllers(
        self,
        controller_factories: dict[type[BaseController], Callable[[], BaseController]],
    ) -> None:
        self.__controller_factories = controller_factories

        controller_parents = self.__get_controllers_dependency_graph()

        sorted_controller_classes = self.__get_topologically_sorted_controller_classes(
            controller_parents,
        )

        blueprints = {}

        for controller_class in sorted_controller_classes:
            blueprint = self.__create_blueprint(controller_class)
            blueprints[controller_class] = blueprint

        for controller_class in sorted_controller_classes:
            blueprint = blueprints[controller_class]

            if controller_parents[controller_class]:
                for parent_controller_class in controller_parents[controller_class]:
                    parent_blueprint = blueprints[parent_controller_class]
                    parent_blueprint.register_blueprint(blueprint)

            else:
                self.__app.register_blueprint(blueprint)

        blueprint_to_full_names = defaultdict(list)

        for full_name, bp in self.__app.blueprints.items():
            blueprint_to_full_names[bp].append(full_name)

        for controller_class in sorted_controller_classes:
            blueprint = blueprints[controller_class]

            for full_name in blueprint_to_full_names[blueprint]:
                self.__endpoint_to_controller_class[full_name] = controller_class

    def __create_controller_instance(self) -> None:
        if request.endpoint is not None:
            blueprint_full_name = request.endpoint.rsplit(".", maxsplit=1)[0]
            if blueprint_full_name in self.__endpoint_to_controller_class:
                controller_class = self.__endpoint_to_controller_class[blueprint_full_name]
                g.controller = self.__controller_factories[controller_class]()

    def __create_blueprint(
        self,
        controller_class: type[BaseController],
    ) -> Blueprint:
        blueprint = Blueprint(
            controller_class.name,
            __name__,
            url_prefix=controller_class.url_prefix,
            subdomain=controller_class.subdomain,
        )

        self.__register_controller_callbacks(blueprint, controller_class)

        self.__register_view_functions(blueprint, controller_class)

        return blueprint

    @staticmethod
    def __bind_request_controller[
        S, **P,
        R,
    ](
        func: Callable[Concatenate[S, P], R] | Callable[Concatenate[S, P], Awaitable[R]],
    ) -> (
        Callable[P, R] | Callable[P, Awaitable[R]]
    ):
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return await func(g.controller, *args, **kwargs)

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return cast(R, func(g.controller, *args, **kwargs))

        return sync_wrapper

    def __bind_websocket_controller[
        **P,
        R,
    ](
        self,
        func: Callable[Concatenate[BaseController, P], Awaitable[R]],
        controller_class: type[BaseController],
    ) -> Callable[
        P,
        Awaitable[R],
    ]:
        @wraps(func)
        async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            controller = self.__controller_factories[controller_class]()
            return await func(controller, *args, **kwargs)

        return async_wrapper

    def __register_view_functions(
        self,
        blueprint: Blueprint,
        controller_class: type[BaseController],
    ) -> None:
        for view_function_name, view_function in inspect.getmembers(
            controller_class,
            predicate=inspect.isfunction,
        ):
            if hasattr(view_function, "route"):
                rule, options = view_function.route

                if "websocket" in options and options["websocket"]:
                    wrapped_view_function = self.__bind_websocket_controller(
                        view_function,
                        controller_class,
                    )
                else:
                    wrapped_view_function = self.__bind_request_controller(
                        view_function,
                    )

                blueprint.add_url_rule(
                    rule=rule,
                    endpoint=view_function_name,
                    view_func=wrapped_view_function,
                    **options,
                )

    def __register_app_callbacks(self) -> None:
        for _, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, "is_before_serving_callback"):
                self.__app.before_serving(method)

    def __register_controller_callbacks(
        self,
        blueprint: Blueprint,
        controller_class: type[BaseController],
    ) -> None:
        for _, callback in inspect.getmembers(
            controller_class,
            predicate=inspect.isfunction,
        ):
            if hasattr(callback, "is_before_request_callback"):
                blueprint.before_request(self.__bind_request_controller(callback))

            if hasattr(callback, "is_after_request_callback"):
                blueprint.after_request(self.__bind_request_controller(callback))

            if hasattr(callback, "is_template_context_processor"):
                blueprint.context_processor(self.__bind_request_controller(callback))

            if hasattr(callback, "is_error_handler") and hasattr(
                callback,
                "error_handler_exception",
            ):
                blueprint.register_error_handler(
                    callback.error_handler_exception,
                    self.__bind_request_controller(callback),
                )

    @staticmethod
    def __get_topologically_sorted_controller_classes(
        controllers_dependency_graph: dict[
            type[BaseController],
            list[type[BaseController]],
        ],
    ) -> Sequence[type[BaseController]]:
        """
        Gets controllers in their topological order.
        """
        ts = TopologicalSorter(controllers_dependency_graph)

        try:
            return list(reversed(list(ts.static_order())))

        except CycleError as e:
            msg = "Cycle detected in controller inheritance hierarchy."
            raise ValueError(msg) from e

    def __get_controllers_dependency_graph(
        self,
    ) -> dict[type[BaseController], list[type[BaseController]]]:
        controllers_dependency_graph = {}
        for controller_class in self.__controller_factories.keys():
            parents = [
                base
                for base in controller_class.__bases__
                if issubclass(base, BaseController) and base != BaseController
            ]
            controllers_dependency_graph[controller_class] = parents
        return controllers_dependency_graph

    async def __call__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        return await self.__app(*args, **kwargs)
