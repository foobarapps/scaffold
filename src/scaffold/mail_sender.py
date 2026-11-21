from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from typing import override

import aiosmtplib

from .email_notification_service import Attachment, MailDeliveryError, MailSender, Message

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

# Used for an attachment whose content type is missing either half of type/subtype.
DEFAULT_CONTENT_TYPE = ("application", "octet-stream")


class SmtpMailSender(MailSender):
    def __init__(
        self,
        host: str,
        port: int,
        username: str | None = None,
        password: str | None = None,
        message_id_domain: str | None = None,
        use_tls: bool = False,
        start_tls: bool | None = None,
    ) -> None:
        """
        `use_tls` connects over TLS from the start (port 465, usually). `start_tls` upgrades
        a plain connection with STARTTLS: True requires it and False never does it. None, the
        default, requires it when the sender logs in and otherwise upgrades when the server
        offers it.
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_tls = use_tls
        # With credentials and no implicit TLS, STARTTLS is required by default: aiosmtplib's
        # opportunistic default would otherwise log in over plaintext whenever the server (or
        # anyone on the path to it) leaves STARTTLS out of its EHLO reply.
        if start_tls is None and not use_tls and self._logs_in:
            start_tls = True
        self.start_tls = start_tls
        # When unset, the Message-ID domain is the local host name.
        self.message_id_domain = message_id_domain

    @property
    def _logs_in(self) -> bool:
        # Both or neither: a user name on its own would make aiosmtplib try to log in
        return bool(self.username and self.password)

    @override
    async def send(self, input: Message) -> str:
        message_id = make_msgid(domain=self.message_id_domain)
        message = build_mime_message(input, message_id)

        try:
            await aiosmtplib.send(
                message,
                sender=input.sender,
                recipients=input.recipients,
                hostname=self.host,
                port=self.port,
                username=self.username if self._logs_in else None,
                password=self.password if self._logs_in else None,
                use_tls=self.use_tls,
                start_tls=self.start_tls,
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            # The same error whichever sender is in use, so a caller can handle one
            error_message = f"Failed to send email: {exc}"
            raise MailDeliveryError(error_message) from exc

        return message_id


class InMemoryMailSender(MailSender):
    """Keeps the messages instead of sending them, for tests and for running an app without mail."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    @override
    async def send(self, input: Message) -> str:
        self.messages.append(input)
        return make_msgid()


def build_mime_message(input: Message, message_id: str) -> MIMEMultipart:
    """
    The MIME message to put on the wire, built apart from sending so its shape can be examined.

    An email with attachments is multipart/mixed carrying the multipart/alternative body; one
    without is that alternative part on its own, with nothing wrapped around it.
    """
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText(input.body, "plain", "utf-8"))

    if input.html is not None:
        alternative.attach(MIMEText(input.html, "html", "utf-8"))

    if input.attachments:
        message = MIMEMultipart("mixed")
        message.attach(alternative)
        for attachment in input.attachments:
            message.attach(_to_mime_part(attachment))
    else:
        message = alternative

    # Set on whichever part ended up outermost, so that a mail server can read them.
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

    return message


def _to_mime_part(attachment: Attachment) -> MIMEBase:
    main_type, _, sub_type = attachment.content_type.partition("/")
    part = MIMEBase(
        *((main_type, sub_type) if main_type and sub_type else DEFAULT_CONTENT_TYPE),
    )
    part.set_payload(attachment.content)
    encoders.encode_base64(part)
    # add_header does the RFC 2231 encoding, so a file name with non-ASCII characters in it
    # survives instead of raising on the way into the header.
    part.add_header("Content-Disposition", "attachment", filename=attachment.file_name)

    return part
