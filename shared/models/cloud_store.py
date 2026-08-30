from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Protocol

from tools.documents.normalization import business_key, normalize_extraction_payload
from tools.finops.models import UsageRecord

from .entities import AgentEvent, Document, ERPRecord, Extraction, HumanReview, Job, utc_now
from .persistence import PersistenceConfigurationError, PersistenceStore


@dataclass(frozen=True)
class CloudStoreConfig:
    project_id: str
    firestore_database: str
    storage_bucket: str

    @classmethod
    def from_env(cls) -> "CloudStoreConfig":
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        firestore_database = os.environ.get("FIRESTORE_DATABASE", "").strip()
        storage_bucket = os.environ.get("FLOWOPS_STORAGE_BUCKET", "").strip()
        missing = [
            name
            for name, value in (
                ("GOOGLE_CLOUD_PROJECT", project_id),
                ("FIRESTORE_DATABASE", firestore_database),
                ("FLOWOPS_STORAGE_BUCKET", storage_bucket),
            )
            if not value
        ]
        if missing:
            raise PersistenceConfigurationError(
                "STORAGE_MODE=cloud requires configuration: " + ", ".join(missing)
            )
        return cls(project_id=project_id, firestore_database=firestore_database, storage_bucket=storage_bucket)


class FirestoreBackend(Protocol):
    def upsert(self, collection: str, document_id: str, payload: dict[str, Any]) -> None: ...
    def delete_collection(self, collection: str) -> None: ...


class ObjectStorageBackend(Protocol):
    def object_path(self, document: Document) -> str: ...
    def upload_bytes(self, object_path: str, content: bytes, content_type: str) -> str: ...


class FirestoreRepository:
    def __init__(self, config: CloudStoreConfig):
        try:
            from google.cloud import firestore
        except ImportError as exc:
            raise PersistenceConfigurationError(
                "STORAGE_MODE=cloud requires google-cloud-firestore to be installed."
            ) from exc
        self.client = firestore.Client(project=config.project_id, database=config.firestore_database)

    def upsert(self, collection: str, document_id: str, payload: dict[str, Any]) -> None:
        self.client.collection(collection).document(document_id).set(payload)

    def delete_collection(self, collection: str) -> None:
        for document in self.client.collection(collection).stream():
            document.reference.delete()


class CloudStorageRepository:
    def __init__(self, config: CloudStoreConfig):
        try:
            from google.cloud import storage
        except ImportError as exc:
            raise PersistenceConfigurationError(
                "STORAGE_MODE=cloud requires google-cloud-storage to be installed."
            ) from exc
        self.bucket = storage.Client(project=config.project_id).bucket(config.storage_bucket)

    def object_path(self, document: Document) -> str:
        safe_name = Path(document.file_name).name
        return f"documents/default/{document.id}/{safe_name}"

    def upload_bytes(self, object_path: str, content: bytes, content_type: str) -> str:
        blob = self.bucket.blob(object_path)
        blob.upload_from_string(content, content_type=content_type)
        return f"gs://{self.bucket.name}/{object_path}"


def _filter_dataclass_payload(model: type, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {field.name for field in fields(model)}
    return {key: value for key, value in payload.items() if key in allowed}


class CloudStore(PersistenceStore):
    COLLECTIONS = {
        "counters": "flowops_counters",
        "jobs": "flowops_jobs",
        "documents": "flowops_documents",
        "extractions": "flowops_extractions",
        "events": "flowops_events",
        "human_reviews": "flowops_human_reviews",
        "erp_records": "flowops_erp_records",
        "finops_usage_records": "flowops_finops_usage_records",
    }

    def __init__(
        self,
        config: CloudStoreConfig,
        firestore_backend: FirestoreBackend | None = None,
        object_storage: ObjectStorageBackend | None = None,
    ):
        self.config = config
        self.path = Path("cloud")
        self.backup_path = Path("cloud")
        self.firestore = firestore_backend or FirestoreRepository(config)
        self.object_storage = object_storage or CloudStorageRepository(config)
        self.jobs: dict[str, Job] = {}
        self.documents: dict[str, Document] = {}
        self.extractions: dict[str, Extraction] = {}
        self.events: dict[str, AgentEvent] = {}
        self.human_reviews: dict[str, HumanReview] = {}
        self.erp_records: dict[str, ERPRecord] = {}
        self.finops_usage_records: dict[str, UsageRecord] = {}
        self._counters: dict[str, int] = {}

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        self._persist("counters", "global", {"counters": dict(self._counters)})
        return f"{prefix}_{self._counters[prefix]:06d}"

    def save(self) -> None:
        self._persist("counters", "global", {"counters": dict(self._counters)})

    def load(self) -> None:
        return None

    def reset(self) -> None:
        self.jobs.clear()
        self.documents.clear()
        self.extractions.clear()
        self.events.clear()
        self.human_reviews.clear()
        self.erp_records.clear()
        self.finops_usage_records.clear()
        self._counters.clear()
        for collection in self.COLLECTIONS.values():
            try:
                self.firestore.delete_collection(collection)
            except Exception as exc:
                raise PersistenceConfigurationError(f"CloudStore reset failed for collection {collection}.") from exc

    def add_job(self, job: Job) -> Job:
        self.jobs[job.id] = job
        self._persist("jobs", job.id, job.to_dict())
        return job

    def add_document(self, document: Document) -> Document:
        self.documents[document.id] = document
        self._persist("documents", document.id, document.to_dict())
        return document

    def add_extraction(self, extraction: Extraction) -> Extraction:
        self.extractions[extraction.id] = extraction
        self._persist("extractions", extraction.id, extraction.to_dict())
        return extraction

    def add_event(self, event: AgentEvent) -> AgentEvent:
        self.events[event.id] = event
        self._persist("events", event.id, event.to_dict())
        return event

    def add_human_review(self, review: HumanReview) -> HumanReview:
        self.human_reviews[review.id] = review
        self._persist("human_reviews", review.id, review.to_dict())
        return review

    def add_erp_record(self, record: ERPRecord) -> ERPRecord:
        existing = self.find_registered_invoice(
            cnpj=record.cnpj,
            invoice_number=record.invoice_number,
            country_code=record.country_code,
            tax_id=record.tax_id,
            normalized_tax_id=record.normalized_tax_id,
            normalized_invoice_number=record.normalized_invoice_number,
        )
        if existing:
            return existing
        normalized = normalize_extraction_payload(record.to_dict(), processing_region=record.country_code or "AUTO")
        record.country_code = record.country_code or normalized.get("country_code")
        record.tax_id = record.tax_id or normalized.get("tax_id")
        record.tax_id_type = record.tax_id_type or normalized.get("tax_id_type")
        record.normalized_tax_id = record.normalized_tax_id or normalized.get("normalized_tax_id")
        record.normalized_invoice_number = record.normalized_invoice_number or normalized.get("normalized_invoice_number")
        record.currency = record.currency or normalized.get("currency")
        self.erp_records[record.id] = record
        self._persist("erp_records", record.id, record.to_dict())
        return record

    def add_finops_usage_record(self, record: UsageRecord) -> UsageRecord:
        self.finops_usage_records[record.id] = record
        self._persist("finops_usage_records", record.id, asdict(record))
        return record

    def update_job(self, job_id: str, **changes: Any) -> Job:
        job = self.jobs[job_id]
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = utc_now()
        self._persist("jobs", job.id, job.to_dict())
        return job

    def update_document(self, document_id: str, **changes: Any) -> Document:
        document = self.documents[document_id]
        for key, value in changes.items():
            setattr(document, key, value)
        document.updated_at = utc_now()
        self._persist("documents", document.id, document.to_dict())
        return document

    def documents_for_job(self, job_id: str) -> list[Document]:
        return [doc for doc in self.documents.values() if doc.job_id == job_id]

    def events_for_job(self, job_id: str) -> list[AgentEvent]:
        return sorted(
            [event for event in self.events.values() if event.job_id == job_id],
            key=lambda event: event.created_at,
        )

    def extraction_for_document(self, document_id: str) -> Extraction | None:
        matches = [item for item in self.extractions.values() if item.document_id == document_id]
        return matches[-1] if matches else None

    def reviews_for_job(self, job_id: str) -> list[HumanReview]:
        return [review for review in self.human_reviews.values() if review.job_id == job_id]

    def erp_records_for_job(self, job_id: str) -> list[ERPRecord]:
        return [record for record in self.erp_records.values() if record.job_id == job_id]

    def find_registered_invoice(
        self,
        cnpj: str | None = None,
        invoice_number: str | None = None,
        *,
        country_code: str | None = None,
        tax_id: str | None = None,
        normalized_tax_id: str | None = None,
        normalized_invoice_number: str | None = None,
    ) -> ERPRecord | None:
        lookup_key = business_key(
            {
                "country_code": country_code or ("BR" if cnpj else None),
                "tax_id": tax_id or cnpj,
                "cnpj": cnpj,
                "invoice_number": invoice_number,
                "normalized_tax_id": normalized_tax_id,
                "normalized_invoice_number": normalized_invoice_number,
            }
        )
        if not lookup_key:
            return None
        for record in self.erp_records.values():
            if business_key(record) == lookup_key:
                return record
        return None

    def open_human_reviews(self) -> list[HumanReview]:
        return sorted(
            [review for review in self.human_reviews.values() if review.status == "OPEN"],
            key=lambda review: review.created_at,
        )

    def object_path_for_document(self, document: Document) -> str:
        return self.object_storage.object_path(document)

    def store_document_bytes(self, document: Document, content: bytes, content_type: str = "application/pdf") -> str:
        object_path = self.object_storage.object_path(document)
        try:
            return self.object_storage.upload_bytes(object_path, content, content_type)
        except Exception as exc:
            raise PersistenceConfigurationError(f"CloudStore object upload failed for {object_path}.") from exc

    def _persist(self, entity_name: str, document_id: str, payload: dict[str, Any]) -> None:
        collection = self.COLLECTIONS[entity_name]
        try:
            self.firestore.upsert(collection, document_id, self._compatible_payload(entity_name, payload))
        except Exception as exc:
            raise PersistenceConfigurationError(f"CloudStore write failed for {collection}/{document_id}.") from exc

    def _compatible_payload(self, entity_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if entity_name == "jobs":
            return _filter_dataclass_payload(Job, {"processing_region": "AUTO", **payload})
        if entity_name == "documents":
            return _filter_dataclass_payload(Document, {"processing_region": "AUTO", **payload})
        if entity_name == "extractions":
            return _filter_dataclass_payload(
                Extraction,
                normalize_extraction_payload(payload, processing_region=payload.get("country_code") or "AUTO"),
            )
        if entity_name == "human_reviews":
            suggested_fields = payload.get("suggested_fields")
            if isinstance(suggested_fields, dict):
                payload = {
                    **payload,
                    "suggested_fields": normalize_extraction_payload(
                        suggested_fields,
                        processing_region=suggested_fields.get("country_code") or "AUTO",
                    ),
                }
            return _filter_dataclass_payload(HumanReview, payload)
        if entity_name == "erp_records":
            normalized = normalize_extraction_payload(payload, processing_region=payload.get("country_code") or "AUTO")
            return _filter_dataclass_payload(
                ERPRecord,
                {
                    **payload,
                    "country_code": payload.get("country_code") or normalized.get("country_code"),
                    "tax_id": payload.get("tax_id") or normalized.get("tax_id"),
                    "tax_id_type": payload.get("tax_id_type") or normalized.get("tax_id_type"),
                    "normalized_tax_id": payload.get("normalized_tax_id") or normalized.get("normalized_tax_id"),
                    "normalized_invoice_number": payload.get("normalized_invoice_number")
                    or normalized.get("normalized_invoice_number"),
                    "currency": payload.get("currency") or normalized.get("currency"),
                },
            )
        return dict(payload)
