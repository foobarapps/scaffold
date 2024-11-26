from typing import Any, ClassVar

from quart import (
    Request,
    Websocket,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
    websocket,
)
from quart.sessions import SessionMixin
from quart.typing import ResponseValue


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
        return request

    @property
    def session(self) -> SessionMixin:
        return session

    @property
    def websocket(self) -> Websocket:
        return websocket

    @staticmethod
    async def render_template(
        template_name_or_list: str,
        **context: Any,  # noqa: ANN401
    ) -> str:
        return await render_template(template_name_or_list, **context)
