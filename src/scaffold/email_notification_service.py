import abc
import dataclasses
import inspect
import pathlib
from functools import partial
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from quart import url_for

from scaffold.web.base_app import BaseWebApp


@dataclasses.dataclass
class Message:
    subject: str
    recipients: list[str]
    sender: str
    body: str
    html: str | None = None


class MailSender(abc.ABC):
    @abc.abstractmethod
    async def send(
        self,
        message: Message,
    ) -> None:
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
        env = Environment(
            # TODO Write a custom preloading loader which preloads the templates to memory during startup
            # because Jinja does not support async template loading and blocks the async loop.
            # And use the loader in the web app as well.
            loader=FileSystemLoader(templates_dir),
            autoescape=select_autoescape(),
        )
        env.globals["url_for"] = partial(url_for, _external=True)
        return env

    async def render_template(
        self,
        template_path: str,
        **context: Any,  # noqa: ANN401
    ) -> str:
        env = self.get_jinja_env()
        template = env.get_template(template_path)
        async with self.web_app.app_context():
            return template.render(**context)
