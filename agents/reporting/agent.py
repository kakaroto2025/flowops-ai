from __future__ import annotations

from agents.base import BaseAgent
from shared.models import JobStatus
from tools.reporting import build_dashboard, build_report


class ReportingAgent(BaseAgent):
    name = "ReportingAgent"

    def refresh_job_metrics(self, job_id: str) -> dict:
        documents = self.store.documents_for_job(job_id)
        erp_records = self.store.erp_records_for_job(job_id)
        reviews = self.store.reviews_for_job(job_id)
        failed = [doc for doc in documents if doc.status in {"FAILED", "REJECTED"}]
        processed = [
            doc
            for doc in documents
            if doc.status in {"REGISTERED", "HUMAN_REVIEW", "DUPLICATE_BLOCKED", "FAILED", "REJECTED", "COMPLETED"}
        ]
        is_complete = len(processed) == len(documents)
        self.store.update_job(
            job_id,
            status=JobStatus.COMPLETED if is_complete else JobStatus.PROCESSING,
            processed_count=len(processed),
            approved_count=len(erp_records),
            human_review_count=len([review for review in reviews if review.status == "OPEN"]),
            failed_count=len(failed),
        )
        self.event(job_id, "JOB_METRICS_UPDATED", "Updated job metrics.")
        return build_dashboard(self.store, job_id)

    def report(self, job_id: str) -> dict:
        self.event(job_id, "REPORT_GENERATED", "Generated operational report.")
        return build_report(self.store, job_id)
