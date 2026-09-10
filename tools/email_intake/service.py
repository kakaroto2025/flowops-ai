from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable

from shared.models import AgentEvent, AuthContext, Job, PersistenceStore, require_tenant_id
from tools.documents.normalization import normalize_region
from tools.finops import CostGuard

from .models import EmailAttachment, EmailAttachmentResult, EmailIntakeResult, NormalizedEmailMessage
from .providers import EmailIntakeProvider


SubmitFiles = Callable[[list[Path], str], Job]
RunJob = Callable[[str], dict[str, Any]]


class EmailIntakeService:
    def __init__(
        self,
        store: PersistenceStore,
        auth_context: AuthContext,
        cost_guard: CostGuard,
        *,
        submit_files: SubmitFiles,
        run_job: RunJob,
        work_dir: str | Path = "local_data/email_intake",
    ):
        self.store = store
        self.auth_context = auth_context
        self.cost_guard = cost_guard
        self.submit_files = submit_files
        self.run_job = run_job
        self.work_dir = Path(work_dir)
        self._processed_keys: set[tuple[str, str, str]] = set()

    def process_provider(self, provider: EmailIntakeProvider, processing_region: str = "AUTO") -> list[EmailIntakeResult]:
        provider_name = getattr(provider, "provider_name", "email")
        return [
            self.process_message(message, processing_region=processing_region, provider_name=provider_name)
            for message in provider.list_messages()
        ]

    def process_message(
        self,
        message: NormalizedEmailMessage,
        processing_region: str = "AUTO",
        provider_name: str = "email",
    ) -> EmailIntakeResult:
        tenant_id = require_tenant_id(self.auth_context)
        region = normalize_region(processing_region)
        accepted: list[EmailAttachmentResult] = []
        rejected: list[EmailAttachmentResult] = []
        duplicate: list[EmailAttachmentResult] = []
        accepted_files: list[Path] = []
        claimed_receipts: list[tuple[str, EmailAttachment]] = []

        for attachment in message.attachments:
            key = self._idempotency_key(tenant_id, message, attachment)
            if key in self._processed_keys:
                duplicate.append(self._result(attachment, "DUPLICATE_SKIPPED", "email_attachment_already_processed"))
                continue
            rejection_reason = self._rejection_reason(attachment)
            if rejection_reason:
                rejected.append(self._result(attachment, "REJECTED", rejection_reason))
                continue
            receipt_id = self._receipt_id(tenant_id, provider_name, message, attachment)
            receipt = self._receipt_payload(
                tenant_id,
                provider_name,
                message,
                attachment,
                receipt_id,
                status="PROCESSING",
            )
            claimed, existing = self.store.claim_email_intake_receipt(receipt_id, receipt)
            if not claimed:
                duplicate.append(
                    self._result(
                        attachment,
                        "DUPLICATE_SKIPPED",
                        "email_attachment_already_processed",
                    )
                )
                self._processed_keys.add(key)
                continue
            path = self._write_attachment(tenant_id, message, attachment)
            accepted_files.append(path)
            accepted.append(self._result(attachment, "ACCEPTED"))
            claimed_receipts.append((receipt_id, attachment))

        if not accepted_files:
            return EmailIntakeResult(
                provider_message_id=message.metadata.provider_message_id,
                submitted_job_id=None,
                accepted=tuple(accepted),
                rejected=tuple(rejected),
                duplicate=tuple(duplicate),
            )

        try:
            job = self.submit_files(accepted_files, region)
        except Exception as exc:
            self._fail_receipts(claimed_receipts, {"error_type": type(exc).__name__})
            raise
        self._event(
            job.id,
            "EMAIL_RECEIVED",
            "Email message accepted for document intake.",
            data={
                "provider_message_id": message.metadata.provider_message_id,
                "provider_thread_id": message.metadata.provider_thread_id,
                "attachment_count": len(message.attachments),
                "accepted_count": len(accepted),
                "rejected_count": len(rejected),
                "duplicate_count": len(duplicate),
            },
        )
        for attachment_result in accepted:
            self._event(
                job.id,
                "EMAIL_ATTACHMENT_ACCEPTED",
                "Email attachment accepted for processing.",
                data={
                    "provider_message_id": message.metadata.provider_message_id,
                    "attachment_id": attachment_result.attachment_id,
                    "file_name": attachment_result.file_name,
                },
            )
        for attachment_result in rejected:
            self._event(
                job.id,
                "EMAIL_ATTACHMENT_REJECTED",
                "Email attachment rejected before processing.",
                data={
                    "provider_message_id": message.metadata.provider_message_id,
                    "attachment_id": attachment_result.attachment_id,
                    "file_name": attachment_result.file_name,
                    "reason": attachment_result.reason,
                },
            )
        self._event(
            job.id,
            "EMAIL_ATTACHMENT_SUBMITTED",
            "Email attachment submitted to existing document pipeline.",
            data={"provider_message_id": message.metadata.provider_message_id, "submitted_count": len(accepted_files)},
        )

        for attachment in message.attachments:
            if any(item.attachment_id == attachment.attachment_id for item in accepted):
                self._processed_keys.add(self._idempotency_key(tenant_id, message, attachment))

        try:
            dashboard = self.run_job(job.id)
        except Exception as exc:
            self._fail_receipts(claimed_receipts, {"job_id": job.id, "error_type": type(exc).__name__})
            raise
        documents = self.store.documents_for_job(job.id)
        for receipt_id, attachment in claimed_receipts:
            document_id = _document_id_for_attachment(documents, attachment.file_name)
            self.store.complete_email_intake_receipt(
                receipt_id,
                {
                    "job_id": job.id,
                    "document_id": document_id,
                    "completed_at": self.store.jobs[job.id].updated_at,
                },
            )
        accepted_with_job = tuple(
            EmailAttachmentResult(
                attachment_id=item.attachment_id,
                file_name=item.file_name,
                status=item.status,
                reason=item.reason,
                job_id=job.id,
            )
            for item in accepted
        )
        return EmailIntakeResult(
            provider_message_id=message.metadata.provider_message_id,
            submitted_job_id=job.id,
            accepted=accepted_with_job,
            rejected=tuple(rejected),
            duplicate=tuple(duplicate),
            dashboard=dashboard,
        )

    def _rejection_reason(self, attachment: EmailAttachment) -> str | None:
        if attachment.content_type.lower() != "application/pdf" and not attachment.file_name.lower().endswith(".pdf"):
            return "unsupported_attachment_type"
        if attachment.size_bytes > self.cost_guard.config.max_file_size_bytes:
            return "file_size_limit_exceeded"
        return None

    def _write_attachment(self, tenant_id: str, message: NormalizedEmailMessage, attachment: EmailAttachment) -> Path:
        safe_message_id = _safe_component(message.metadata.provider_message_id)
        safe_attachment_id = _safe_component(attachment.attachment_id)
        safe_name = Path(attachment.file_name).name or "attachment.pdf"
        target_dir = self.work_dir / tenant_id / safe_message_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_attachment_id}_{safe_name}"
        target.write_bytes(attachment.content)
        return target

    def _idempotency_key(
        self,
        tenant_id: str,
        message: NormalizedEmailMessage,
        attachment: EmailAttachment,
    ) -> tuple[str, str, str]:
        return (tenant_id, message.metadata.provider_message_id, attachment.attachment_id)

    def _receipt_id(
        self,
        tenant_id: str,
        provider_name: str,
        message: NormalizedEmailMessage,
        attachment: EmailAttachment,
    ) -> str:
        raw = "|".join((tenant_id, provider_name, message.metadata.provider_message_id, attachment.attachment_id))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _receipt_payload(
        self,
        tenant_id: str,
        provider_name: str,
        message: NormalizedEmailMessage,
        attachment: EmailAttachment,
        receipt_id: str,
        status: str,
    ) -> dict[str, Any]:
        return {
            "id": receipt_id,
            "tenant_id": tenant_id,
            "provider": provider_name,
            "provider_message_id": message.metadata.provider_message_id,
            "attachment_id": attachment.attachment_id,
            "file_name": attachment.file_name,
            "status": status,
            "job_id": None,
            "document_id": None,
            "created_at": utc_timestamp(),
            "completed_at": None,
        }

    def _fail_receipts(self, receipts: list[tuple[str, EmailAttachment]], changes: dict[str, Any]) -> None:
        for receipt_id, _attachment in receipts:
            self.store.fail_email_intake_receipt(receipt_id, changes)

    def _result(self, attachment: EmailAttachment, status: str, reason: str | None = None) -> EmailAttachmentResult:
        return EmailAttachmentResult(
            attachment_id=attachment.attachment_id,
            file_name=attachment.file_name,
            status=status,
            reason=reason,
        )

    def _event(
        self,
        job_id: str,
        event_type: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            id=self.store.next_id("evt"),
            job_id=job_id,
            agent="EmailIntakeService",
            event_type=event_type,
            message=message,
            tenant_id=self.store.jobs[job_id].tenant_id,
            data=data or {},
        )
        return self.store.add_event(event)


def _safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return sanitized[:80] or "unknown"


def _document_id_for_attachment(documents: list[Any], file_name: str) -> str | None:
    for document in documents:
        if document.file_name == file_name:
            return document.id
    return documents[0].id if len(documents) == 1 else None


def utc_timestamp() -> str:
    from shared.models.entities import utc_now

    return utc_now()
