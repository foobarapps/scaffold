"""Response helpers with the types that Quart leaves out or keeps loose."""

from collections.abc import Mapping
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Protocol

import quart
from quart import Response, current_app
from quart.typing import ResponseReturnValue


class _FileHelpers(Protocol):
    async def send_file(
        self,
        filename_or_io: str | Path | BytesIO,
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
        directory: str | Path,
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


_file_helpers: _FileHelpers = quart


async def send_file(
    filename_or_io: str | Path | BytesIO,
    mimetype: str | None = None,
    as_attachment: bool = False,
    attachment_filename: str | None = None,
    add_etags: bool = True,
    cache_timeout: int | None = None,
    conditional: bool = False,
    last_modified: datetime | None = None,
) -> Response:
    """Quart's `send_file`, fully typed."""
    return await _file_helpers.send_file(
        filename_or_io,
        mimetype=mimetype,
        as_attachment=as_attachment,
        attachment_filename=attachment_filename,
        add_etags=add_etags,
        cache_timeout=cache_timeout,
        conditional=conditional,
        last_modified=last_modified,
    )


async def send_from_directory(
    directory: str | Path,
    file_name: str,
    *,
    mimetype: str | None = None,
    as_attachment: bool = False,
    attachment_filename: str | None = None,
    add_etags: bool = True,
    cache_timeout: int | None = None,
    conditional: bool = True,
    last_modified: datetime | None = None,
) -> Response:
    """Quart's `send_from_directory`, fully typed."""
    return await _file_helpers.send_from_directory(
        directory,
        file_name,
        mimetype=mimetype,
        as_attachment=as_attachment,
        attachment_filename=attachment_filename,
        add_etags=add_etags,
        cache_timeout=cache_timeout,
        conditional=conditional,
        last_modified=last_modified,
    )


async def make_response(*args: ResponseReturnValue | int | Mapping[str, str]) -> Response:
    """
    Quart's `make_response`, returning Quart's own response.

    Quart types the result as either its response or Werkzeug's, so that methods only the
    former has, such as the awaitable `make_conditional`, need a check first.
    """
    response = await quart.make_response(*args)
    if not isinstance(response, Response):
        message = f"Expected a Quart response, got {type(response)!r}"
        raise TypeError(message)
    return response


def app_config() -> Mapping[str, object]:
    """The config of the current app. Quart leaves its keys and values untyped."""
    return current_app.config
