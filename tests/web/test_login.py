import dataclasses

import pytest
from quart import request
from quart.typing import TestClientProtocol

from scaffold.web import BaseWebApp
from scaffold.web.login import (
    REMEMBER_COOKIE_NAME,
    LoginManager,
    current_user,
    get_current_user,
    login_required,
    login_user,
    logout_user,
)

pytestmark = pytest.mark.asyncio


@dataclasses.dataclass
class User:
    id: str
    name: str

    @property
    def is_authenticated(self) -> bool:
        return True

    def get_id(self) -> str:
        return self.id


USERS = {"1": User("1", "Eva")}


def make_app(*, login_view: str | None = "sign_in", skip_assets: bool = False) -> BaseWebApp:
    app = BaseWebApp("__main__", secret_key=b"secret", serve_assets=False)
    loads: list[str] = []
    app.config["LOADS"] = loads

    async def load_user(user_id: str) -> User | None:
        loads.append(user_id)
        return USERS.get(user_id)

    login_manager = LoginManager[User](
        skip_request=(lambda: request.path.startswith("/assets/")) if skip_assets else None,
    )
    login_manager.user_loader(load_user)
    login_manager.login_view = login_view
    app.register_extension(login_manager)

    async def sign_in() -> str:
        if request.method == "POST":
            form = await request.form
            login_user(USERS["1"], remember=form.get("remember") == "1")
        return "sign in page"

    async def sign_out() -> str:
        logout_user()
        return "signed out"

    @login_required
    async def account() -> str:
        return f"account of {current_user.name}"

    async def whoami() -> str:
        user = get_current_user()
        return user.name if isinstance(user, User) else "anonymous"

    async def asset() -> str:
        return "asset"

    app.add_url_rule("/sign-in", sign_in, methods=["GET", "POST"])
    app.add_url_rule("/sign-out", sign_out, methods=["POST"])
    app.add_url_rule("/account", account)
    app.add_url_rule("/whoami", whoami)
    app.add_url_rule("/assets/app.css", asset)
    return app


async def text(client: TestClientProtocol, path: str) -> str:
    return await (await client.get(path)).get_data(as_text=True)


async def test_visitor_is_anonymous() -> None:
    client = make_app().test_client()

    assert await text(client, "/whoami") == "anonymous"


async def test_signed_in_user_is_remembered_across_requests() -> None:
    client = make_app().test_client()

    await client.post("/sign-in")

    assert await text(client, "/whoami") == "Eva"
    assert await text(client, "/account") == "account of Eva"


async def test_signed_out_user_is_anonymous_again() -> None:
    client = make_app().test_client()
    await client.post("/sign-in")

    await client.post("/sign-out")

    assert await text(client, "/whoami") == "anonymous"


async def test_guarded_view_sends_a_visitor_to_the_login_view_and_back() -> None:
    client = make_app().test_client()

    response = await client.get("/account?tab=billing")

    assert response.status_code == 302
    assert response.headers["Location"] == "/sign-in?next=%2Faccount%3Ftab%3Dbilling"


async def test_guarded_view_answers_unauthorized_without_a_login_view() -> None:
    client = make_app(login_view=None).test_client()

    response = await client.get("/account")

    assert response.status_code == 401


async def test_user_who_no_longer_exists_is_anonymous() -> None:
    client = make_app().test_client()
    async with client.session_transaction() as session:
        session["_user_id"] = "999"

    assert await text(client, "/whoami") == "anonymous"


async def test_remember_me_signs_the_user_in_after_the_session_is_gone() -> None:
    app = make_app()
    client = app.test_client()
    response = await client.post("/sign-in", form={"remember": "1"})
    assert REMEMBER_COOKIE_NAME in response.headers["Set-Cookie"]

    async with client.session_transaction() as session:
        session.clear()

    assert await text(client, "/whoami") == "Eva"


async def test_remember_cookie_with_a_forged_signature_is_ignored() -> None:
    client = make_app().test_client()
    client.set_cookie("localhost", REMEMBER_COOKIE_NAME, "1|forged")

    assert await text(client, "/whoami") == "anonymous"


async def test_signing_out_clears_the_remember_cookie() -> None:
    client = make_app().test_client()
    await client.post("/sign-in", form={"remember": "1"})

    response = await client.post("/sign-out")

    assert f"{REMEMBER_COOKIE_NAME}=;" in response.headers["Set-Cookie"]
    async with client.session_transaction() as session:
        session.clear()
    assert await text(client, "/whoami") == "anonymous"


async def test_skipped_request_loads_no_user_and_leaves_the_session_alone() -> None:
    app = make_app(skip_assets=True)
    client = app.test_client()
    await client.post("/sign-in")
    app.config["LOADS"].clear()

    response = await client.get("/assets/app.css")

    assert app.config["LOADS"] == []
    assert "Cookie" not in response.headers.get("Vary", "")
