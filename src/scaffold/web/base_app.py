import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from functools import wraps
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any, Concatenate, Protocol, cast, runtime_checkable

from flask.sansio.blueprints import Blueprint as SansioBlueprint  # type: ignore
from flask.templating import Environment
from quart import (
    Blueprint,
    Quart,
    Request,
    ResponseReturnValue,
    Websocket,
    g,
    request,
    send_from_directory,  # type: ignore
    websocket,
)
from quart.ctx import AppContext
from quart.typing import (
    BeforeRequestCallable,
    BeforeServingCallable,
    TemplateContextProcessorCallable,
    TestClientProtocol,
)

from .assets import Assets
from .base_controller import BaseController


@runtime_checkable
class RouteFunction(Protocol):
    route: tuple[str, dict[str, Any]]


@runtime_checkable
class ErrorHandlerCallbackFunction(Protocol):
    is_error_handler: bool
    error_handler_exception: type[Exception]


@runtime_checkable
class BeforeServingCallbackFunction(Protocol):
    is_before_serving_callback: bool


@runtime_checkable
class BeforeRequestCallbackFunction(Protocol):
    is_before_request_callback: bool


@runtime_checkable
class AfterRequestCallbackFunction(Protocol):
    is_after_request_callback: bool


@runtime_checkable
class BeforeWebSocketCallbackFunction(Protocol):
    is_before_websocket_callback: bool


@runtime_checkable
class AfterWebSocketCallbackFunction(Protocol):
    is_after_websocket_callback: bool


@runtime_checkable
class TemplateContextProcessorFunction(Protocol):
    is_template_context_processor: bool


class Extension(Protocol):
    def init_app(self, app: "BaseWebApp") -> None: ...


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
        self.__app.config.update(  # type: ignore
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

        # Assets will be initialized in setup
        self.__assets: Assets | None = None

        self.__app.before_request(self.__create_request_controller_instance)
        self.__app.before_websocket(self.__create_websocket_controller_instance)

        self.__register_app_callbacks()
        self.__setup_assets_serving()

        @self.__app.route("/health")
        def _() -> ResponseReturnValue:
            return "", 200

        self.init()

    def init(self) -> None:
        pass

    def register_extension(self, extension: Extension) -> None:
        # extension.init_app(self.__app)
        extension.init_app(self)

    @property
    def config(self) -> Mapping[str, Any]:
        return self.__app.config

    @property
    def debug(self) -> bool:
        return self.__app.debug

    @debug.setter
    def debug(self, value: bool) -> None:
        self.__app.debug = value

    @property
    def jinja_env(self) -> Environment:
        return self.__app.jinja_env

    def context_processor(
        self,
        context_processor_callable: TemplateContextProcessorCallable,
    ) -> None:
        self.__app.context_processor(context_processor_callable)

    @property
    def static_folder(self) -> str | None:
        return self.__app.static_folder

    def app_context(self) -> AppContext:
        return self.__app.app_context()

    def test_client(
        self,
        *args: Any,  # noqa: ANN401
        **kwargs: Any,  # noqa: ANN401
    ) -> TestClientProtocol:
        return self.__app.test_client(*args, **kwargs)

    def before_serving(self, before_serving_callable: BeforeServingCallable) -> None:
        self.__app.before_serving(before_serving_callable)

    def before_request(self, before_request_callable: BeforeRequestCallable) -> None:
        self.__app.before_request(before_request_callable)

    def register_controllers(
        self,
        controller_factories: dict[type[BaseController], Callable[[], BaseController]],
    ) -> None:
        self.__controller_factories = controller_factories

        controller_parents = self.__get_controllers_dependency_graph()

        sorted_controller_classes = self.__get_topologically_sorted_controller_classes(
            controller_parents,
        )

        blueprints: dict[type[BaseController], Blueprint] = {}

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

        blueprint_to_full_names: dict[SansioBlueprint, list[str]] = defaultdict(list)

        for full_name, bp in self.__app.blueprints.items():
            blueprint_to_full_names[bp].append(full_name)

        for controller_class in sorted_controller_classes:
            blueprint = blueprints[controller_class]

            for full_name in blueprint_to_full_names[blueprint]:
                self.__endpoint_to_controller_class[full_name] = controller_class

    def __create_request_controller_instance(self) -> None:
        self.__create_controller_instance(request)

    def __create_websocket_controller_instance(self) -> None:
        self.__create_controller_instance(websocket)

    def __create_controller_instance(
        self,
        request_or_websocket: Request | Websocket,
    ) -> None:
        if request_or_websocket.endpoint is not None:
            blueprint_full_name = request_or_websocket.endpoint.rsplit(".", maxsplit=1)[
                0
            ]
            if blueprint_full_name in self.__endpoint_to_controller_class:
                controller_class = self.__endpoint_to_controller_class[
                    blueprint_full_name
                ]
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
    def __bind_controller[
        S,
        **P,
        R,
    ](
        func: Callable[Concatenate[S, P], R]
        | Callable[Concatenate[S, P], Awaitable[R]],
    ) -> Callable[P, R] | Callable[P, Awaitable[R]]:
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                return await func(g.controller, *args, **kwargs)

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return cast(R, func(g.controller, *args, **kwargs))

        return sync_wrapper

    def __register_view_functions(
        self,
        blueprint: Blueprint,
        controller_class: type[BaseController],
    ) -> None:
        for view_function_name, view_function in inspect.getmembers(
            controller_class,
            predicate=lambda member: inspect.isfunction(member)
            and isinstance(member, RouteFunction),  # pyright: ignore[reportUnnecessaryIsInstance]
        ):
            rule, options = view_function.route

            wrapped_view_function = self.__bind_controller(
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
            if isinstance(method, BeforeServingCallbackFunction):  # pyright: ignore[reportUnnecessaryIsInstance]
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
            if isinstance(callback, BeforeRequestCallbackFunction):  # pyright: ignore[reportUnnecessaryIsInstance]
                blueprint.before_request(self.__bind_controller(callback))

            if isinstance(callback, AfterRequestCallbackFunction):  # pyright: ignore[reportUnnecessaryIsInstance]
                blueprint.after_request(self.__bind_controller(callback))

            if isinstance(callback, BeforeWebSocketCallbackFunction):  # pyright: ignore[reportUnnecessaryIsInstance]
                blueprint.before_websocket(self.__bind_controller(callback))

            if isinstance(callback, AfterWebSocketCallbackFunction):  # pyright: ignore[reportUnnecessaryIsInstance]
                blueprint.after_websocket(self.__bind_controller(callback))

            if isinstance(callback, TemplateContextProcessorFunction):  # pyright: ignore[reportUnnecessaryIsInstance]
                blueprint.context_processor(self.__bind_controller(callback))

            if isinstance(callback, ErrorHandlerCallbackFunction):  # pyright: ignore[reportUnnecessaryIsInstance]
                blueprint.register_error_handler(
                    callback.error_handler_exception,
                    self.__bind_controller(callback),
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
        controllers_dependency_graph: dict[
            type[BaseController],
            list[type[BaseController]],
        ] = {}
        for controller_class in self.__controller_factories.keys():
            parents = [
                base
                for base in controller_class.__bases__
                if issubclass(base, BaseController) and base != BaseController
            ]
            controllers_dependency_graph[controller_class] = parents
        return controllers_dependency_graph

    def __setup_assets_serving(self) -> None:
        """Set up asset serving functionality."""
        self.__app.before_serving(self.__setup_assets)
        self.__app.context_processor(self.__template_context)

        # Set up the blueprint for serving static files under '/assets'
        blueprint = Blueprint("assets", __name__)
        blueprint.add_url_rule(
            "/assets/<path:path>",
            "asset",
            self.__serve_assets,
        )
        self.__app.register_blueprint(blueprint)

    async def __setup_assets(self) -> None:
        """Initialize assets when the app starts serving."""
        if self.__app.static_folder is None:
            error_message = "A static folder has to be set"
            raise RuntimeError(error_message)

        # Initialize assets container
        static_dir = Path(self.__app.static_folder)
        self.__assets = Assets(static_dir)
        await self.__assets.update_file_maps()

    async def __serve_assets(self, path: str) -> ResponseReturnValue:
        """Serve assets with hashed filenames."""
        if self.__app.static_folder is None:
            error_message = "A static folder has to be set"
            raise RuntimeError(error_message)

        if self.__assets is None:
            error_message = "Assets not initialized"
            raise RuntimeError(error_message)

        original_filename = self.__assets.get_original_filename(path)
        return await send_from_directory(self.__app.static_folder, original_filename)

    def __template_context(self) -> dict[str, Any]:
        """Inject assets into template context."""
        return {"assets": self.__assets}

    async def __call__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        return await self.__app(*args, **kwargs)
