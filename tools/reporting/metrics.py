from __future__ import annotations

from collections import Counter
from typing import Any

from shared.models import PersistenceStore


def build_dashboard(store: PersistenceStore, job_id: str) -> dict[str, Any]:
    job = store.jobs[job_id]
    documents = store.documents_for_job(job_id)
    events = store.events_for_job(job_id)
    reviews = store.reviews_for_job(job_id)
    erp_records = store.erp_records_for_job(job_id)
    status_counts = Counter(doc.status for doc in documents)

    processed = sum(
        1
        for doc in documents
        if doc.status in {"COMPLETED", "REGISTERED", "DUPLICATE_BLOCKED", "HUMAN_REVIEW", "FAILED", "REJECTED"}
    )
    success_rate = (len(erp_records) / processed * 100) if processed else 0.0

    document_rows = []
    for doc in documents:
        row = doc.to_dict()
        extraction = store.extraction_for_document(doc.id)
        if extraction:
            row.update(
                {
                    "country_code": extraction.country_code,
                    "tax_id_type": extraction.tax_id_type,
                    "tax_id": extraction.tax_id,
                    "invoice_number": extraction.invoice_number,
                    "currency": extraction.currency,
                }
            )
        document_rows.append(row)

    return {
        "job": job.to_dict(),
        "kpis": {
            "documents_processed": processed,
            "success_rate": round(success_rate, 2),
            "human_reviews": len([review for review in reviews if review.status == "OPEN"]),
            "active_jobs": 1 if job.status == "PROCESSING" else 0,
            "erp_records": len(erp_records),
        },
        "status_counts": dict(status_counts),
        "documents": document_rows,
        "recent_events": [event.to_dict() for event in events[-25:]],
        "human_reviews": [review.to_dict() for review in reviews],
        "erp_records": [record.to_dict() for record in erp_records],
    }


def build_job_history(store: PersistenceStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for job in store.jobs.values():
        documents = store.documents_for_job(job.id)
        reviews = store.reviews_for_job(job.id)
        erp_records = store.erp_records_for_job(job.id)
        rejected = len([doc for doc in documents if doc.status == "REJECTED"])
        rows.append(
            {
                "job_id": job.id,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "document_count": job.document_count,
                "status": job.status,
                "approved": len(erp_records),
                "human_reviews": len([review for review in reviews if review.status == "OPEN"]),
                "human_reviews_total": len(reviews),
                "human_reviews_open": len([review for review in reviews if review.status == "OPEN"]),
                "rejected": rejected,
            }
        )
    return sorted(rows, key=lambda row: row["created_at"], reverse=True)


def build_global_human_review_queue(store: PersistenceStore) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for review in store.open_human_reviews():
        document = store.documents.get(review.document_id)
        rows.append(
            {
                **review.to_dict(),
                "file_name": document.file_name if document else None,
                "document_status": document.status if document else None,
            }
        )
    return rows


def build_global_erp_records(store: PersistenceStore) -> list[dict[str, Any]]:
    return [
        record.to_dict()
        for record in sorted(
            store.erp_records.values(),
            key=lambda record: record.registered_at,
            reverse=True,
        )
    ]


def build_report(store: PersistenceStore, job_id: str) -> dict[str, Any]:
    dashboard = build_dashboard(store, job_id)
    return {
        "job_id": job_id,
        "summary": dashboard["kpis"],
        "documents": dashboard["documents"],
        "erp_records": dashboard["erp_records"],
        "human_reviews": dashboard["human_reviews"],
    }
