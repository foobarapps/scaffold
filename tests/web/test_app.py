import asyncio
import dataclasses
from typing import Any

import pytest
from quart.typing import ResponseReturnValue

from scaffold.web.base_app import BaseWebApp
from scaffold.web.base_controller import BaseController
from scaffold.web.decorators import controller, error_handler, route


@pytest.mark.asyncio
async def test_app() -> None:
    @controller(name="test")
    class TestController(BaseController):
        @route("/")
        def index(self) -> ResponseReturnValue:
            return "Hello, World!"

    app = BaseWebApp("__main__")
    app.register_controllers(
        {
            TestController: lambda: TestController(),
        },
    )

    client = app.test_client()

    response = await client.get("/")
    assert response.status_code == 200
    data = await response.data
    assert data == b"Hello, World!"


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    app = BaseWebApp("__main__")

    client = app.test_client()
    response = await client.get("/health")
    assert response.status_code == 200


# A platform health check arrives with whatever Host the platform picks: one of several
# custom domains, any of which may be a subdomain, or the service's own internal name
# before any domain is verified. With SERVER_NAME set, routing rejects all but the
# configured host, so the check has to be answered regardless of Host.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host",
    [
        "example.test",
        "app.example.test",
        "www.example.test",
        "service.onrender.com",
        "10.0.0.5:9000",
    ],
)
async def test_health_endpoint_answers_on_any_host(host: str) -> None:
    app = BaseWebApp("__main__", server_name="example.test")

    client = app.test_client()
    response = await client.get("/health", headers={"Host": host})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_endpoint_answers_head() -> None:
    app = BaseWebApp("__main__", server_name="example.test")

    client = app.test_client()
    response = await client.head("/health", headers={"Host": "app.example.test"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_health_endpoint_does_not_affect_other_routes() -> None:
    @controller(name="portal", subdomain="portal")
    class PortalController(BaseController):
        @route("/")
        def index(self) -> ResponseReturnValue:
            return "portal"

    app = BaseWebApp("__main__", server_name="example.test")
    app.register_controllers({PortalController: lambda: PortalController()})

    client = app.test_client()

    response = await client.get("/", headers={"Host": "portal.example.test"})
    assert response.status_code == 200
    assert await response.data == b"portal"

    response = await client.get("/missing", headers={"Host": "portal.example.test"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_failing_controller_factory_does_not_leak_a_traceback() -> None:
    # The controller is created in a before_request hook. When its factory raises, the
    # controller's own error handlers run with no controller to bind to; that must end in a
    # plain 500, not in Quart's debug traceback page with paths, source and frame locals.
    @controller(name="items", url_prefix="/items")
    class ItemsController(BaseController):
        @route("/")
        async def index(self) -> ResponseReturnValue:
            return "items"

        @error_handler(Exception)
        async def handle_error(self, error: Exception) -> ResponseReturnValue:
            return "handled", 500

    def failing_factory() -> ItemsController:
        message = "the database is down"
        raise RuntimeError(message)

    app = BaseWebApp("__main__", secret_key=b"test-secret-key")
    app.register_controllers({ItemsController: failing_factory})

    response = await call_asgi(app, "/items/")

    assert response.status == 500
    assert b"Traceback" not in response.body
    assert b"base_app.py" not in response.body
    assert b"the database is down" not in response.body


@dataclasses.dataclass
class AsgiResponse:
    status: int
    body: bytes


async def call_asgi(app: BaseWebApp, path: str) -> AsgiResponse:
    """Calls the app the way a server does: the test client turns on testing mode, which
    changes how Quart reports unhandled errors, so it cannot show the production behaviour."""
    response = AsgiResponse(status=0, body=b"")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"localhost")],
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 12345),
        "root_path": "",
    }

    request_sent = False
    response_complete = asyncio.Event()

    async def receive() -> dict[str, object]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # Quart keeps listening for a disconnect; give it one once the response is out.
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")
            if not message.get("more_body", False):
                response_complete.set()

    await app(scope, receive, send)
    return response
