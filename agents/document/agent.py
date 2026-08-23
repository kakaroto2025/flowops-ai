from __future__ import annotations

from agents.base import BaseAgent
from shared.models import Document, DocumentStatus, Extraction
from tools.documents import GeminiExtractionError, extract_document, extract_with_gemini
from tools.documents.extractor import read_document_text


class DocumentAgent(BaseAgent):
    name = "DocumentAgent"

    def process(self, document: Document) -> Extraction:
        self.store.update_document(document.id, status=DocumentStatus.PROCESSING)
        self.event(document.job_id, "EXTRACTION_STARTED", "Started document extraction.", document.id)

        raw_text = read_document_text(document.storage_path)
        self.event(
            document.job_id,
            "PDF_TEXT_EXTRACTED",
            f"PDF_TEXT_LENGTH={len(raw_text)}",
            document.id,
            {
                "storage_path": document.storage_path,
                "pdf_text_length": len(raw_text),
                "pdf_text_preview": raw_text[:500],
            },
        )

        extraction_method = "GEMINI_EXTRACTION"
        try:
            result = extract_with_gemini(raw_text)
            self.event(
                document.job_id,
                "GEMINI_EXTRACTION",
                "Extracted fields with Gemini.",
                document.id,
                {
                    "model": "gemini-3.6-flash",
                    "warnings": result.get("warnings", []),
                },
            )
        except GeminiExtractionError as exc:
            extraction_method = "LOCAL_PARSER_FALLBACK"
            result = extract_document(document.storage_path, retry_count=document.retry_count)
            error_details = dict(getattr(exc, "details", {}) or {})
            error_details["reason"] = str(exc)
            error_details["warnings"] = result.get("warnings", [])
            self.event(
                document.job_id,
                "LOCAL_PARSER_FALLBACK",
                "Gemini extraction unavailable; used local parser.",
                document.id,
                error_details,
            )
        extraction = Extraction(
            id=self.store.next_id("ext"),
            document_id=document.id,
            document_type=result["document_type"],
            cnpj=result["cnpj"],
            company_name=result["company_name"],
            invoice_number=result["invoice_number"],
            issue_date=result["issue_date"],
            total_amount=result["total_amount"],
            confidence=result["confidence"],
            warnings=result["warnings"],
        )
        self.store.add_extraction(extraction)
        self.store.update_document(document.id, status=DocumentStatus.EXTRACTED)
        self.event(
            document.job_id,
            "EXTRACTION_COMPLETED",
            f"Extracted fields with confidence {extraction.confidence:.2f}.",
            document.id,
            {"extraction_id": extraction.id, "warnings": extraction.warnings, "method": extraction_method},
        )
        return extraction
