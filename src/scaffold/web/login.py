"""
Session based sign-in state for a web app.

    login_manager = LoginManager[User]()
    login_manager.user_loader(load_user)
    login_manager.login_view = "auth.sign_in"
    app.register_extension(login_manager)

The signed-in user is loaded once per request and is available as `current_user`, in
views and in templates. `login_user` and `logout_user` sign a user in and out, and
`login_required` guards a view.

The session keys and the remember-me cookie have the same names and formats as the ones
Flask-Login writes, so an app moving over from Flask-Login keeps its users signed in.
"""

import datetime
import hmac
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from hashlib import sha512
from typing import Final, Protocol, cast
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from quart import (
    Response,
    abort,
    current_app,
    flash,
    g,
    has_request_context,
    redirect,
    request,
    session,
    url_for,
)
from quart.typing import ResponseReturnValue
from werkzeug.local import LocalProxy

from .base_app import BaseWebApp

USER_ID_SESSION_KEY: Final = "_user_id"
FRESH_SESSION_KEY: Final = "_fresh"
REMEMBER_SESSION_KEY: Final = "_remember"
NEXT_SESSION_KEY: Final = "next"
# Written by Flask-Login for its session protection, which is not used any more
LEGACY_SESSION_IDENTIFIER_KEY: Final = "_id"

# The remember-me cookie is written when the response goes out, so signing in and out only
# leave a note in the session saying what to do with it. Values are Flask-Login's.
REMEMBER_SET: Final = "set"
REMEMBER_CLEAR: Final = "clear"

# Defaults of the REMEMBER_COOKIE_* settings, which mean what they mean in Flask-Login:
# REMEMBER_COOKIE_NAME, _DURATION (a timedelta or seconds), _DOMAIN, _PATH, _SECURE,
# _HTTPONLY and _SAMESITE.
REMEMBER_COOKIE_NAME: Final = "remember_token"
REMEMBER_COOKIE_DURATION: Final = datetime.timedelta(days=365)

EXEMPT_METHODS: Final = frozenset({"OPTIONS"})
CONFIG_KEY: Final = "LOGIN_MANAGER"


class AuthenticatedUser(Protocol):
    @property
    def is_authenticated(self) -> bool: ...

    @property
    def is_active(self) -> bool:
        """False for an account that may not sign in, e.g. a suspended one."""
        ...

    def get_id(self) -> str: ...


class AnonymousUser:
    @property
    def is_authenticated(self) -> bool:
        return False

    @property
    def is_active(self) -> bool:
        return False

    @property
    def is_anonymous(self) -> bool:
        return True

    def get_id(self) -> None:
        return None


type UserLoader[U: AuthenticatedUser] = Callable[[str], Awaitable[U | None]]


class LoginManager[U: AuthenticatedUser]:
    def __init__(self, skip_request: Callable[[], bool] | None = None) -> None:
        """
        `skip_request` tells, during a request, whether the request should be left alone:
        no user is loaded and no cookie is written for it. Use it for responses that have
        to stay cacheable, such as assets, because touching the session adds Vary: Cookie.
        """
        self.login_view: str | None = None
        self.login_message: str | None = "Please log in to access this page."
        self.login_message_category = "message"
        self._user_loader: UserLoader[U] | None = None
        self._skip_request = skip_request or _never

    def init_app(self, app: BaseWebApp) -> None:
        app.config[CONFIG_KEY] = self
        app.before_request(self._load_user)
        app.after_request(self._update_remember_cookie)  # pyright: ignore[reportArgumentType]
        app.context_processor(_user_context_processor)

    def user_loader(self, callback: UserLoader[U]) -> UserLoader[U]:
        self._user_loader = callback
        return callback

    async def unauthorized(self) -> ResponseReturnValue:
        if not self.login_view:
            abort(401)

        if self.login_message:
            await flash(self.login_message, category=self.login_message_category)

        target_url = _expand_login_view(self.login_view)
        if current_app.config.get("USE_SESSION_FOR_NEXT", False):  # pyright: ignore[reportUnknownMemberType]
            session[NEXT_SESSION_KEY] = _make_next_param(target_url, request.url)
            return redirect(target_url)

        return redirect(login_url(self.login_view, next_url=request.url))

    async def _load_user(self) -> None:
        if self._skip_request():
            return

        if self._user_loader is None:
            message = "A user loader has to be registered"
            raise RuntimeError(message)

        user: U | None = None

        user_id: object = session.get(USER_ID_SESSION_KEY)  # pyright: ignore[reportUnknownMemberType]
        if user_id is not None:
            user = await self._user_loader(str(user_id))

        if user is None and session.get(REMEMBER_SESSION_KEY) != REMEMBER_CLEAR:
            cookie = request.cookies.get(_remember_cookie_name())
            remembered_user_id = _decode_cookie(cookie) if cookie is not None else None
            if remembered_user_id is not None:
                session[USER_ID_SESSION_KEY] = remembered_user_id
                session[FRESH_SESSION_KEY] = False
                user = await self._user_loader(remembered_user_id)

        g.login_user = user

    async def _update_remember_cookie(self, response: Response) -> Response:
        if self._skip_request() or REMEMBER_SESSION_KEY not in session:
            return response

        operation = session.pop(REMEMBER_SESSION_KEY, None)

        config = cast("Mapping[str, object]", current_app.config)
        name = _remember_cookie_name()
        domain = cast("str | None", config.get("REMEMBER_COOKIE_DOMAIN"))
        path = cast("str", config.get("REMEMBER_COOKIE_PATH", "/"))

        if operation == REMEMBER_SET and USER_ID_SESSION_KEY in session:
            duration = cast(
                "datetime.timedelta | int",
                config.get("REMEMBER_COOKIE_DURATION", REMEMBER_COOKIE_DURATION),
            )
            if not isinstance(duration, datetime.timedelta):
                duration = datetime.timedelta(seconds=duration)
            response.set_cookie(
                name,
                value=_encode_cookie(str(session[USER_ID_SESSION_KEY])),
                expires=datetime.datetime.now(datetime.UTC) + duration,
                domain=domain,
                path=path,
                secure=bool(config.get("REMEMBER_COOKIE_SECURE", False)),
                httponly=bool(config.get("REMEMBER_COOKIE_HTTPONLY", True)),
                samesite=cast("str | None", config.get("REMEMBER_COOKIE_SAMESITE")),
            )
        elif operation == REMEMBER_CLEAR:
            response.delete_cookie(name, domain=domain, path=path)

        return response


def _never() -> bool:
    return False


def _remember_cookie_name() -> str:
    config = cast("Mapping[str, object]", current_app.config)
    return cast("str", config.get("REMEMBER_COOKIE_NAME", REMEMBER_COOKIE_NAME))


def get_current_user() -> AuthenticatedUser | AnonymousUser:
    if has_request_context():
        user = cast("AuthenticatedUser | None", g.get("login_user"))
        if user is not None:
            return user
    return AnonymousUser()


# Typed loosely on purpose: views and templates read attributes of the app's own user model.
# Code that needs the precise type calls get_current_user() and narrows it.
current_user = LocalProxy(get_current_user)


def login_user(user: AuthenticatedUser, *, remember: bool = False, force: bool = False) -> bool:
    """
    Sign the user in and tell whether that happened.

    A user who is not active is refused, unless `force` is set.
    """
    if not force and not user.is_active:
        return False

    session[USER_ID_SESSION_KEY] = user.get_id()
    session[FRESH_SESSION_KEY] = True
    session.pop(LEGACY_SESSION_IDENTIFIER_KEY, None)
    if remember:
        session[REMEMBER_SESSION_KEY] = REMEMBER_SET
    g.login_user = user
    return True


def logout_user() -> None:
    session.pop(USER_ID_SESSION_KEY, None)
    session.pop(FRESH_SESSION_KEY, None)
    session.pop(LEGACY_SESSION_IDENTIFIER_KEY, None)
    if _remember_cookie_name() in request.cookies:
        session[REMEMBER_SESSION_KEY] = REMEMBER_CLEAR
    g.login_user = None


def login_required[**P](
    view: Callable[P, Awaitable[ResponseReturnValue]],
) -> Callable[P, Awaitable[ResponseReturnValue]]:
    @wraps(view)
    async def decorated_view(*args: P.args, **kwargs: P.kwargs) -> ResponseReturnValue:
        if request.method not in EXEMPT_METHODS and not get_current_user().is_authenticated:
            manager = cast("LoginManager[AuthenticatedUser]", current_app.config[CONFIG_KEY])
            return await manager.unauthorized()
        return await view(*args, **kwargs)

    return decorated_view


def login_url(login_view: str, next_url: str | None = None) -> str:
    base = _expand_login_view(login_view)
    if next_url is None:
        return base

    parsed = urlsplit(base)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query[NEXT_SESSION_KEY] = [_make_next_param(base, next_url)]
    return urlunsplit(parsed._replace(query=urlencode(query, doseq=True)))


def _user_context_processor() -> dict[str, object]:
    return {"current_user": get_current_user()}


def _expand_login_view(login_view: str) -> str:
    if login_view.startswith(("https://", "http://", "/")):
        return login_view
    return url_for(login_view)


def _make_next_param(target_url: str, current_url: str) -> str:
    # Drops the scheme and host when they match the login page's, to keep the value short
    target = urlsplit(target_url)
    current = urlsplit(current_url)
    same_scheme = not target.scheme or target.scheme == current.scheme
    same_host = not target.netloc or target.netloc == current.netloc
    if same_scheme and same_host:
        return urlunsplit(("", "", current.path, current.query, ""))
    return current_url


def _cookie_digest(payload: str) -> str:
    secret_key = cast("str | bytes", current_app.config["SECRET_KEY"])
    key = secret_key.encode("latin1") if isinstance(secret_key, str) else secret_key
    return hmac.new(key, payload.encode("utf-8"), sha512).hexdigest()


def _encode_cookie(payload: str) -> str:
    return f"{payload}|{_cookie_digest(payload)}"


def _decode_cookie(cookie: str) -> str | None:
    payload, separator, digest = cookie.rpartition("|")
    if not separator:
        return None
    if hmac.compare_digest(_cookie_digest(payload).encode(), digest.encode()):
        return payload
    return None
