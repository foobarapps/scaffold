"""
The data of the current request, with the types that Quart leaves out.

Quart types the form, the files and the values of a request as multi-dicts of unknown
keys and values, which a strict type checker refuses. These say what they hold.
"""

from typing import Protocol, cast

from quart import request
from quart.datastructures import FileStorage
from werkzeug.datastructures import CombinedMultiDict, MultiDict


class _RequestData(Protocol):
    @property
    async def form(self) -> MultiDict[str, str]: ...

    @property
    async def files(self) -> MultiDict[str, FileStorage]: ...

    @property
    async def values(self) -> CombinedMultiDict[str, str]: ...


# Returning the request, rather than assigning it to a variable, keeps a type checker from
# narrowing the protocol back to Quart's own type
def _request_data() -> _RequestData:
    return request


async def request_form() -> MultiDict[str, str]:
    """The form fields of the current request."""
    return await _request_data().form


async def request_files() -> MultiDict[str, FileStorage]:
    """The uploaded files of the current request."""
    return await _request_data().files


async def request_values() -> CombinedMultiDict[str, str]:
    """The query parameters and the form fields of the current request."""
    return await _request_data().values


async def request_form_and_files() -> CombinedMultiDict[str, str | FileStorage]:
    """The uploaded files and the form fields together, e.g. as the data of a form with a file field."""
    # MultiDict is invariant in the type of its values, so the two are widened to combine them
    sources = cast(
        "list[MultiDict[str, str | FileStorage]]",
        [await request_files(), await request_form()],
    )
    return CombinedMultiDict(sources)
