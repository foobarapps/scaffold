import pytest
from quart.typing import ResponseReturnValue

from scaffold.web.base_app import BaseWebApp
from scaffold.web.base_controller import BaseController
from scaffold.web.decorators import controller, route


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
