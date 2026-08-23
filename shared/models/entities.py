from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(StrEnum):
    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DocumentStatus(StrEnum):
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    EXTRACTED = "EXTRACTED"
    VALIDATING = "VALIDATING"
    RETRY = "RETRY"
    APPROVED = "APPROVED"
    REGISTERED = "REGISTERED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


@dataclass
class Job:
    id: str
    source: str
    status: str = JobStatus.CREATED
    document_count: int = 0
    processed_count: int = 0
    approved_count: int = 0
    human_review_count: int = 0
    failed_count: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Document:
    id: str
    job_id: str
    file_name: str
    storage_path: str
    status: str = DocumentStatus.UPLOADED
    retry_count: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Extraction:
    id: str
    document_id: str
    document_type: str
    cnpj: str | None
    company_name: str | None
    invoice_number: str | None
    issue_date: str | None
    total_amount: float | None
    confidence: float
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentEvent:
    id: str
    job_id: str
    agent: str
    event_type: str
    message: str
    document_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HumanReview:
    id: str
    job_id: str
    document_id: str
    reason: str
    status: str = "OPEN"
    suggested_fields: dict[str, Any] = field(default_factory=dict)
    reviewed_by: str | None = None
    created_at: str = field(default_factory=utc_now)
    resolved_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ERPRecord:
    id: str
    job_id: str
    document_id: str
    invoice_number: str
    cnpj: str
    total_amount: float
    status: str = "REGISTERED"
    registered_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
