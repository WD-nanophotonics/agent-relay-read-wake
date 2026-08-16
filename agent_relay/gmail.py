from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol

from gmail_courier.core import gmail_service


@dataclass(frozen=True)
class Attachment:
    filename: str
    data: bytes


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    thread_id: str | None
    received_at: str | None
    body: str
    attachments: tuple[Attachment, ...] = ()


class GmailGateway(Protocol):
    def test_connection(self) -> None: ...
    def list_messages(self) -> list[str]: ...
    def fetch_message(self, message_id: str) -> GmailMessage: ...


def _walk_parts(payload: dict[str, Any]):
    for part in payload.get("parts", []):
        yield from _walk_parts(part)
    yield payload


class GoogleGmailGateway:
    def __init__(self, auth_home):
        self.service = gmail_service(auth_home)

    def test_connection(self) -> None:
        self.service.users().getProfile(userId="me").execute()

    def list_messages(self) -> list[str]:
        # Deliberately broad: protocol validation, not Gmail search syntax, controls routing.
        response = self.service.users().messages().list(userId="me", q="in:inbox", maxResults=100).execute()
        return [item["id"] for item in response.get("messages", [])]

    def fetch_message(self, message_id: str) -> GmailMessage:
        raw = self.service.users().messages().get(userId="me", id=message_id, format="full").execute()
        payload = raw.get("payload", {})
        body_chunks: list[str] = []
        attachments: list[Attachment] = []
        for part in _walk_parts(payload):
            body = part.get("body", {})
            data = body.get("data")
            mime = part.get("mimeType", "")
            if data and mime.startswith("text/"):
                body_chunks.append(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace"))
            if part.get("filename") and body.get("attachmentId"):
                encoded = self.service.users().messages().attachments().get(userId="me", messageId=message_id, id=body["attachmentId"]).execute()["data"]
                attachments.append(Attachment(part["filename"], base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))))
        return GmailMessage(message_id, raw.get("threadId"), raw.get("internalDate"), "\n".join(body_chunks), tuple(attachments))
