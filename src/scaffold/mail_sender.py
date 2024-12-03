from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from typing import override

import aiosmtplib

from .email_notification_service import MailSender, Message


class SmtpMailSender(MailSender):
    def __init__(
        self,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    @override
    async def send(self, input: Message) -> None:
        message = MIMEMultipart("alternative")

        message["Subject"] = input.subject
        message["From"] = input.sender
        message["To"] = ", ".join(input.recipients)
        message["Message-ID"] = make_msgid()

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
