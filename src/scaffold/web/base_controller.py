from typing import Any, ClassVar, Protocol, cast

from quart import (
    # Request,
    Websocket,
    flash,
    redirect,
    render_template,
    request,
    send_file,  # type: ignore
    session,
    url_for,
    websocket,
)
from quart.sessions import SessionMixin
from quart.typing import ResponseValue
from werkzeug.datastructures import Headers, ImmutableMultiDict, MIMEAccept, MultiDict

# TODO define its own protocols for the return values so that it's not leaking Quart types


class Request(Protocol):
    @property
    def method(self) -> str: ...

    @property
    async def form(self) -> MultiDict[str, str]: ...

    @property
    def args(self) -> MultiDict[str, str]: ...

    @property
    def view_args(self) -> dict[str, Any]: ...

    @property
    def cookies(self) -> ImmutableMultiDict[str, str]: ...

    @property
    def headers(self) -> Headers: ...

    @property
    def accept_mimetypes(self) -> MIMEAccept: ...


class BaseController:
    name: ClassVar[str]
    url_prefix: ClassVar[str | None] = None
    subdomain: ClassVar[str | None] = None

    @staticmethod
    def redirect(location: str, code: int = 302) -> ResponseValue:
        return redirect(location, code)

    @staticmethod
    def url_for(endpoint: str, **values: Any) -> str:  # noqa: ANN401
        return url_for(endpoint, **values)

    async def flash(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        return await flash(*args, **kwargs)

    @property
    def request(self) -> Request:
        return cast(Request, request)

    @property
    def session(self) -> SessionMixin:
        return session

    @property
    def websocket(self) -> Websocket:
        return websocket

    async def send_file(self, filename: str, mimetype: str) -> ResponseValue:
        return await send_file(filename, mimetype)

    @staticmethod
    async def render_template(
        template_name_or_list: str,
        **context: Any,  # noqa: ANN401
    ) -> str:
        return await render_template(template_name_or_list, **context)
