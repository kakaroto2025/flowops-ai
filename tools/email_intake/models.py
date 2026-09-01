from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class EmailMessageMetadata:
    provider_message_id: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    received_at: str
    provider_thread_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "recipients": list(self.recipients)}


@dataclass(frozen=True)
class EmailAttachment:
    attachment_id: str
    file_name: str
    content_type: str
    size_bytes: int
    content: bytes

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("content", None)
        return payload


@dataclass(frozen=True)
class NormalizedEmailMessage:
    metadata: EmailMessageMetadata
    attachments: tuple[EmailAttachment, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "attachments": [attachment.to_dict() for attachment in self.attachments],
        }


@dataclass(frozen=True)
class EmailAttachmentResult:
    attachment_id: str
    file_name: str
    status: str
    reason: str | None = None
    job_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EmailIntakeResult:
    provider_message_id: str
    submitted_job_id: str | None
    accepted: tuple[EmailAttachmentResult, ...] = field(default_factory=tuple)
    rejected: tuple[EmailAttachmentResult, ...] = field(default_factory=tuple)
    duplicate: tuple[EmailAttachmentResult, ...] = field(default_factory=tuple)
    dashboard: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_message_id": self.provider_message_id,
            "submitted_job_id": self.submitted_job_id,
            "accepted": [item.to_dict() for item in self.accepted],
            "rejected": [item.to_dict() for item in self.rejected],
            "duplicate": [item.to_dict() for item in self.duplicate],
            "dashboard": self.dashboard,
        }
