from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, Concatenate

from werkzeug.exceptions import Unauthorized

from .base_controller import BaseController


def login_required[
    S: BaseController, **P,
    R,
](f: Callable[Concatenate[S, P], Awaitable[R]]) -> Callable[Concatenate[S, P], Awaitable[R]]:
    @wraps(f)
    async def decorated_function(self: S, *args: P.args, **kwargs: P.kwargs) -> R:
        user_id = self.session.get("user_id")
        if user_id is None:
            raise Unauthorized
        return await f(self, *args, **kwargs)

    return decorated_function


def route[F: Callable](rule: str, **options: Any) -> Callable[[F], F]:  # noqa: ANN401
    def decorator(f: F) -> F:
        setattr(f, "route", (rule, options))
        return f

    return decorator


def before_serving[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    setattr(f, "is_before_serving_callback", True)
    return f


def before_request[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    setattr(f, "is_before_request_callback", True)
    return f


def after_request[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    setattr(f, "is_after_request_callback", True)
    return f


def template_context_processor[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    setattr(f, "is_template_context_processor", True)
    return f


def error_handler[F: Callable, E: type[Exception]](exception: E) -> Callable[[F], F]:
    def decorator(f: F) -> F:
        setattr(f, "is_error_handler", True)
        setattr(f, "error_handler_exception", exception)
        return f

    return decorator


def controller(
    name: str,
    url_prefix: str | None = None,
    subdomain: str | None = None,
) -> Callable[[type[BaseController]], type[BaseController]]:
    def decorator(controller_class: type[BaseController]) -> type[BaseController]:
        controller_class.name = name
        controller_class.url_prefix = url_prefix
        controller_class.subdomain = subdomain
        return controller_class

    return decorator
