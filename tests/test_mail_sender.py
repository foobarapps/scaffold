import base64
import dataclasses
from collections.abc import Mapping, Sequence
from email.message import Message as MimeMessage
from typing import cast

import aiosmtplib
import pytest

from scaffold.email_notification_service import Attachment, Message
from scaffold.mail_sender import SmtpMailSender, build_mime_message


@dataclasses.dataclass
class SentEmail:
    message: MimeMessage
    sender: str
    recipients: list[str]


class FakeSmtp:
    """Stands in for aiosmtplib.send so that nothing goes over the network."""

    def __init__(self) -> None:
        self.sent: list[SentEmail] = []

    async def send(
        self,
        message: MimeMessage,
        *,
        sender: str,
        recipients: list[str],
        **kwargs: object,
    ) -> tuple[dict[str, object], str]:
        self.sent.append(
            SentEmail(message=message, sender=sender, recipients=recipients),
        )
        return ({}, "OK")


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> FakeSmtp:
    fake = FakeSmtp()
    monkeypatch.setattr(aiosmtplib, "send", fake.send)
    return fake


@pytest.fixture
def mail_sender() -> SmtpMailSender:
    return SmtpMailSender(host="smtp.example.com", port=587)


def make_message(
    *,
    subject: str = "Hello",
    recipients: list[str] | None = None,
    sender: str = "support@theircompany.com",
    body: str = "Plain text body",
    html: str | None = None,
    reply_to: str | None = None,
    headers: Mapping[str, str] | None = None,
    attachments: Sequence[Attachment] = (),
) -> Message:
    return Message(
        subject=subject,
        recipients=recipients if recipients is not None else ["alice@example.com"],
        sender=sender,
        body=body,
        html=html,
        reply_to=reply_to,
        headers=headers,
        attachments=attachments,
    )


def get_parts(message: MimeMessage) -> list[MimeMessage]:
    payload = message.get_payload()
    assert isinstance(payload, list)
    return cast("list[MimeMessage]", payload)


@pytest.mark.asyncio
async def test_send_sets_the_basic_headers(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    await mail_sender.send(
        make_message(recipients=["alice@example.com", "bob@example.com"]),
    )

    assert len(smtp.sent) == 1
    sent = smtp.sent[0]
    assert sent.sender == "support@theircompany.com"
    assert sent.recipients == ["alice@example.com", "bob@example.com"]
    assert sent.message["Subject"] == "Hello"
    assert sent.message["From"] == "support@theircompany.com"
    assert sent.message["To"] == "alice@example.com, bob@example.com"


@pytest.mark.asyncio
async def test_send_returns_the_message_id_it_used(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    message_id = await mail_sender.send(make_message())

    assert message_id.startswith("<")
    assert message_id.endswith(">")
    assert smtp.sent[0].message["Message-ID"] == message_id


@pytest.mark.asyncio
async def test_send_uses_a_fresh_message_id_per_message(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    first = await mail_sender.send(make_message())
    second = await mail_sender.send(make_message())

    assert first != second


@pytest.mark.asyncio
async def test_reply_to_is_set_when_provided(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    reply_to = "reply+workspace.conversation.audience.sig@inbound.example.com"

    await mail_sender.send(make_message(reply_to=reply_to))

    sent = smtp.sent[0]
    assert sent.message["Reply-To"] == reply_to
    # From stays the caller's own address; only replies are routed elsewhere.
    assert sent.message["From"] == "support@theircompany.com"


@pytest.mark.asyncio
async def test_reply_to_is_absent_when_not_provided(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    await mail_sender.send(make_message())

    assert smtp.sent[0].message["Reply-To"] is None


@pytest.mark.asyncio
async def test_custom_headers_are_applied(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    parent_id = "<parent@inbound.example.com>"

    await mail_sender.send(
        make_message(
            headers={
                "In-Reply-To": parent_id,
                "References": f"<root@inbound.example.com> {parent_id}",
                "X-Conversation-Id": "42",
            },
        ),
    )

    message = smtp.sent[0].message
    assert message["In-Reply-To"] == parent_id
    assert message["References"] == f"<root@inbound.example.com> {parent_id}"
    assert message["X-Conversation-Id"] == "42"


@pytest.mark.parametrize(
    "header",
    [
        "Subject",
        "From",
        "To",
        "Reply-To",
        "Message-ID",
        "MIME-Version",
        "Content-Type",
        # Reserved headers are matched case-insensitively.
        "message-id",
        "sUbJeCt",
    ],
)
@pytest.mark.asyncio
async def test_reserved_headers_cannot_be_overwritten(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
    header: str,
) -> None:
    message = make_message(
        reply_to="reply@inbound.example.com",
        headers={header: "spoofed"},
    )

    with pytest.raises(ValueError, match=header):
        await mail_sender.send(message)

    assert smtp.sent == []


@pytest.mark.asyncio
async def test_reserved_header_check_does_not_clobber_the_real_header(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    message = make_message(headers={"Subject": "spoofed"})

    with pytest.raises(ValueError, match="Subject"):
        await mail_sender.send(message)

    assert message.subject == "Hello"


@pytest.mark.asyncio
async def test_body_and_html_are_attached_as_alternatives(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    await mail_sender.send(make_message(html="<p>HTML body</p>"))

    message = smtp.sent[0].message
    assert message.get_content_subtype() == "alternative"
    parts = get_parts(message)
    assert [part.get_content_type() for part in parts] == ["text/plain", "text/html"]
    assert parts[0].get_payload(decode=True) == b"Plain text body"
    assert parts[1].get_payload(decode=True) == b"<p>HTML body</p>"


@pytest.mark.asyncio
async def test_only_the_plain_text_part_is_attached_without_html(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    await mail_sender.send(make_message())

    parts = get_parts(smtp.sent[0].message)
    assert [part.get_content_type() for part in parts] == ["text/plain"]


def get_message_id_domain(message_id: str) -> str:
    return message_id.removeprefix("<").removesuffix(">").rpartition("@")[2]


@pytest.mark.asyncio
async def test_configured_message_id_domain_is_used(smtp: FakeSmtp) -> None:
    mail_sender = SmtpMailSender(
        host="smtp.example.com",
        port=587,
        message_id_domain="inbound.helpdesk.example",
    )

    message_id = await mail_sender.send(make_message(sender="support@theircompany.com"))

    assert get_message_id_domain(message_id) == "inbound.helpdesk.example"
    # The customer's own brand still owns the From header.
    assert smtp.sent[0].message["From"] == "support@theircompany.com"
    assert smtp.sent[0].message["Message-ID"] == message_id


@pytest.mark.asyncio
async def test_message_id_domain_is_not_taken_from_the_sender(
    smtp: FakeSmtp,
    mail_sender: SmtpMailSender,
) -> None:
    message_id = await mail_sender.send(make_message(sender="support@theircompany.com"))

    domain = get_message_id_domain(message_id)
    assert domain != ""
    assert domain != "theircompany.com"


MESSAGE_ID = "<generated@inbound.example.com>"

PDF = Attachment(
    file_name="invoice.pdf",
    content_type="application/pdf",
    content=b"%PDF-1.4 fake bytes",
)


def structure(message: MimeMessage) -> list[str]:
    """The part tree, depth first, which is what a client reads the message's shape from."""
    return [part.get_content_type() for part in message.walk()]


def attachment_parts(message: MimeMessage) -> list[MimeMessage]:
    return [part for part in message.walk() if part.get_content_disposition() == "attachment"]


def decoded_payload(part: MimeMessage) -> bytes:
    """The part's own bytes, back from the base64 transfer encoding it was sent under."""
    payload = part.get_payload()
    assert isinstance(payload, str)
    return base64.b64decode(payload)


def test_a_message_without_attachments_stays_a_plain_alternative() -> None:
    message = build_mime_message(make_message(html="<p>HTML body</p>"), MESSAGE_ID)

    # No mixed wrapper: byte-for-byte the shape callers got before attachments existed.
    assert structure(message) == ["multipart/alternative", "text/plain", "text/html"]
    assert attachment_parts(message) == []


def test_attachments_wrap_the_body_in_a_mixed_part() -> None:
    message = build_mime_message(
        make_message(html="<p>HTML body</p>", attachments=[PDF]),
        MESSAGE_ID,
    )

    assert structure(message) == [
        "multipart/mixed",
        "multipart/alternative",
        "text/plain",
        "text/html",
        "application/pdf",
    ]


def test_an_attachment_carries_its_name_type_and_bytes() -> None:
    message = build_mime_message(make_message(attachments=[PDF]), MESSAGE_ID)

    parts = attachment_parts(message)
    assert len(parts) == 1
    part = parts[0]
    assert part.get_filename() == "invoice.pdf"
    assert part.get_content_type() == "application/pdf"
    assert decoded_payload(part) == b"%PDF-1.4 fake bytes"


def test_headers_are_set_on_the_outer_part_when_there_are_attachments() -> None:
    message = build_mime_message(
        make_message(
            recipients=["alice@example.com", "bob@example.com"],
            attachments=[PDF],
        ),
        MESSAGE_ID,
    )

    # Read off the mixed part itself; a mail server never looks inside for these.
    assert message.get_content_type() == "multipart/mixed"
    assert message["Subject"] == "Hello"
    assert message["Message-ID"] == MESSAGE_ID
    assert message["To"] == "alice@example.com, bob@example.com"


def test_a_non_ascii_file_name_survives() -> None:
    attachment = Attachment(
        file_name="\u00fa\u010dtenka.pdf",
        content_type="application/pdf",
        content=b"bytes",
    )

    message = build_mime_message(make_message(attachments=[attachment]), MESSAGE_ID)

    assert attachment_parts(message)[0].get_filename() == "\u00fa\u010dtenka.pdf"


def test_an_unusable_content_type_falls_back_to_octet_stream() -> None:
    attachment = Attachment(
        file_name="mystery.bin",
        content_type="nonsense",
        content=b"bytes",
    )

    message = build_mime_message(make_message(attachments=[attachment]), MESSAGE_ID)

    assert attachment_parts(message)[0].get_content_type() == "application/octet-stream"
