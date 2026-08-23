from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from dataclasses import asdict
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .entities import AgentEvent, Document, ERPRecord, Extraction, HumanReview, Job, utc_now


logger = logging.getLogger(__name__)


def _normalize_business_key(value: str | None) -> str:
    return "".join(char for char in str(value or "") if char.isalnum()).lower()


class LocalStore:
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
        self.jobs = {k: Job(**v) for k, v in payload.get("jobs", {}).items()}
        self.documents = {k: Document(**v) for k, v in payload.get("documents", {}).items()}
        self.extractions = {k: Extraction(**v) for k, v in payload.get("extractions", {}).items()}
        self.events = {k: AgentEvent(**v) for k, v in payload.get("events", {}).items()}
        self.human_reviews = {k: HumanReview(**v) for k, v in payload.get("human_reviews", {}).items()}
        self.erp_records = {k: ERPRecord(**v) for k, v in payload.get("erp_records", {}).items()}

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
        self.erp_records[record.id] = record
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

    def find_registered_invoice(self, cnpj: str | None, invoice_number: str | None) -> ERPRecord | None:
        cnpj_key = _normalize_business_key(cnpj)
        invoice_key = _normalize_business_key(invoice_number)
        if not cnpj_key or not invoice_key:
            return None
        for record in self.erp_records.values():
            if (
                _normalize_business_key(record.cnpj) == cnpj_key
                and _normalize_business_key(record.invoice_number) == invoice_key
            ):
                return record
        return None

    def open_human_reviews(self) -> list[HumanReview]:
        return sorted(
            [review for review in self.human_reviews.values() if review.status == "OPEN"],
            key=lambda review: review.created_at,
        )
