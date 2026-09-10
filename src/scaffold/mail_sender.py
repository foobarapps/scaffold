from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import make_msgid
from typing import override

import aiosmtplib

from .email_notification_service import Attachment, MailSender, Message

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
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        # When unset, the Message-ID domain is the local host name.
        self.message_id_domain = message_id_domain

    @override
    async def send(self, input: Message) -> str:
        message_id = make_msgid(domain=self.message_id_domain)
        message = build_mime_message(input, message_id)

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
