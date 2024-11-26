from collections.abc import Callable
from typing import Any

from .base_controller import BaseController


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


def errorhandler[F: Callable, E: type[Exception]](exception: E) -> Callable[[F], F]:
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
