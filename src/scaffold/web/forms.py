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
            return current_app.config["SECRET_KEY"]
