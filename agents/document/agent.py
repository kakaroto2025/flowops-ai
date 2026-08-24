from __future__ import annotations

from agents.base import BaseAgent
from shared.models import Document, DocumentStatus, Extraction
from tools.documents import GeminiExtractionError, extract_document, extract_with_gemini
from tools.documents.extractor import read_document_text
from tools.documents.normalization import normalize_extraction_payload, normalize_region


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

        processing_region = normalize_region(document.processing_region)
        if processing_region in {"BR", "US"}:
            self.event(
                document.job_id,
                "COUNTRY_SELECTED",
                f"Processing region selected: {processing_region}.",
                document.id,
                {"country_code": processing_region, "method": "USER_SELECTED"},
            )

        extraction_method = "GEMINI_EXTRACTION"
        try:
            result = extract_with_gemini(raw_text, processing_region=processing_region)
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
            result = extract_document(document.storage_path, retry_count=document.retry_count, processing_region=processing_region)
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
        result = normalize_extraction_payload(result, raw_text=raw_text, processing_region=processing_region)
        self.event(
            document.job_id,
            "COUNTRY_DETECTED",
            f"Country resolved: {result.get('country_code')}.",
            document.id,
            {
                "country_code": result.get("country_code"),
                "confidence": result.get("country_confidence"),
                "method": result.get("country_detection_method"),
            },
        )
        self.event(
            document.job_id,
            "DOCUMENT_NORMALIZED",
            "Document fields normalized to universal schema.",
            document.id,
            {
                "country_code": result.get("country_code"),
                "tax_id_type": result.get("tax_id_type"),
                "currency": result.get("currency"),
            },
        )
        extraction = Extraction(
            id=self.store.next_id("ext"),
            document_id=document.id,
            document_type=result.get("document_type") or "INVOICE",
            cnpj=result.get("cnpj"),
            company_name=result.get("company_name"),
            invoice_number=result.get("invoice_number"),
            issue_date=result.get("issue_date"),
            total_amount=result.get("total_amount"),
            confidence=result.get("confidence") or 0.0,
            country_code=result.get("country_code"),
            country_confidence=result.get("country_confidence") or 0.0,
            tax_id=result.get("tax_id"),
            tax_id_type=result.get("tax_id_type"),
            normalized_tax_id=result.get("normalized_tax_id"),
            normalized_invoice_number=result.get("normalized_invoice_number"),
            currency=result.get("currency"),
            warnings=result.get("warnings") or [],
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
