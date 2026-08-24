from __future__ import annotations

from shared.models import ERPRecord, Extraction


def build_erp_record(record_id: str, job_id: str, extraction: Extraction) -> ERPRecord:
    tax_id = extraction.tax_id or extraction.cnpj
    if not extraction.invoice_number or not tax_id or extraction.total_amount is None:
        raise ValueError("Cannot register ERP record with missing invoice fields")
    return ERPRecord(
        id=record_id,
        job_id=job_id,
        document_id=extraction.document_id,
        invoice_number=extraction.invoice_number,
        cnpj=extraction.cnpj or tax_id,
        total_amount=extraction.total_amount,
        company_name=extraction.company_name,
        issue_date=extraction.issue_date,
        country_code=extraction.country_code,
        tax_id=tax_id,
        tax_id_type=extraction.tax_id_type,
        normalized_tax_id=extraction.normalized_tax_id,
        normalized_invoice_number=extraction.normalized_invoice_number,
        currency=extraction.currency,
    )
