from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import EmailAttachment, EmailMessageMetadata, NormalizedEmailMessage
from .providers import EmailIntakeProvider


class GmailAdapterError(RuntimeError):
    pass


class GmailMessageNotFoundError(GmailAdapterError):
    pass


class GmailMalformedPayloadError(GmailAdapterError):
    pass


class GmailAttachmentFetchError(GmailAdapterError):
    pass


class GmailClient(Protocol):
    def fetch_message(self, message_id: str) -> dict[str, Any] | None: ...
    def fetch_attachment(self, message_id: str, attachment_id: str) -> dict[str, Any] | None: ...


class GmailEmailIntakeProvider(EmailIntakeProvider):
    def __init__(self, client: GmailClient, message_ids: list[str]):
        self.client = client
        self.message_ids = list(message_ids)

    def list_messages(self) -> list[NormalizedEmailMessage]:
        return [self.fetch_message(message_id) for message_id in self.message_ids]

    def fetch_message(self, message_id: str) -> NormalizedEmailMessage:
        message = self.client.fetch_message(message_id)
        if message is None:
            raise GmailMessageNotFoundError(f"Gmail message not found: {message_id}")
        return self._normalize_message(message)

    def _normalize_message(self, message: dict[str, Any]) -> NormalizedEmailMessage:
        provider_message_id = str(message.get("id") or "").strip()
        if not provider_message_id:
            raise GmailMalformedPayloadError("Gmail message is missing id.")

        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise GmailMalformedPayloadError("Gmail message payload is malformed.")

        headers = _headers(payload.get("headers", []))
        metadata = EmailMessageMetadata(
            provider_message_id=provider_message_id,
            provider_thread_id=str(message.get("threadId") or "").strip() or None,
            sender=headers.get("from", ""),
            recipients=tuple(_split_recipients(headers.get("to", ""))),
            subject=headers.get("subject", ""),
            received_at=headers.get("date") or _internal_date(message.get("internalDate")),
        )

        attachments = tuple(self._attachment_from_part(provider_message_id, part) for part in _walk_parts(payload) if _is_attachment(part))
        return NormalizedEmailMessage(metadata=metadata, attachments=attachments)

    def _attachment_from_part(self, provider_message_id: str, part: dict[str, Any]) -> EmailAttachment:
        file_name = str(part.get("filename") or "").strip()
        content_type = str(part.get("mimeType") or "application/octet-stream").strip() or "application/octet-stream"
        body = part.get("body")
        if not isinstance(body, dict):
            raise GmailMalformedPayloadError("Gmail attachment body is malformed.")

        attachment_id = str(body.get("attachmentId") or "").strip()
        encoded_data = body.get("data")
        if attachment_id:
            fetched = self.client.fetch_attachment(provider_message_id, attachment_id)
            if fetched is None:
                raise GmailAttachmentFetchError(f"Gmail attachment fetch failed: {attachment_id}")
            if not isinstance(fetched, dict) or "data" not in fetched:
                raise GmailAttachmentFetchError(f"Gmail attachment payload is malformed: {attachment_id}")
            encoded_data = fetched["data"]
        elif not encoded_data:
            raise GmailMalformedPayloadError("Gmail attachment is missing attachmentId or inline data.")

        content = _decode_base64url(encoded_data)
        size_bytes = int(body.get("size") or len(content))
        return EmailAttachment(
            attachment_id=attachment_id or _inline_attachment_id(part),
            file_name=file_name,
            content_type=content_type,
            size_bytes=size_bytes,
            content=content,
        )


def _headers(raw_headers: Any) -> dict[str, str]:
    if not isinstance(raw_headers, list):
        return {}
    parsed: dict[str, str] = {}
    for item in raw_headers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        if name:
            parsed[name] = str(item.get("value") or "")
    return parsed


def _split_recipients(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _internal_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        timestamp = int(value) / 1000
    except (TypeError, ValueError):
        return str(value)
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _walk_parts(part: dict[str, Any]):
    yield part
    children = part.get("parts", [])
    if not isinstance(children, list):
        return
    for child in children:
        if isinstance(child, dict):
            yield from _walk_parts(child)


def _is_attachment(part: dict[str, Any]) -> bool:
    filename = str(part.get("filename") or "").strip()
    body = part.get("body")
    if not filename or not isinstance(body, dict):
        return False
    return bool(body.get("attachmentId") or body.get("data"))


def _decode_base64url(value: Any) -> bytes:
    if not isinstance(value, str) or not value:
        raise GmailMalformedPayloadError("Gmail attachment data is empty or invalid.")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except Exception as exc:
        raise GmailMalformedPayloadError("Gmail attachment data is not valid base64url.") from exc


def _inline_attachment_id(part: dict[str, Any]) -> str:
    part_id = str(part.get("partId") or "").strip()
    filename = str(part.get("filename") or "inline").strip()
    return f"inline:{part_id}:{filename}"
