from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from typing import override

import aiosmtplib

from .email_notification_service import MailSender, Message

# Headers the sender constructs itself, either directly or through MIMEMultipart.
# Custom headers are not allowed to overwrite them.
RESERVED_HEADERS = frozenset(
    {
        "subject",
        "from",
        "to",
        "reply-to",
        "message-id",
        "mime-version",
        "content-type",
        "content-transfer-encoding",
    },
)


class SmtpMailSender(MailSender):
    def __init__(
        self,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        message_id_domain: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        # When unset, the Message-ID domain is the local host name.
        self.message_id_domain = message_id_domain

    @override
    async def send(self, input: Message) -> str:
        message = MIMEMultipart("alternative")

        message_id = make_msgid(domain=self.message_id_domain)

        message["Subject"] = input.subject
        message["From"] = input.sender
        message["To"] = ", ".join(input.recipients)
        message["Message-ID"] = message_id

        if input.reply_to is not None:
            message["Reply-To"] = input.reply_to

        for name, value in (input.headers or {}).items():
            if name.lower() in RESERVED_HEADERS:
                error_message = f"Header {name!r} is set by the mail sender and cannot be overridden"
                raise ValueError(error_message)
            message[name] = value

        plain_text_message = MIMEText(input.body, "plain", "utf-8")
        message.attach(plain_text_message)

        if input.html is not None:
            html_message = MIMEText(input.html, "html", "utf-8")
            message.attach(html_message)

        await aiosmtplib.send(
            message,
            sender=input.sender,
            recipients=input.recipients,
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
        )

        return message_id
