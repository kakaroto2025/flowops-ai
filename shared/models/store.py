from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from dataclasses import asdict, fields
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .entities import AgentEvent, Document, ERPRecord, Extraction, HumanReview, Job, utc_now
from .persistence import PersistenceStore
from tools.finops.models import UsageRecord
from tools.documents.normalization import business_key, normalize_extraction_payload


logger = logging.getLogger(__name__)


def _filter_dataclass_payload(model: type, payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {field.name for field in fields(model)}
    return {key: value for key, value in payload.items() if key in allowed}


def _normalize_business_key(value: str | None) -> str:
    return "".join(char for char in str(value or "") if char.isalnum()).lower()


class LocalStore(PersistenceStore):
    def __init__(self, path: str | Path = "local_data/state.json"):
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        # Process-local protection only. This does not coordinate multiple Uvicorn workers/processes.
        self._write_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.documents: dict[str, Document] = {}
        self.extractions: dict[str, Extraction] = {}
        self.events: dict[str, AgentEvent] = {}
        self.human_reviews: dict[str, HumanReview] = {}
        self.erp_records: dict[str, ERPRecord] = {}
        self.finops_usage_records: dict[str, UsageRecord] = {}
        self._counters: dict[str, int] = {}
        self.load()

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}_{self._counters[prefix]:06d}"

    def save(self) -> None:
        with self._write_lock:
            payload = {
                "counters": self._counters,
                "jobs": {k: asdict(v) for k, v in self.jobs.items()},
                "documents": {k: asdict(v) for k, v in self.documents.items()},
                "extractions": {k: asdict(v) for k, v in self.extractions.items()},
                "events": {k: asdict(v) for k, v in self.events.items()},
                "human_reviews": {k: asdict(v) for k, v in self.human_reviews.items()},
                "erp_records": {k: asdict(v) for k, v in self.erp_records.items()},
                "finops_usage_records": {k: asdict(v) for k, v in self.finops_usage_records.items()},
            }
            self._atomic_write_json(payload)

    def load(self) -> None:
        source_path = self.path
        if not self.path.exists():
            if self.backup_path.exists():
                logger.warning("state.json missing; loading LocalStore from backup %s", self.backup_path)
                source_path = self.backup_path
            else:
                return
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except JSONDecodeError:
            if source_path == self.path and self.backup_path.exists():
                try:
                    payload = json.loads(self.backup_path.read_text(encoding="utf-8"))
                except JSONDecodeError:
                    logger.exception("state.json and state.json.bak are both invalid; LocalStore load failed")
                    raise
                logger.warning("state.json invalid; loading LocalStore from backup %s", self.backup_path)
            else:
                logger.exception("LocalStore state file is invalid: %s", source_path)
                raise
        except OSError:
            logger.exception("LocalStore state file could not be read: %s", source_path)
            raise

        self._load_payload(payload)

    def _load_payload(self, payload: dict[str, Any]) -> None:
        self._counters = payload.get("counters", {})
        self.jobs = {k: Job(**self._compatible_job_payload(v)) for k, v in payload.get("jobs", {}).items()}
        self.documents = {k: Document(**self._compatible_document_payload(v)) for k, v in payload.get("documents", {}).items()}
        self.extractions = {k: Extraction(**self._compatible_extraction_payload(v)) for k, v in payload.get("extractions", {}).items()}
        self.events = {k: AgentEvent(**v) for k, v in payload.get("events", {}).items()}
        self.human_reviews = {
            k: HumanReview(**self._compatible_human_review_payload(v)) for k, v in payload.get("human_reviews", {}).items()
        }
        self.erp_records = {k: ERPRecord(**self._compatible_erp_payload(v)) for k, v in payload.get("erp_records", {}).items()}
        self.finops_usage_records = {
            k: UsageRecord(**self._compatible_finops_usage_payload(v))
            for k, v in payload.get("finops_usage_records", {}).items()
        }

    def _compatible_job_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _filter_dataclass_payload(Job, {"processing_region": "AUTO", **payload})

    def _compatible_document_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _filter_dataclass_payload(Document, {"processing_region": "AUTO", **payload})

    def _compatible_extraction_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _filter_dataclass_payload(
            Extraction,
            normalize_extraction_payload(payload, processing_region=payload.get("country_code") or "AUTO"),
        )

    def _compatible_human_review_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    def _compatible_erp_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
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

    def _compatible_finops_usage_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _filter_dataclass_payload(UsageRecord, payload)

    def _atomic_write_json(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        serialized = json.dumps(payload, indent=2, ensure_ascii=False)
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())

            if self._is_valid_json_file(self.path):
                shutil.copy2(self.path, self.backup_path)
            os.replace(temp_path, self.path)
            self._fsync_directory()
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _is_valid_json_file(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (JSONDecodeError, OSError):
            return False
        return True

    def _fsync_directory(self) -> None:
        if os.name == "nt":
            return
        directory_fd = os.open(self.path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def reset(self) -> None:
        self.jobs.clear()
        self.documents.clear()
        self.extractions.clear()
        self.events.clear()
        self.human_reviews.clear()
        self.erp_records.clear()
        self.finops_usage_records.clear()
        self._counters.clear()
        self.save()

    def add_job(self, job: Job) -> Job:
        self.jobs[job.id] = job
        self.save()
        return job

    def add_document(self, document: Document) -> Document:
        self.documents[document.id] = document
        self.save()
        return document

    def add_extraction(self, extraction: Extraction) -> Extraction:
        self.extractions[extraction.id] = extraction
        self.save()
        return extraction

    def add_event(self, event: AgentEvent) -> AgentEvent:
        self.events[event.id] = event
        self.save()
        return event

    def add_human_review(self, review: HumanReview) -> HumanReview:
        self.human_reviews[review.id] = review
        self.save()
        return review

    def add_erp_record(self, record: ERPRecord) -> ERPRecord:
        normalized = normalize_extraction_payload(record.to_dict(), processing_region=record.country_code or "AUTO")
        record.country_code = record.country_code or normalized.get("country_code")
        record.tax_id = record.tax_id or normalized.get("tax_id")
        record.tax_id_type = record.tax_id_type or normalized.get("tax_id_type")
        record.normalized_tax_id = record.normalized_tax_id or normalized.get("normalized_tax_id")
        record.normalized_invoice_number = record.normalized_invoice_number or normalized.get("normalized_invoice_number")
        record.currency = record.currency or normalized.get("currency")
        self.erp_records[record.id] = record
        self.save()
        return record

    def add_finops_usage_record(self, record: UsageRecord) -> UsageRecord:
        self.finops_usage_records[record.id] = record
        self.save()
        return record

    def update_job(self, job_id: str, **changes: Any) -> Job:
        job = self.jobs[job_id]
        for key, value in changes.items():
            setattr(job, key, value)
        job.updated_at = utc_now()
        self.save()
        return job

    def update_document(self, document_id: str, **changes: Any) -> Document:
        document = self.documents[document_id]
        for key, value in changes.items():
            setattr(document, key, value)
        document.updated_at = utc_now()
        self.save()
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
