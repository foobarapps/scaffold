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
    app.register_controllers({
        TestController: lambda: TestController(),
    })

    client = app.test_client()

    response = await client.get("/")
    assert response.status_code == 200
    data = await response.data
    assert data == b"Hello, World!"
