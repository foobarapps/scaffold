import abc
import dataclasses
import inspect
import pathlib
from collections.abc import Callable, Mapping, Sequence
from typing import Any, cast
from urllib.parse import quote

from jinja2 import Environment, FileSystemLoader, select_autoescape

from scaffold.web.base_app import BaseWebApp


@dataclasses.dataclass
class Attachment:
    file_name: str
    # Taken as-is from whatever produced the file, so it may be malformed; the mail sender
    # falls back to application/octet-stream rather than putting a broken type on the wire.
    content_type: str
    content: bytes


@dataclasses.dataclass
class Message:
    subject: str
    recipients: list[str]
    sender: str
    body: str
    html: str | None = None
    reply_to: str | None = None
    headers: Mapping[str, str] | None = None
    # Last, so that existing positional construction keeps working. The empty tuple is
    # immutable, so it needs no default_factory.
    attachments: Sequence[Attachment] = ()


class MailDeliveryError(Exception):
    """The mail service could not be reached or refused the message."""


class MailRecipientInactiveError(MailDeliveryError):
    """
    The mail service will not deliver to this recipient, e.g. after a hard bounce or a spam complaint.

    Sending again will not help, so the app should stop mailing the address.
    """

    def __init__(self, reason: str = "inactive_recipient") -> None:
        self.reason = reason
        super().__init__(reason)


class MailSender(abc.ABC):
    @abc.abstractmethod
    async def send(
        self,
        message: Message,
        /,
    ) -> str:
        """Send the message and return the Message-ID header value it was sent with."""
        raise NotImplementedError


class EmailNotificationService:
    def __init__(
        self,
        web_app: BaseWebApp,
        mail_sender: MailSender,
        default_sender_email: str,
    ) -> None:
        self.web_app = web_app
        self.mail_sender = mail_sender
        self.default_sender_email = default_sender_email

    def get_templates_dir(self) -> pathlib.Path:
        return pathlib.Path(inspect.getfile(self.__class__)).parent / "templates"

    def get_jinja_env(self) -> Environment:
        templates_dir = self.get_templates_dir()
        return Environment(
            # TODO Write a custom preloading loader which preloads the templates to memory during startup
            # because Jinja does not support async template loading and blocks the async loop.
            # And use the loader in the web app as well.
            loader=FileSystemLoader(templates_dir),
            autoescape=select_autoescape(),
        )

    async def render_template(
        self,
        template_path: str,
        **context: Any,  # noqa: ANN401
    ) -> str:
        env = self.get_jinja_env()
        template = env.get_template(template_path)
        async with self.web_app.app_context():
            # Links in an email must point at the app's configured host. Quart's url_for
            # prefers the current request's adapter, which is bound to the client-supplied
            # Host header -- and background tasks inherit that request context -- so a client
            # could otherwise have a password-reset link sent out pointing at its own host.
            # build_url only ever uses the configured server name, and raises without one.
            env.globals["url_for"] = _external_url_for(self.web_app)  # pyright: ignore[reportArgumentType]
            return template.render(**context)


def _external_url_for(web_app: BaseWebApp) -> Callable[..., str]:
    """
    A url_for for email templates, taking Quart's arguments but always building with build_url.

    The host is never one a template or a request supplies: it is the app's server name.
    """

    def url_for(endpoint: str, **values: Any) -> str:  # noqa: ANN401
        # Links in an email are external whatever the template asks; the method only picks
        # between rules and does not change the URL that is built.
        values.pop("_external", None)
        values.pop("_method", None)
        anchor = values.pop("_anchor", None)
        scheme = cast("str | None", values.pop("_scheme", None))
        for reserved in ("server_name", "scheme"):
            if reserved in values:
                message = f"url_for in an email template does not take {reserved!r}: the host is the app's server name"
                raise TypeError(message)

        url = web_app.build_url(endpoint, scheme=scheme, **values)
        if anchor is not None:
            url += "#" + quote(str(anchor))
        return url

    return url_for
