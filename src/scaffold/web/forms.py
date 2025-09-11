from typing import Any

from quart import current_app, session
from quart.sessions import SessionMixin
from wtforms import Form
from wtforms.csrf.session import SessionCSRF


class BaseForm(Form):
    class Meta:
        csrf = True
        csrf_class = SessionCSRF

        @property
        def csrf_context(self) -> SessionMixin:
            return session

        @property
        def csrf_secret(self) -> bytes:
            secret_key: Any = current_app.config["SECRET_KEY"]  # pyright: ignore[reportUnknownVariableType]
            if secret_key is None:
                msg = "SECRET_KEY is not configured"
                raise ValueError(msg)
            if not isinstance(secret_key, bytes):
                msg = "SECRET_KEY must be bytes"
                raise TypeError(msg)
            return secret_key
