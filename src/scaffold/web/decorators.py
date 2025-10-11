from collections.abc import AsyncGenerator, Awaitable, Callable
from functools import wraps
from typing import Any, Concatenate

from quart.globals import _cv_app, _cv_request  # type: ignore[reportPrivateUsage]
from werkzeug.exceptions import Unauthorized

from .base_controller import BaseController


def login_required[
    S: BaseController,
    **P,
    R,
](
    f: Callable[Concatenate[S, P], Awaitable[R]],
) -> Callable[Concatenate[S, P], Awaitable[R]]:
    @wraps(f)
    async def decorated_function(self: S, *args: P.args, **kwargs: P.kwargs) -> R:
        user_id = self.session.get("user_id")
        if user_id is None:
            raise Unauthorized
        return await f(self, *args, **kwargs)

    return decorated_function


def route[**P, R](
    rule: str,
    **options: Any,  # noqa: ANN401
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(f: Callable[P, R]) -> Callable[P, R]:
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


def before_websocket[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    setattr(f, "is_before_websocket_callback", True)
    return f


def after_websocket[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    setattr(f, "is_after_websocket_callback", True)
    return f


def template_context_processor[**P, R](f: Callable[P, R]) -> Callable[P, R]:
    setattr(f, "is_template_context_processor", True)
    return f


def error_handler[**P, R](
    exception: type[Exception],
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorator(f: Callable[P, R]) -> Callable[P, R]:
        setattr(f, "is_error_handler", True)
        setattr(f, "error_handler_exception", exception)
        return f

    return decorator


def controller[C: BaseController](
    name: str,
    url_prefix: str | None = None,
    subdomain: str | None = None,
) -> Callable[[type[C]], type[C]]:
    def decorator(controller_class: type[C]) -> type[C]:
        controller_class.name = name
        controller_class.url_prefix = url_prefix
        controller_class.subdomain = subdomain
        return controller_class

    return decorator


def stream_with_context[**P, Y, S](
    func: Callable[P, AsyncGenerator[Y, S]],
) -> Callable[P, AsyncGenerator[Y, S]]:
    app_context = _cv_app.get().copy()
    request_context = _cv_request.get().copy()

    @wraps(wrapped=func)
    async def generator(*args: P.args, **kwargs: P.kwargs) -> AsyncGenerator[Y, S]:
        async with app_context, request_context:
            async for item in func(*args, **kwargs):
                yield item

    return generator
