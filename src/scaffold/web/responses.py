"""Response helpers with the types that Quart leaves out or keeps loose."""

import os
from collections.abc import Mapping
from datetime import datetime
from io import BytesIO
from typing import Protocol

import quart
from quart import Response, current_app
from quart.typing import ResponseReturnValue

# Quart's own FilePath leaves the type of the path of a PathLike out, which a strict type
# checker refuses. This is the same set of types, spelled out.
type FilePath = bytes | str | os.PathLike[str] | os.PathLike[bytes]


class _FileHelpers(Protocol):
    async def send_file(
        self,
        filename_or_io: FilePath | BytesIO,
        mimetype: str | None = None,
        as_attachment: bool = False,
        attachment_filename: str | None = None,
        add_etags: bool = True,
        cache_timeout: int | None = None,
        conditional: bool = False,
        last_modified: datetime | None = None,
    ) -> Response: ...

    async def send_from_directory(
        self,
        directory: FilePath,
        file_name: str,
        *,
        mimetype: str | None = None,
        as_attachment: bool = False,
        attachment_filename: str | None = None,
        add_etags: bool = True,
        cache_timeout: int | None = None,
        conditional: bool = True,
        last_modified: datetime | None = None,
    ) -> Response: ...


def _typed(helpers: _FileHelpers) -> _FileHelpers:
    # Passing the module through a function keeps a type checker from narrowing the
    # protocol back to the module, whose own signatures are the partly untyped ones
    return helpers


_file_helpers = _typed(quart)

# Quart's `send_file` and `send_from_directory`, with the signatures above
send_file = _file_helpers.send_file
send_from_directory = _file_helpers.send_from_directory


async def make_response(*args: ResponseReturnValue | int | Mapping[str, str]) -> Response:
    """
    Quart's `make_response`, returning Quart's own response.

    Quart types the result as either its response or Werkzeug's, so that methods only the
    former has, such as the awaitable `make_conditional`, need a check first.

    Quart hands a Werkzeug response back as it is. This raises a `TypeError` for one instead:
    use `quart.make_response` for a response that has to stay a Werkzeug one.
    """
    response = await quart.make_response(*args)
    if not isinstance(response, Response):
        message = f"Expected a Quart response, got {type(response)!r}"
        raise TypeError(message)
    return response


def app_config() -> Mapping[str, object]:
    """The config of the current app. Quart leaves its keys and values untyped."""
    return current_app.config
