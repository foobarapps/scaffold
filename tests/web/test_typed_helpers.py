import io
from pathlib import Path

import pytest
from quart import Response, render_template_string, request
from quart.datastructures import FileStorage

from scaffold.web import (
    BaseController,
    BaseWebApp,
    app_config,
    controller,
    make_response,
    request_files,
    request_form,
    request_form_and_files,
    request_values,
    route,
    send_file,
    send_from_directory,
)
from scaffold.web.testing import session_transaction

pytestmark = pytest.mark.asyncio


def double(number: int) -> int:
    return number * 2


@controller(name="helpers")
class HelpersController(BaseController):
    @route("/form", methods=["POST"])
    async def form(self) -> str:
        form = await request_form()
        return form["name"].upper()

    @route("/values", methods=["POST"])
    async def values(self) -> str:
        values = await request_values()
        return f"{values['from_query']} {values['from_form']}"

    @route("/files", methods=["POST"])
    async def files(self) -> str:
        files = await request_files()
        return files["statement"].filename or ""

    @route("/form-and-files", methods=["POST"])
    async def form_and_files(self) -> str:
        data = await request_form_and_files()
        statement = data["statement"]
        assert isinstance(statement, FileStorage)
        return f"{data['name']} {statement.filename}"

    @route("/download")
    async def download(self) -> Response:
        return await send_file(
            io.BytesIO(b"report"),
            mimetype="text/plain",
            as_attachment=True,
            attachment_filename="report.txt",
        )

    @route("/readme")
    async def readme(self) -> Response:
        return await send_from_directory(Path(__file__).parent, Path(__file__).name)

    @route("/conditional")
    async def conditional(self) -> Response:
        response = await make_response("cached")
        response.set_etag("version-1")
        await response.make_conditional(request)
        return response

    @route("/config")
    async def config(self) -> str:
        return str(app_config()["GREETING"])

    @route("/template")
    async def template(self) -> str:
        return await render_template_string("{{ site_name }} {{ 'shout' | upper_case }} {{ double(2) }}")

    @route("/session")
    async def session_value(self) -> str:
        return str(self.session.get("visits"))


def create_app() -> BaseWebApp:
    app = BaseWebApp("__main__", secret_key=b"secret")
    app.config["GREETING"] = "hello"
    app.register_controllers({HelpersController: HelpersController})
    return app


async def test_request_form_is_typed_and_reads_the_form() -> None:
    client = create_app().test_client()

    response = await client.post("/form", form={"name": "ada"})

    assert await response.get_data(as_text=True) == "ADA"


async def test_request_values_combine_the_query_and_the_form() -> None:
    client = create_app().test_client()

    response = await client.post("/values?from_query=a", form={"from_form": "b"})

    assert await response.get_data(as_text=True) == "a b"


async def test_request_files_and_form_and_files() -> None:
    client = create_app().test_client()

    files_response = await client.post(
        "/files",
        files={"statement": FileStorage(io.BytesIO(b"1,2"), filename="statement.csv")},
    )
    combined_response = await client.post(
        "/form-and-files",
        form={"name": "ada"},
        files={"statement": FileStorage(io.BytesIO(b"1,2"), filename="statement.csv")},
    )

    assert await files_response.get_data(as_text=True) == "statement.csv"
    assert await combined_response.get_data(as_text=True) == "ada statement.csv"


async def test_send_file_and_send_from_directory() -> None:
    client = create_app().test_client()

    download = await client.get("/download")
    readme = await client.get("/readme")

    assert await download.get_data() == b"report"
    assert "report.txt" in download.headers["Content-Disposition"]
    assert readme.status_code == 200
    assert "test_send_file_and_send_from_directory" in await readme.get_data(as_text=True)


async def test_make_response_returns_a_response_that_can_be_made_conditional() -> None:
    client = create_app().test_client()

    first = await client.get("/conditional")
    second = await client.get("/conditional", headers={"If-None-Match": first.headers["ETag"]})

    assert first.status_code == 200
    assert second.status_code == 304


async def test_app_config_reads_the_config_of_the_current_app() -> None:
    client = create_app().test_client()

    response = await client.get("/config")

    assert await response.get_data(as_text=True) == "hello"


async def test_template_filters_and_globals() -> None:
    app = create_app()
    app.add_template_filter("upper_case", str.upper)
    app.add_template_global("site_name", "Scaffold")
    app.add_template_global("double", double)

    response = await app.test_client().get("/template")

    assert await response.get_data(as_text=True) == "Scaffold SHOUT 4"


async def test_session_transaction_changes_the_session_of_a_test_client() -> None:
    client = create_app().test_client()

    async with session_transaction(client) as session:
        session["visits"] = 3
    response = await client.get("/session")

    assert await response.get_data(as_text=True) == "3"


async def test_test_client_without_cookies_does_not_keep_a_session() -> None:
    client = create_app().test_client(use_cookies=False)

    response = await client.get("/session")

    assert await response.get_data(as_text=True) == "None"
