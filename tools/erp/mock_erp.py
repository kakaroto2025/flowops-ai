from __future__ import annotations

from shared.models import ERPRecord, Extraction


def build_erp_record(record_id: str, job_id: str, extraction: Extraction) -> ERPRecord:
    if not extraction.invoice_number or not extraction.cnpj or extraction.total_amount is None:
        raise ValueError("Cannot register ERP record with missing invoice fields")
    return ERPRecord(
        id=record_id,
        job_id=job_id,
        document_id=extraction.document_id,
        invoice_number=extraction.invoice_number,
        cnpj=extraction.cnpj,
        total_amount=extraction.total_amount,
    )

