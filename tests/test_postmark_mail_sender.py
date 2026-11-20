import json
from collections.abc import Callable

import httpx
import pytest

from scaffold.email_notification_service import Attachment, MailDeliveryError, MailRecipientInactiveError, Message
from scaffold.postmark_mail_sender import PostmarkMailSender

pytestmark = pytest.mark.asyncio


def _message() -> Message:
    return Message(
        subject="Test",
        recipients=["investor@example.com"],
        sender="sender@example.com",
        body="Hello",
    )


def _sender(handler: Callable[[httpx.Request], httpx.Response]) -> PostmarkMailSender:
    return PostmarkMailSender(api_token="token", transport=httpx.MockTransport(handler))  # noqa: S106


def _recording_sender(sent_payloads: list[dict[str, object]]) -> PostmarkMailSender:
    def respond(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"MessageID": "message-1"})

    return _sender(respond)


async def test_postmark_sender_returns_the_message_id() -> None:
    sent_payloads: list[dict[str, object]] = []
    sender = _recording_sender(sent_payloads)

    response = await sender.send(_message())

    assert response == "message-1"
    assert sent_payloads == [
        {
            "From": "sender@example.com",
            "To": "investor@example.com",
            "Subject": "Test",
            "TextBody": "Hello",
        },
    ]


async def test_postmark_sender_authenticates_with_the_server_token() -> None:
    received_tokens: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        received_tokens.append(request.headers.get("X-Postmark-Server-Token"))
        return httpx.Response(200, json={"MessageID": "message-1"})

    await _sender(respond).send(_message())

    assert received_tokens == ["token"]


async def test_postmark_sender_raises_inactive_recipient_for_postmark_error_code() -> None:
    sender = _sender(
        lambda request: httpx.Response(422, json={"ErrorCode": 406, "Message": "Inactive recipient"}),
    )

    with pytest.raises(MailRecipientInactiveError) as exc_info:
        await sender.send(_message())

    assert exc_info.value.reason == "postmark_inactive_recipient"


async def test_postmark_sender_raises_delivery_error_for_unexpected_failure() -> None:
    sender = _sender(lambda request: httpx.Response(500, json={"ErrorCode": 500, "Message": "Server error"}))

    with pytest.raises(MailDeliveryError) as exc_info:
        await sender.send(_message())

    assert "status=500" in str(exc_info.value)


async def test_postmark_sender_raises_delivery_error_for_request_failure() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        message = "connection failed"
        raise httpx.ConnectError(message, request=request)

    with pytest.raises(MailDeliveryError) as exc_info:
        await _sender(fail).send(_message())

    assert "connection failed" in str(exc_info.value)


async def test_postmark_sender_passes_custom_headers() -> None:
    sent_payloads: list[dict[str, object]] = []
    message = _message()
    message.headers = {
        "List-Unsubscribe": "<https://app.test/unsubscribe>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }

    await _recording_sender(sent_payloads).send(message)

    assert sent_payloads[0]["Headers"] == [
        {"Name": "List-Unsubscribe", "Value": "<https://app.test/unsubscribe>"},
        {"Name": "List-Unsubscribe-Post", "Value": "List-Unsubscribe=One-Click"},
    ]


async def test_postmark_sender_raises_inactive_recipient_for_the_http_status() -> None:
    sender = _sender(lambda request: httpx.Response(406, json={"Message": "Inactive recipient"}))

    with pytest.raises(MailRecipientInactiveError):
        await sender.send(_message())


async def test_inactive_recipient_is_a_delivery_error() -> None:
    sender = _sender(lambda request: httpx.Response(406, json={}))

    with pytest.raises(MailDeliveryError):
        await sender.send(_message())


async def test_postmark_sender_passes_html_reply_to_and_attachments() -> None:
    sent_payloads: list[dict[str, object]] = []
    message = _message()
    message.html = "<p>Hello</p>"
    message.reply_to = "replies@example.com"
    message.attachments = [Attachment(file_name="report.pdf", content_type="application/pdf", content=b"%PDF")]

    await _recording_sender(sent_payloads).send(message)

    assert sent_payloads[0]["HtmlBody"] == "<p>Hello</p>"
    assert sent_payloads[0]["ReplyTo"] == "replies@example.com"
    assert sent_payloads[0]["Attachments"] == [
        {"Name": "report.pdf", "Content": "JVBERg==", "ContentType": "application/pdf"},
    ]
