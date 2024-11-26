import inspect
import os
import pkgutil
from importlib import import_module
from types import ModuleType
from typing import cast
from urllib.parse import urlparse, urlunparse


def find_subclasses[T: type](package: ModuleType, base_class: T) -> list[T]:
    subclasses: list[T] = []

    for _, subpath, ispkg in pkgutil.iter_modules(package.__path__):
        fullpath = package.__name__ + "." + subpath
        module = import_module(fullpath)
        if ispkg:
            subclasses += find_subclasses(module, base_class)
        for _, obj in inspect.getmembers(module):
            if (
                inspect.isclass(obj)
                and issubclass(obj, base_class)
                and obj != base_class
                and obj.__module__ == module.__name__
            ):
                subclasses.append(cast(T, obj))

    return subclasses


def get_env_flag(variable_name: str) -> bool:
    val = os.environ.get(variable_name)
    return bool(val and val.lower() not in {"0", "false", "no"})


def get_postgresql_url(for_sqlalchemy: bool = False) -> str:
    # Get the base URL from the environment variable
    base_url = os.environ.get("DATABASE_URL")

    if not base_url:
        message = "DATABASE_URL environment variable is not set"
        raise ValueError(message)

    # Parse the URL
    parsed = urlparse(base_url)

    # Assert that the scheme is postgresql
    assert parsed.scheme == "postgresql", "DATABASE_URL must use the postgresql:// scheme"

    if for_sqlalchemy:
        # Append the driver name for SQLAlchemy
        new_scheme = "postgresql+psycopg"
    else:
        # Keep the original postgresql scheme
        new_scheme = "postgresql"

    # Reconstruct the URL with the new scheme
    return urlunparse(
        (
            new_scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        ),
    )
