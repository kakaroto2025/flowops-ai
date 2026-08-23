from __future__ import annotations

from agents.base import BaseAgent
from shared.models import Document, DocumentStatus, Extraction
from tools.validation import validate_extraction


class ValidationAgent(BaseAgent):
    name = "ValidationAgent"

    def validate(self, document: Document, extraction: Extraction) -> dict:
        self.store.update_document(document.id, status=DocumentStatus.VALIDATING)
        known = {
            (record.cnpj, record.invoice_number)
            for record in self.store.erp_records.values()
            if record.job_id == document.job_id
        }
        result = validate_extraction(extraction.to_dict(), known_invoices=known)
        self.event(
            document.job_id,
            "VALIDATION_COMPLETED",
            f"Validation status: {result['status']}.",
            document.id,
            result,
        )
        return result

