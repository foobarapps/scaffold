import pytest
from hypercorn.typing import ASGIFramework, ASGIReceiveCallable, ASGISendCallable, Scope
from quart import Response
from quart.typing import ResponseReturnValue

from scaffold.web import BaseController, BaseWebApp, controller, route


class TeapotError(Exception):
    pass


@controller(name="greetings")
class GreetingsController(BaseController):
    @route("/hello/<name>")
    async def hello(self, name: str) -> str:
        return f"Hello {name}"

    @route("/teapot")
    async def teapot(self) -> str:
        raise TeapotError

    @route("/goodbye", defaults={"name": None})
    @route("/goodbye/<name>")
    async def goodbye(self, name: str | None) -> str:
        return f"Goodbye {name or 'everyone'}"


def create_app(server_name: str | None = None) -> BaseWebApp:
    app = BaseWebApp("__main__", server_name=server_name)
    app.register_controllers({GreetingsController: GreetingsController})
    return app


@pytest.mark.asyncio
async def test_after_request_runs_for_every_controller() -> None:
    app = create_app()

    async def mark(response: Response) -> Response:
        response.headers["X-Marked"] = "yes"
        return response

    app.after_request(mark)

    response = await app.test_client().get("/hello/Ada")

    assert response.headers["X-Marked"] == "yes"


@pytest.mark.asyncio
async def test_registered_error_handler_handles_errors_of_any_controller() -> None:
    app = create_app()

    async def handle_teapot(error: TeapotError) -> ResponseReturnValue:
        return "I'm a teapot", 418

    app.register_error_handler(TeapotError, handle_teapot)

    response = await app.test_client().get("/teapot")

    assert response.status_code == 418
    assert await response.get_data(as_text=True) == "I'm a teapot"


@pytest.mark.asyncio
async def test_wrapped_asgi_app_is_used_by_the_test_client() -> None:
    app = create_app()
    seen_paths: list[str] = []

    def record_paths(asgi_app: ASGIFramework) -> ASGIFramework:
        async def middleware(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
            if scope["type"] == "http":
                seen_paths.append(scope["path"])
            await asgi_app(scope, receive, send)  # pyright: ignore[reportCallIssue, reportArgumentType]

        return middleware  # pyright: ignore[reportReturnType]

    app.wrap_asgi_app(record_paths)

    response = await app.test_client().get("/hello/Ada")

    assert response.status_code == 200
    assert seen_paths == ["/hello/Ada"]


def test_build_url_builds_an_absolute_url_outside_of_a_request() -> None:
    app = create_app(server_name="example.test")

    assert app.build_url("greetings.hello", name="Ada") == "http://example.test/hello/Ada"


def test_build_url_turns_extra_values_into_query_parameters() -> None:
    app = create_app(server_name="example.test")

    url = app.build_url("greetings.hello", name="Ada", scheme="https", utm_source="email")

    assert url == "https://example.test/hello/Ada?utm_source=email"


def test_build_url_takes_the_host_as_an_argument_when_the_app_has_no_server_name() -> None:
    app = create_app()

    assert app.build_url("greetings.hello", server_name="other.test", name="Ada") == "http://other.test/hello/Ada"


def test_build_url_needs_a_server_name() -> None:
    app = create_app()

    with pytest.raises(RuntimeError, match="server name"):
        app.build_url("greetings.hello", name="Ada")


@pytest.mark.asyncio
async def test_app_without_a_server_name_answers_on_any_host() -> None:
    app = create_app()

    response = await app.test_client().get("/hello/Ada", headers={"Host": "anything.test:8585"})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_assets_can_be_left_to_the_app() -> None:
    app = BaseWebApp("__main__", serve_assets=False)

    @controller(name="assets")
    class OwnAssetsController(BaseController):
        @route("/assets/<path:path>")
        async def asset(self, path: str) -> str:
            return f"own {path}"

    app.register_controllers({OwnAssetsController: OwnAssetsController})

    response = await app.test_client().get("/assets/app.css")

    assert await response.get_data(as_text=True) == "own app.css"


@pytest.mark.asyncio
async def test_request_context_is_available_for_tests() -> None:
    app = create_app()

    async with app.test_request_context("/hello/Ada"):
        assert GreetingsController.url_for("greetings.hello", name="Grace") == "/hello/Grace"


@pytest.mark.asyncio
async def test_stacked_routes_serve_one_method_at_several_rules() -> None:
    client = create_app().test_client()

    with_name = await client.get("/goodbye/Ada")
    without_name = await client.get("/goodbye")

    assert await with_name.get_data(as_text=True) == "Goodbye Ada"
    assert await without_name.get_data(as_text=True) == "Goodbye everyone"


@pytest.mark.asyncio
async def test_stacked_routes_share_one_endpoint() -> None:
    app = create_app()

    async with app.test_request_context("/"):
        assert GreetingsController.url_for("greetings.goodbye") == "/goodbye"
        assert GreetingsController.url_for("greetings.goodbye", name="Ada") == "/goodbye/Ada"
