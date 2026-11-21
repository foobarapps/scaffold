import pathlib

import pytest
from quart.typing import ResponseReturnValue

from scaffold.email_notification_service import EmailNotificationService, MailSender, Message
from scaffold.web.base_app import BaseWebApp
from scaffold.web.base_controller import BaseController
from scaffold.web.decorators import controller, route


class NullMailSender(MailSender):
    async def send(self, message: Message, /) -> str:
        return "<id@example.test>"


def make_service(app: BaseWebApp, templates_dir: pathlib.Path) -> EmailNotificationService:
    class Service(EmailNotificationService):
        def get_templates_dir(self) -> pathlib.Path:
            return templates_dir

    return Service(app, NullMailSender(), "noreply@example.test")


def make_app(server_name: str | None, rendered: list[str], templates_dir: pathlib.Path) -> BaseWebApp:
    app = BaseWebApp("__main__", secret_key=b"test-secret-key", server_name=server_name)
    service = make_service(app, templates_dir)

    @controller(name="account", url_prefix="/account")
    class AccountController(BaseController):
        @route("/reset/<token>")
        async def reset(self, token: str) -> ResponseReturnValue:
            return token

        @route("/forgot", methods=["POST"])
        async def forgot(self) -> ResponseReturnValue:
            rendered.append(await service.render_template("reset.txt"))
            return "sent"

    app.register_controllers({AccountController: lambda: AccountController()})
    return app


@pytest.fixture
def templates_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "reset.txt").write_text("Reset: {{ url_for('account.reset', token='T0K') }}")
    return tmp_path


@pytest.mark.asyncio
async def test_email_links_use_the_configured_host(templates_dir: pathlib.Path) -> None:
    rendered: list[str] = []
    app = make_app("app.example.test", rendered, templates_dir)

    response = await app.test_client().post("/account/forgot", headers={"Host": "app.example.test"})

    assert response.status_code == 200
    assert rendered == ["Reset: http://app.example.test/account/reset/T0K"]


@pytest.mark.asyncio
async def test_email_links_never_take_their_host_from_the_request(templates_dir: pathlib.Path) -> None:
    # Without a configured host, the only host available is the client-supplied Host header,
    # which would let a client have a password-reset link sent out pointing at its own server.
    # Rendering refuses instead, so no link to the client's host is ever produced.
    rendered: list[str] = []
    app = make_app(None, rendered, templates_dir)

    response = await app.test_client().post("/account/forgot", headers={"Host": "attacker.invalid"})

    assert response.status_code == 500
    assert rendered == []
    assert b"attacker.invalid" not in await response.get_data(as_text=False)


@pytest.mark.asyncio
async def test_email_url_for_takes_quarts_arguments(tmp_path: pathlib.Path) -> None:
    # Templates written for Quart's url_for keep working: _external is implied, _scheme and
    # _anchor are honoured, and none of them leaks into the query string.
    (tmp_path / "reset.txt").write_text(
        "Reset: {{ url_for('account.reset', token='T0K', _external=True, _scheme='https', _anchor='top') }}",
    )
    rendered: list[str] = []
    app = make_app("app.example.test", rendered, tmp_path)

    response = await app.test_client().post("/account/forgot", headers={"Host": "app.example.test"})

    assert response.status_code == 200
    assert rendered == ["Reset: https://app.example.test/account/reset/T0K#top"]


@pytest.mark.asyncio
async def test_email_template_cannot_choose_the_host(tmp_path: pathlib.Path) -> None:
    (tmp_path / "reset.txt").write_text("{{ url_for('account.reset', token='T0K', server_name='attacker.invalid') }}")
    app = BaseWebApp("__main__", secret_key=b"test-secret-key", server_name="app.example.test")
    service = make_service(app, tmp_path)

    with pytest.raises(TypeError, match="server_name"):
        await service.render_template("reset.txt")
