from __future__ import annotations

from pathlib import Path

from agents.base import BaseAgent
from shared.models import Document, DocumentStatus, Job, JobStatus
from tools.documents.normalization import normalize_region


class IntakeAgent(BaseAgent):
    name = "IntakeAgent"

    def create_job(self, files: list[Path], source: str = "manual_upload", processing_region: str = "AUTO") -> Job:
        region = normalize_region(processing_region)
        job = self.store.add_job(
            Job(
                id=self.store.next_id("job"),
                source=source,
                status=JobStatus.CREATED,
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
                status=DocumentStatus.QUEUED,
                processing_region=region,
            )
            self.store.add_document(document)
            self.event(
                job.id,
                "DOCUMENT_QUEUED",
                f"Queued document {file_path.name}.",
                document_id=document.id,
            )

        self.store.update_job(job.id, status=JobStatus.PROCESSING)
        return self.store.jobs[job.id]
