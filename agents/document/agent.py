from __future__ import annotations

from agents.base import BaseAgent
from shared.models import Document, DocumentStatus, Extraction
from tools.finops import CostGuard
from tools.finops.pricing import GeminiPricing
from tools.documents import GeminiExtractionError, extract_document, extract_with_gemini
from tools.documents.extractor import read_document_text
from tools.documents.normalization import normalize_extraction_payload, normalize_region


class DocumentAgent(BaseAgent):
    name = "DocumentAgent"

    def __init__(self, store, cost_guard: CostGuard | None = None):
        super().__init__(store)
        self.cost_guard = cost_guard
        self.last_finops_usage: dict[str, dict] = {}

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
        gemini_usage_metadata: dict = {}
        gemini_calls = 0
        gemini_block_reason = None
        warning_reason = None
        file_size_bytes = self._file_size_bytes(document.storage_path)
        try:
            gemini_guard = self.cost_guard.can_call_gemini() if self.cost_guard else None
            if gemini_guard and not gemini_guard.allowed:
                raise GeminiExtractionError(gemini_guard.reason or "gemini_blocked_by_cost_guard")
            if gemini_guard and gemini_guard.reason:
                warning_reason = gemini_guard.reason
                self.event(
                    document.job_id,
                    "FINOPS_WARNING",
                    "Cost Guard warning before Gemini extraction.",
                    document.id,
                    gemini_guard.to_dict(),
                )
            gemini_calls = 1
            result = extract_with_gemini(raw_text, processing_region=processing_region)
            gemini_usage_metadata = dict(result.pop("gemini_usage_metadata", {}) or {})
            self.event(
                document.job_id,
                "GEMINI_EXTRACTION",
                "Extracted fields with Gemini.",
                document.id,
                {
                    "model": "gemini-3.6-flash",
                    "warnings": result.get("warnings", []),
                    "usage_metadata_available": bool(gemini_usage_metadata),
                    "input_tokens": gemini_usage_metadata.get("input_tokens"),
                    "output_tokens": gemini_usage_metadata.get("output_tokens"),
                    "total_tokens": gemini_usage_metadata.get("total_tokens"),
                },
            )
            self.event(
                document.job_id,
                "GEMINI_USAGE_RECORDED",
                "Gemini usage metadata captured.",
                document.id,
                {
                    "model": "gemini-3.6-flash",
                    "input_tokens": gemini_usage_metadata.get("input_tokens"),
                    "output_tokens": gemini_usage_metadata.get("output_tokens"),
                    "total_tokens": gemini_usage_metadata.get("total_tokens"),
                    "usage_metadata_available": bool(gemini_usage_metadata),
                },
            )
        except GeminiExtractionError as exc:
            extraction_method = "LOCAL_PARSER_FALLBACK"
            result = extract_document(document.storage_path, retry_count=document.retry_count, processing_region=processing_region)
            error_details = dict(getattr(exc, "details", {}) or {})
            error_details["reason"] = str(exc)
            error_details["warnings"] = result.get("warnings", [])
            if str(exc) in {"daily_gemini_limit_exceeded", "monthly_soft_budget_reached"}:
                gemini_block_reason = str(exc)
                self.event(
                    document.job_id,
                    "FINOPS_BLOCK",
                    "Cost Guard blocked Gemini; local parser remained available.",
                    document.id,
                    {"block_reason": gemini_block_reason},
                )
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
            tenant_id=document.tenant_id,
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
        pricing = GeminiPricing.from_env("gemini-3.6-flash")
        estimated_usd = pricing.estimate_usd(
            gemini_usage_metadata.get("input_tokens"),
            gemini_usage_metadata.get("output_tokens"),
        )
        estimated_brl = None
        if estimated_usd is not None and self.cost_guard and self.cost_guard.config.usd_brl_rate is not None:
            estimated_brl = round(estimated_usd * self.cost_guard.config.usd_brl_rate, 8)
        self.last_finops_usage[document.id] = {
            "document_type": extraction.document_type,
            "country": extraction.country_code,
            "file_size_bytes": file_size_bytes,
            "gemini_used": extraction_method == "GEMINI_EXTRACTION",
            "gemini_model": "gemini-3.6-flash" if extraction_method == "GEMINI_EXTRACTION" else None,
            "gemini_calls": gemini_calls,
            "input_tokens": gemini_usage_metadata.get("input_tokens"),
            "output_tokens": gemini_usage_metadata.get("output_tokens"),
            "total_tokens": gemini_usage_metadata.get("total_tokens"),
            "estimated_ai_cost_usd": estimated_usd,
            "estimated_ai_cost_brl": estimated_brl,
            "parser_fallback_used": extraction_method == "LOCAL_PARSER_FALLBACK",
            "blocked_by_cost_guard": bool(gemini_block_reason),
            "block_reason": gemini_block_reason,
            "warning_reason": warning_reason,
        }
        self.event(
            document.job_id,
            "EXTRACTION_COMPLETED",
            f"Extracted fields with confidence {extraction.confidence:.2f}.",
            document.id,
            {"extraction_id": extraction.id, "warnings": extraction.warnings, "method": extraction_method},
        )
        return extraction

    def _file_size_bytes(self, path: str) -> int | None:
        try:
            from pathlib import Path

            return Path(path).stat().st_size
        except OSError:
            return None
