# FlowOps AI Data Model v1.0

## jobs

```json
{
  "id": "job_000001",
  "created_by": "user_001",
  "status": "PROCESSING",
  "source": "manual_upload",
  "processing_region": "AUTO",
  "document_count": 500,
  "processed_count": 137,
  "approved_count": 120,
  "human_review_count": 10,
  "failed_count": 7,
  "created_at": "2026-08-11T12:00:00Z",
  "updated_at": "2026-08-11T12:05:00Z"
}
```

## documents

```json
{
  "id": "doc_001",
  "job_id": "job_000001",
  "file_name": "NF001.pdf",
  "storage_uri": "gs://flowops-documents/job_000001/NF001.pdf",
  "status": "VALIDATING",
  "processing_region": "AUTO",
  "retry_count": 0,
  "created_at": "2026-08-11T12:00:01Z",
  "updated_at": "2026-08-11T12:00:33Z"
}
```

## extractions

```json
{
  "id": "ext_001",
  "document_id": "doc_001",
  "document_type": "INVOICE",
  "country_code": "BR",
  "country_confidence": 0.99,
  "tax_id": "12.345.678/0001-90",
  "tax_id_type": "CNPJ",
  "normalized_tax_id": "12345678000190",
  "normalized_invoice_number": "98342",
  "cnpj": "12.345.678/0001-90",
  "company_name": "Empresa Alfa Ltda",
  "invoice_number": "98342",
  "issue_date": "2026-08-11",
  "total_amount": 18750.00,
  "currency": "BRL",
  "confidence": 0.94,
  "warnings": [],
  "created_at": "2026-08-11T12:00:30Z"
}
```

## agent_events

```json
{
  "id": "evt_001",
  "job_id": "job_000001",
  "document_id": "doc_001",
  "agent": "ValidationAgent",
  "event_type": "VALIDATION_COMPLETED",
  "message": "CNPJ, date, amount and required fields passed.",
  "data": {
    "checks_passed": ["cnpj", "date", "currency", "required_fields"]
  },
  "created_at": "2026-08-11T12:00:34Z"
}
```

## human_reviews

```json
{
  "id": "review_001",
  "document_id": "doc_009",
  "job_id": "job_000001",
  "reason": "Low confidence total_amount extraction",
  "status": "OPEN",
  "suggested_fields": {
    "total_amount": 18750.00
  },
  "reviewed_by": null,
  "created_at": "2026-08-11T12:03:00Z",
  "resolved_at": null
}
```

## erp_records

```json
{
  "id": "erp_001",
  "document_id": "doc_001",
  "invoice_number": "98342",
  "cnpj": "12.345.678/0001-90",
  "company_name": "Empresa Alfa Ltda",
  "issue_date": "2026-08-11",
  "country_code": "BR",
  "tax_id": "12.345.678/0001-90",
  "tax_id_type": "CNPJ",
  "normalized_tax_id": "12345678000190",
  "normalized_invoice_number": "98342",
  "currency": "BRL",
  "total_amount": 18750.00,
  "registered_at": "2026-08-11T12:00:40Z",
  "status": "REGISTERED"
}
```

## Universal Business Key

Duplicate detection uses the normalized multi-country identity:

```text
country_code + normalized_tax_id + normalized_invoice_number
```

Legacy records that only contain `cnpj` are interpreted as `BR/CNPJ` during store loading and duplicate lookup.
