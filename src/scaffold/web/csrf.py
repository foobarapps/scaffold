import hmac
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha1

from markupsafe import Markup, escape
from quart import abort, current_app, request, session
from werkzeug.datastructures import MultiDict

from .base_app import BaseWebApp

CSRF_FIELD_NAME = "csrf_token"
CSRF_SESSION_KEY = "csrf"
SAFE_CSRF_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
CSRF_TIME_FORMAT = "%Y%m%d%H%M%S"

type ViewFunction = Callable[..., object]


class CSRFError(ValueError):
    pass


class CSRFProtect:
    def __init__(self) -> None:
        self._exempt_views: set[str] = set()

    def init_app(self, app: BaseWebApp) -> None:
        app.jinja_env.globals["csrf_token"] = generate_csrf_token # type: ignore[reportUnknownVariableType]
        app.jinja_env.globals["csrf_input"] = generate_csrf_input # type: ignore[reportUnknownVariableType]
        app.before_request(self.protect)

    async def protect(self) -> None:
        if request.method in SAFE_CSRF_METHODS:
            return

        if self._is_exempt():
            return

        form_data: MultiDict[str, str] = await request.form  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        token: str | None = form_data.get(CSRF_FIELD_NAME) or request.headers.get(
            "X-CSRF-Token",
        )

        try:
            validate_csrf_token(token)
        except CSRFError:
            abort(400)

    def exempt[F: ViewFunction](self, view: F) -> F:
        self._exempt_views.add(_get_view_location(view))
        return view

    def _is_exempt(self) -> bool:
        if not request.endpoint:
            return False

        view = current_app.view_functions.get(request.endpoint)
        if view is None:
            return False

        return _get_view_location(view) in self._exempt_views


_csrf = CSRFProtect()


def register_csrf(app: BaseWebApp) -> None:
    _csrf.init_app(app)


def csrf_exempt[F: ViewFunction](view: F) -> F:
    return _csrf.exempt(view)


def generate_csrf_token() -> str:
    raw_token = _get_or_create_session_token()
    time_limit = _get_csrf_time_limit()
    expires = (
        ""
        if time_limit is None
        else (datetime.now() + time_limit).strftime(CSRF_TIME_FORMAT)
    )
    signature = _sign_token(raw_token, expires)
    return f"{expires}##{signature}"


def generate_csrf_input() -> Markup:
    return Markup('<input type="hidden" name="%s" value="%s">') % (
        escape(CSRF_FIELD_NAME),
        escape(generate_csrf_token()),
    )


def validate_csrf_token(token: str | None) -> None:
    if not token or "##" not in token:
        msg = "CSRF token missing."
        raise CSRFError(msg)

    raw_token = session.get(CSRF_SESSION_KEY)
    if not isinstance(raw_token, str):
        msg = "CSRF session token missing."
        raise CSRFError(msg)

    expires, hmac_csrf = token.split("##", 1)
    expected_hmac = _sign_token(raw_token, expires)
    if not hmac.compare_digest(expected_hmac, hmac_csrf):
        msg = "CSRF failed."
        raise CSRFError(msg)

    if expires and datetime.now().strftime(CSRF_TIME_FORMAT) > expires:
        msg = "CSRF token expired."
        raise CSRFError(msg)


def _get_or_create_session_token() -> str:
    raw_token = session.get(CSRF_SESSION_KEY)
    if isinstance(raw_token, str):
        return raw_token

    raw_token = secrets.token_hex(20)
    session[CSRF_SESSION_KEY] = raw_token
    return raw_token


def _sign_token(raw_token: str, expires: str) -> str:
    csrf_build = f"{raw_token}{expires}"
    return hmac.new(_get_csrf_secret(), csrf_build.encode(), digestmod=sha1).hexdigest()


def _get_csrf_secret() -> bytes:
    secret_key: object | None = current_app.config.get("SECRET_KEY")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if isinstance(secret_key, bytes):
        return secret_key
    if isinstance(secret_key, str):
        return secret_key.encode()

    msg = "SECRET_KEY must be a string or bytes"
    raise TypeError(msg)


def _get_csrf_time_limit() -> timedelta | None:
    configured_time_limit: object | None = current_app.config.get("CSRF_TIME_LIMIT")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if configured_time_limit is None:
        return timedelta(minutes=30)
    if isinstance(configured_time_limit, timedelta):
        return configured_time_limit
    if isinstance(configured_time_limit, int):
        return timedelta(seconds=configured_time_limit)
    if isinstance(configured_time_limit, str):
        return timedelta(seconds=int(configured_time_limit))

    msg = "CSRF_TIME_LIMIT must be an int, string, timedelta, or None"
    raise TypeError(msg)


def _get_view_location(view: ViewFunction) -> str:
    return f"{view.__module__}.{view.__name__}"
