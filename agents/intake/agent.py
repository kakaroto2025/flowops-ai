from __future__ import annotations

from pathlib import Path

from agents.base import BaseAgent
from shared.models import AuthContext, Document, DocumentStatus, Job, JobStatus, require_tenant_id
from tools.documents.normalization import normalize_region


class IntakeAgent(BaseAgent):
    name = "IntakeAgent"

    def create_job(
        self,
        files: list[Path],
        source: str = "manual_upload",
        processing_region: str = "AUTO",
        auth_context: AuthContext | None = None,
    ) -> Job:
        tenant_id = require_tenant_id(auth_context)
        region = normalize_region(processing_region)
        job = self.store.add_job(
            Job(
                id=self.store.next_id("job"),
                source=source,
                status=JobStatus.CREATED,
                tenant_id=tenant_id,
                user_id=auth_context.user_id,
                processing_region=region,
                document_count=len(files),
            )
        )
        self.event(job.id, "JOB_CREATED", f"Created job with {len(files)} document(s).", data={"processing_region": region})

        for file_path in files:
            document = Document(
                id=self.store.next_id("doc"),
                job_id=job.id,
                file_name=file_path.name,
                storage_path=str(file_path),
                tenant_id=tenant_id,
                status=DocumentStatus.QUEUED,
                processing_region=region,
            )
            self.store.add_document(document)
            object_uri = self.store.store_document_bytes(document, file_path.read_bytes(), _content_type(file_path))
            self.event(
                job.id,
                "DOCUMENT_QUEUED",
                f"Queued document {file_path.name}.",
                document_id=document.id,
            )
            self.event(
                job.id,
                "DOCUMENT_STORED",
                "Stored document through persistence backend.",
                document_id=document.id,
                data={"object_uri": object_uri},
            )

        self.store.update_job(job.id, status=JobStatus.PROCESSING)
        return self.store.jobs[job.id]


def _content_type(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return "application/pdf"
    return "application/octet-stream"
