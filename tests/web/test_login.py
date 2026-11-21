import dataclasses

import pytest
from quart import request
from quart.testing import QuartClient

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
    is_active: bool = True

    @property
    def is_authenticated(self) -> bool:
        return True

    def get_id(self) -> str:
        return self.id


USERS = {"1": User("1", "Eva"), "2": User("2", "Suspended", is_active=False)}


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
            form = await request.form  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            signed_in = login_user(
                USERS[form.get("user", "1")],  # pyright: ignore[reportUnknownMemberType]
                remember=form.get("remember") == "1",  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
                force=form.get("force") == "1",  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            )
            return "signed in" if signed_in else "refused"
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


async def text(client: QuartClient, path: str) -> str:
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
    async with client.session_transaction() as session:  # pyright: ignore[reportUnknownMemberType]
        session["_user_id"] = "999"

    assert await text(client, "/whoami") == "anonymous"


async def test_remember_me_signs_the_user_in_after_the_session_is_gone() -> None:
    app = make_app()
    client = app.test_client()
    response = await client.post("/sign-in", form={"remember": "1"})
    assert REMEMBER_COOKIE_NAME in response.headers["Set-Cookie"]

    async with client.session_transaction() as session:  # pyright: ignore[reportUnknownMemberType]
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
    async with client.session_transaction() as session:  # pyright: ignore[reportUnknownMemberType]
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


async def test_inactive_user_is_refused_unless_forced() -> None:
    client = make_app().test_client()

    response = await client.post("/sign-in", form={"user": "2"})
    assert await response.get_data(as_text=True) == "refused"
    assert await text(client, "/whoami") == "anonymous"

    response = await client.post("/sign-in", form={"user": "2", "force": "1"})
    assert await response.get_data(as_text=True) == "signed in"
    assert await text(client, "/whoami") == "Suspended"


async def test_remember_cookie_is_not_secure_by_default_but_is_http_only() -> None:
    client = make_app().test_client()

    cookie = (await client.post("/sign-in", form={"remember": "1"})).headers["Set-Cookie"]

    assert cookie.startswith(f"{REMEMBER_COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie
    assert "Path=/" in cookie


async def test_remember_cookie_follows_the_settings_flask_login_uses() -> None:
    app = make_app()
    app.config.update(
        REMEMBER_COOKIE_NAME="keep_me",
        REMEMBER_COOKIE_DURATION=60,
        REMEMBER_COOKIE_DOMAIN="example.test",
        REMEMBER_COOKIE_PATH="/app",
        REMEMBER_COOKIE_SECURE=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
    )
    client = app.test_client()

    cookie = (await client.post("/sign-in", form={"remember": "1"})).headers["Set-Cookie"]

    assert cookie.startswith("keep_me=")
    for attribute in ("Domain=example.test", "Path=/app", "Secure", "HttpOnly", "SameSite=Lax"):
        assert attribute in cookie
