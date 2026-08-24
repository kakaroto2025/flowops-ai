from __future__ import annotations

from agents.base import BaseAgent
from shared.models import Document, DocumentStatus, Extraction, HumanReview
from tools.documents.normalization import business_key
from tools.erp import build_erp_record


class DecisionAgent(BaseAgent):
    name = "DecisionAgent"

    def _find_duplicate(self, extraction: Extraction):
        return self.store.find_registered_invoice(
            extraction.cnpj,
            extraction.invoice_number,
            country_code=extraction.country_code,
            tax_id=extraction.tax_id,
            normalized_tax_id=extraction.normalized_tax_id,
            normalized_invoice_number=extraction.normalized_invoice_number,
        )

    def _duplicate_event_data(self, extraction: Extraction, duplicate) -> dict:
        return {
            "duplicate_key": {
                "business_key": business_key(extraction),
                "country_code": extraction.country_code,
                "tax_id_type": extraction.tax_id_type,
                "tax_id": extraction.tax_id,
                "cnpj": extraction.cnpj,
                "invoice_number": extraction.invoice_number,
            },
            "original_job_id": duplicate.job_id,
            "original_document_id": duplicate.document_id,
            "original_erp_record_id": duplicate.id,
            "original_total_amount": duplicate.total_amount,
            "current_total_amount": extraction.total_amount,
        }

    def decide(self, document: Document, extraction: Extraction, validation: dict) -> str:
        if validation["retry_recommended"] and document.retry_count < 1:
            self.store.update_document(
                document.id,
                status=DocumentStatus.RETRY,
                retry_count=document.retry_count + 1,
            )
            self.event(
                document.job_id,
                "DECISION_RETRY",
                "Recoverable anomaly detected. Retrying extraction.",
                document.id,
                validation,
            )
            return "RETRY"

        if "duplicate_invoice" in validation.get("errors", []):
            duplicate = self._find_duplicate(extraction)
            if duplicate:
                self.store.update_document(document.id, status=DocumentStatus.DUPLICATE_BLOCKED)
                self.event(
                    document.job_id,
                    "DUPLICATE_DETECTED",
                    "Duplicate invoice detected before mock ERP registration.",
                    document.id,
                    self._duplicate_event_data(extraction, duplicate),
                )
                return "DUPLICATE_BLOCKED"

        if validation["status"] == "FAIL":
            review = HumanReview(
                id=self.store.next_id("review"),
                job_id=document.job_id,
                document_id=document.id,
                reason=", ".join(validation["errors"]) or "validation_failed",
                suggested_fields=extraction.to_dict(),
            )
            self.store.add_human_review(review)
            self.store.update_document(document.id, status=DocumentStatus.HUMAN_REVIEW)
            self.event(
                document.job_id,
                "DECISION_HUMAN_REVIEW",
                f"Document requires human review: {review.reason}.",
                document.id,
                {"review_id": review.id},
            )
            return "HUMAN_REVIEW_REQUIRED"

        duplicate = self._find_duplicate(extraction)
        if duplicate:
            self.store.update_document(document.id, status=DocumentStatus.DUPLICATE_BLOCKED)
            self.event(
                document.job_id,
                "DUPLICATE_DETECTED",
                "Duplicate invoice detected before mock ERP registration.",
                document.id,
                self._duplicate_event_data(extraction, duplicate),
            )
            return "DUPLICATE_BLOCKED"

        record = build_erp_record(self.store.next_id("erp"), document.job_id, extraction)
        self.store.add_erp_record(record)
        self.store.update_document(document.id, status=DocumentStatus.REGISTERED)
        self.event(
            document.job_id,
            "DECISION_APPROVED",
            "Document approved and registered in mock ERP.",
            document.id,
            {"erp_record_id": record.id},
        )
        return "APPROVED"
