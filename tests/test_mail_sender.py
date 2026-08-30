import dataclasses
from collections.abc import Mapping
from email.message import Message as MimeMessage
from typing import cast

import aiosmtplib
import pytest

from scaffold.email_notification_service import Message
from scaffold.mail_sender import SmtpMailSender


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
        self.sent.append(SentEmail(message=message, sender=sender, recipients=recipients))
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
) -> Message:
    return Message(
        subject=subject,
        recipients=recipients if recipients is not None else ["alice@example.com"],
        sender=sender,
        body=body,
        html=html,
        reply_to=reply_to,
        headers=headers,
    )


def get_parts(message: MimeMessage) -> list[MimeMessage]:
    payload = message.get_payload()
    assert isinstance(payload, list)
    return cast("list[MimeMessage]", payload)


@pytest.mark.asyncio
async def test_send_sets_the_basic_headers(smtp: FakeSmtp, mail_sender: SmtpMailSender) -> None:
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
async def test_send_returns_the_message_id_it_used(smtp: FakeSmtp, mail_sender: SmtpMailSender) -> None:
    message_id = await mail_sender.send(make_message())

    assert message_id.startswith("<")
    assert message_id.endswith(">")
    assert smtp.sent[0].message["Message-ID"] == message_id


@pytest.mark.asyncio
async def test_send_uses_a_fresh_message_id_per_message(smtp: FakeSmtp, mail_sender: SmtpMailSender) -> None:
    first = await mail_sender.send(make_message())
    second = await mail_sender.send(make_message())

    assert first != second


@pytest.mark.asyncio
async def test_reply_to_is_set_when_provided(smtp: FakeSmtp, mail_sender: SmtpMailSender) -> None:
    reply_to = "reply+workspace.conversation.audience.sig@inbound.example.com"

    await mail_sender.send(make_message(reply_to=reply_to))

    sent = smtp.sent[0]
    assert sent.message["Reply-To"] == reply_to
    # From stays the caller's own address; only replies are routed elsewhere.
    assert sent.message["From"] == "support@theircompany.com"


@pytest.mark.asyncio
async def test_reply_to_is_absent_when_not_provided(smtp: FakeSmtp, mail_sender: SmtpMailSender) -> None:
    await mail_sender.send(make_message())

    assert smtp.sent[0].message["Reply-To"] is None


@pytest.mark.asyncio
async def test_custom_headers_are_applied(smtp: FakeSmtp, mail_sender: SmtpMailSender) -> None:
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
async def test_body_and_html_are_attached_as_alternatives(smtp: FakeSmtp, mail_sender: SmtpMailSender) -> None:
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
async def test_message_id_domain_is_not_taken_from_the_sender(smtp: FakeSmtp, mail_sender: SmtpMailSender) -> None:
    message_id = await mail_sender.send(make_message(sender="support@theircompany.com"))

    domain = get_message_id_domain(message_id)
    assert domain != ""
    assert domain != "theircompany.com"
