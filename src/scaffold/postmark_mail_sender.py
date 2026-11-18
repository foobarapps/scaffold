import base64
from typing import override

import httpx

from .email_notification_service import MailDeliveryError, MailRecipientInactiveError, MailSender, Message

POSTMARK_API_URL = "https://api.postmarkapp.com/email"
# Postmark answers with this code, as the HTTP status and in the body, for a recipient it has
# deactivated after a hard bounce or a spam complaint.
INACTIVE_RECIPIENT_CODE = 406


class PostmarkMailSender(MailSender):
    def __init__(
        self,
        api_token: str,
        api_url: str = POSTMARK_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_token = api_token
        self.api_url = api_url
        self._transport = transport

    @override
    async def send(self, input: Message) -> str:
        """Send the message and return the id Postmark gave it."""
        try:
            async with httpx.AsyncClient(transport=self._transport) as client:
                response = await client.post(
                    self.api_url,
                    json=build_payload(input),
                    headers={
                        "Accept": "application/json",
                        "X-Postmark-Server-Token": self.api_token,
                    },
                )
        except httpx.HTTPError as exc:
            error_message = f"Failed to send email: {exc}"
            raise MailDeliveryError(error_message) from exc

        data = _json_object(response)

        if response.is_success:
            return str(data.get("MessageID", ""))

        error_code = str(data.get("ErrorCode", ""))
        if str(INACTIVE_RECIPIENT_CODE) in (str(response.status_code), error_code):
            raise MailRecipientInactiveError(reason="postmark_inactive_recipient")

        error_message = f"Failed to send email: status={response.status_code}, body={response.text}"
        raise MailDeliveryError(error_message)


def build_payload(message: Message) -> dict[str, object]:
    payload: dict[str, object] = {
        "From": message.sender,
        "To": ", ".join(message.recipients),
        "Subject": message.subject,
        "TextBody": message.body,
    }

    if message.html:
        payload["HtmlBody"] = message.html

    if message.reply_to is not None:
        payload["ReplyTo"] = message.reply_to

    if message.headers:
        payload["Headers"] = [{"Name": name, "Value": value} for name, value in message.headers.items()]

    if message.attachments:
        payload["Attachments"] = [
            {
                "Name": attachment.file_name,
                "Content": base64.b64encode(attachment.content).decode("ascii"),
                "ContentType": attachment.content_type or "application/octet-stream",
            }
            for attachment in message.attachments
        ]

    return payload


def _json_object(response: httpx.Response) -> dict[str, object]:
    try:
        data: object = response.json()
    except ValueError:
        return {}

    if not isinstance(data, dict):
        return {}

    return {str(key): value for key, value in data.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
