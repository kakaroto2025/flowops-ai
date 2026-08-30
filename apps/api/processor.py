from __future__ import annotations

from pathlib import Path

from agents.adk import FlowOpsAdkOrchestrator
from agents.decision import DecisionAgent
from agents.document import DocumentAgent
from agents.intake import IntakeAgent
from agents.reporting import ReportingAgent
from agents.validation import ValidationAgent
from shared.models import AgentEvent, DocumentStatus, Job, PersistenceStore
from shared.models.entities import utc_now
from tools.documents.normalization import normalize_extraction_payload
from tools.finops import CostGuard, UsageTracker


class JobProcessor:
    def __init__(self, store: PersistenceStore):
        self.store = store
        self.intake = IntakeAgent(store)
        self.usage_tracker = UsageTracker(store)
        self.cost_guard = CostGuard(self.usage_tracker)
        self.document_agent = DocumentAgent(store, cost_guard=self.cost_guard)
        self.validation = ValidationAgent(store)
        self.decision = DecisionAgent(store)
        self.reporting = ReportingAgent(store)
        self.adk_orchestrator = FlowOpsAdkOrchestrator(
            store,
            document_agent=self.document_agent,
            validation_agent=self.validation,
            decision_agent=self.decision,
            reporting_agent=self.reporting,
            cost_guard=self.cost_guard,
            usage_tracker=self.usage_tracker,
        )

    def create_demo_job(self, sample_dir: str | Path = "sample_data/alfa_contabilidade", processing_region: str = "AUTO") -> Job:
        files = sorted(Path(sample_dir).glob("*.pdf"))
        if not files:
            raise FileNotFoundError(f"No sample PDFs found in {sample_dir}")
        return self.intake.create_job(files, source="demo_alfa_contabilidade", processing_region=processing_region)

    def create_upload_job(self, files: list[str | Path], processing_region: str = "AUTO") -> Job:
        paths = [Path(file) for file in files]
        if not paths:
            raise ValueError("No uploaded PDFs provided")
        return self.intake.create_job(paths, source="manual_upload", processing_region=processing_region)

    def run_job(self, job_id: str) -> dict:
        return self.adk_orchestrator.run_job(job_id)

    def run_job_legacy(self, job_id: str) -> dict:
        documents = self.store.documents_for_job(job_id)
        for document in documents:
            while document.status in {DocumentStatus.QUEUED, DocumentStatus.RETRY}:
                extraction = self.document_agent.process(document)
                validation = self.validation.validate(document, extraction)
                decision = self.decision.decide(document, extraction, validation)
                document = self.store.documents[document.id]
                if decision != "RETRY":
                    break
        return self.reporting.refresh_job_metrics(job_id)

    def resolve_review(self, review_id: str, corrected_fields: dict, reviewer: str = "demo_user") -> dict:
        review = self.store.human_reviews[review_id]
        if review.status != "OPEN":
            raise ValueError("Review is not open")

        extraction = self.store.extraction_for_document(review.document_id)
        if extraction is None:
            raise ValueError("No extraction found for review document")
        if any(record.document_id == review.document_id for record in self.store.erp_records.values()):
            raise ValueError("Document already registered in ERP")
        document = self.store.documents[review.document_id]

        allowed_fields = {
            "country_code",
            "tax_id",
            "tax_id_type",
            "cnpj",
            "company_name",
            "invoice_number",
            "issue_date",
            "total_amount",
            "currency",
            "document_type",
        }
        for key, value in corrected_fields.items():
            if key == "total_amount" and value not in (None, ""):
                value = float(value)
            if key in allowed_fields and hasattr(extraction, key):
                value = str(value).strip() if isinstance(value, str) else value
                setattr(extraction, key, value)
        if "country_code" not in corrected_fields and extraction.country_code == "UNKNOWN":
            extraction.country_code = None
        normalized = normalize_extraction_payload(
            extraction.to_dict(),
            processing_region=extraction.country_code or document.processing_region,
        )
        for key, value in normalized.items():
            if hasattr(extraction, key):
                setattr(extraction, key, value)
        extraction.confidence = max(extraction.confidence, 0.92)
        extraction.warnings = []
        self.store.add_extraction(extraction)
        self._event(
            review.job_id,
            "HumanReview",
            "HUMAN_REVIEW_CORRECTED",
            "Human review fields were corrected.",
            review.document_id,
            {"review_id": review.id, "fields": sorted(corrected_fields.keys())},
        )

        self.store.update_document(document.id, status=DocumentStatus.VALIDATING)
        validation = self.validation.validate(document, extraction)

        if validation["status"] != "PASS":
            review.reason = ", ".join(validation["errors"] or validation["warnings"]) or "validation_not_passed"
            review.suggested_fields = extraction.to_dict()
            self.store.human_reviews[review.id] = review
            self.store.update_document(document.id, status=DocumentStatus.HUMAN_REVIEW)
            self.store.save()
            dashboard = self.reporting.refresh_job_metrics(review.job_id)
            dashboard["review_resolution"] = {
                "review_id": review_id,
                "decision": "HUMAN_REVIEW_REMAINS_OPEN",
                "validation": validation,
            }
            return dashboard

        document = self.store.documents[review.document_id]
        decision = self.decision.decide(document, extraction, validation)

        review.status = "RESOLVED"
        review.reviewed_by = reviewer
        review.resolved_at = utc_now()
        self.store.human_reviews[review.id] = review
        self.store.save()
        self._event(
            review.job_id,
            "HumanReview",
            "HUMAN_REVIEW_APPROVED",
            "Human review correction approved and sent to ERP.",
            review.document_id,
            {"review_id": review.id, "decision": decision},
        )
        dashboard = self.reporting.refresh_job_metrics(review.job_id)
        dashboard["review_resolution"] = {"review_id": review_id, "decision": decision, "validation": validation}
        return dashboard

    def reject_review(self, review_id: str, reviewer: str = "demo_user") -> dict:
        review = self.store.human_reviews[review_id]
        if review.status != "OPEN":
            raise ValueError("Review is not open")

        self.store.update_document(review.document_id, status=DocumentStatus.REJECTED)
        review.status = "REJECTED"
        review.reviewed_by = reviewer
        review.resolved_at = utc_now()
        self.store.human_reviews[review.id] = review
        self.store.save()
        self._event(
            review.job_id,
            "HumanReview",
            "HUMAN_REVIEW_REJECTED",
            "Human review rejected by operator.",
            review.document_id,
            {"review_id": review.id, "reviewer": reviewer},
        )
        dashboard = self.reporting.refresh_job_metrics(review.job_id)
        dashboard["review_resolution"] = {"review_id": review_id, "decision": "REJECTED"}
        return dashboard

    def _event(
        self,
        job_id: str,
        agent: str,
        event_type: str,
        message: str,
        document_id: str | None = None,
        data: dict | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            id=self.store.next_id("evt"),
            job_id=job_id,
            document_id=document_id,
            agent=agent,
            event_type=event_type,
            message=message,
            data=data or {},
        )
        return self.store.add_event(event)
