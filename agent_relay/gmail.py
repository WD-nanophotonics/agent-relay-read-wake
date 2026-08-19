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
        self.request_timeout = 25
        # google-api-python-client delegates transport to an httplib2-like
        # object. Set its socket timeout when available; parser validation
        # remains the final authorization boundary.
        for transport in (getattr(self.service, "_http", None), getattr(getattr(self.service, "_http", None), "http", None)):
            if transport is not None and hasattr(transport, "timeout"):
                transport.timeout = self.request_timeout

    def test_connection(self) -> None:
        self.service.users().getProfile(userId="me").execute(num_retries=0)

    def list_messages(self) -> list[str]:
        # Coarse candidate filter only. Exact protocol parsing below still
        # validates the full v1/v2 control envelope and decision attachment.
        ids: list[str] = []
        token = None
        for _ in range(10):
            params = {"userId": "me", "q": 'in:inbox {subject:"[AGENTRELAY]" subject:"AGENTRELAY/1" subject:"AGENTRELAY/2"}', "maxResults": 100}
            if token:
                params["pageToken"] = token
            response = self.service.users().messages().list(**params).execute(num_retries=0)
            ids.extend(item["id"] for item in response.get("messages", []) if item.get("id"))
            token = response.get("nextPageToken")
            if not token:
                break
        return ids

    def fetch_message(self, message_id: str) -> GmailMessage:
        raw = self.service.users().messages().get(userId="me", id=message_id, format="full").execute(num_retries=0)
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
                encoded = self.service.users().messages().attachments().get(userId="me", messageId=message_id, id=body["attachmentId"]).execute(num_retries=0)["data"]
                attachments.append(Attachment(part["filename"], base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))))
        return GmailMessage(message_id, raw.get("threadId"), raw.get("internalDate"), "\n".join(body_chunks), tuple(attachments))
