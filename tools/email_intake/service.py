from __future__ import annotations

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
        return [self.process_message(message, processing_region=processing_region) for message in provider.list_messages()]

    def process_message(self, message: NormalizedEmailMessage, processing_region: str = "AUTO") -> EmailIntakeResult:
        tenant_id = require_tenant_id(self.auth_context)
        region = normalize_region(processing_region)
        accepted: list[EmailAttachmentResult] = []
        rejected: list[EmailAttachmentResult] = []
        duplicate: list[EmailAttachmentResult] = []
        accepted_files: list[Path] = []

        for attachment in message.attachments:
            key = self._idempotency_key(tenant_id, message, attachment)
            if key in self._processed_keys:
                duplicate.append(self._result(attachment, "DUPLICATE_SKIPPED", "email_attachment_already_processed"))
                continue
            rejection_reason = self._rejection_reason(attachment)
            if rejection_reason:
                rejected.append(self._result(attachment, "REJECTED", rejection_reason))
                continue
            path = self._write_attachment(tenant_id, message, attachment)
            accepted_files.append(path)
            accepted.append(self._result(attachment, "ACCEPTED"))

        if not accepted_files:
            return EmailIntakeResult(
                provider_message_id=message.metadata.provider_message_id,
                submitted_job_id=None,
                accepted=tuple(accepted),
                rejected=tuple(rejected),
                duplicate=tuple(duplicate),
            )

        job = self.submit_files(accepted_files, region)
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

        dashboard = self.run_job(job.id)
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
