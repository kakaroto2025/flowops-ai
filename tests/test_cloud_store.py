from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.models import (
    AgentEvent,
    CloudStore,
    CloudStoreConfig,
    Document,
    ERPRecord,
    Extraction,
    HumanReview,
    LocalStore,
    PersistenceConfigurationError,
    Job,
    create_persistence_store,
)


class FakeFirestoreBackend:
    def __init__(self, fail_writes: bool = False):
        self.fail_writes = fail_writes
        self.upserts: list[tuple[str, str, dict]] = []
        self.deleted_collections: list[str] = []

    def upsert(self, collection: str, document_id: str, payload: dict) -> None:
        if self.fail_writes:
            raise RuntimeError("firestore unavailable")
        self.upserts.append((collection, document_id, payload))

    def delete_collection(self, collection: str) -> None:
        self.deleted_collections.append(collection)


class FakeObjectStorage:
    def __init__(self, fail_uploads: bool = False):
        self.fail_uploads = fail_uploads
        self.uploads: list[tuple[str, bytes, str]] = []

    def object_path(self, document: Document) -> str:
        return f"documents/default/{document.id}/{document.file_name}"

    def upload_bytes(self, object_path: str, content: bytes, content_type: str) -> str:
        if self.fail_uploads:
            raise RuntimeError("storage unavailable")
        self.uploads.append((object_path, content, content_type))
        return f"gs://flowops-test/{object_path}"


class CloudStoreTests(unittest.TestCase):
    def config(self) -> CloudStoreConfig:
        return CloudStoreConfig(
            project_id="flowops-test",
            firestore_database="(default)",
            storage_bucket="flowops-test-bucket",
        )

    def store(
        self,
        firestore: FakeFirestoreBackend | None = None,
        storage: FakeObjectStorage | None = None,
    ) -> CloudStore:
        return CloudStore(
            self.config(),
            firestore_backend=firestore or FakeFirestoreBackend(),
            object_storage=storage or FakeObjectStorage(),
        )

    def test_cloud_config_requires_project_database_and_bucket(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(PersistenceConfigurationError, "GOOGLE_CLOUD_PROJECT"):
                CloudStoreConfig.from_env()

        with patch.dict(
            os.environ,
            {
                "GOOGLE_CLOUD_PROJECT": "flowops-test",
                "FIRESTORE_DATABASE": "(default)",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(PersistenceConfigurationError, "FLOWOPS_STORAGE_BUCKET"):
                CloudStoreConfig.from_env()

    def test_cloud_mode_does_not_fallback_to_local(self):
        with patch.dict(
            os.environ,
            {
                "STORAGE_MODE": "cloud",
                "GOOGLE_CLOUD_PROJECT": "flowops-test",
                "FIRESTORE_DATABASE": "(default)",
                "FLOWOPS_STORAGE_BUCKET": "flowops-test-bucket",
            },
            clear=True,
        ):
            with (
                patch("shared.models.cloud_store.FirestoreRepository", return_value=FakeFirestoreBackend()),
                patch("shared.models.cloud_store.CloudStorageRepository", return_value=FakeObjectStorage()),
            ):
                store = create_persistence_store()

        self.assertIsInstance(store, CloudStore)
        self.assertNotIsInstance(store, LocalStore)

    def test_local_mode_does_not_instantiate_cloud_repositories(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"STORAGE_MODE": "local"}, clear=True):
                with (
                    patch("shared.models.cloud_store.FirestoreRepository", side_effect=AssertionError("no cloud")),
                    patch("shared.models.cloud_store.CloudStorageRepository", side_effect=AssertionError("no cloud")),
                ):
                    store = create_persistence_store(path=Path(tmp) / "state.json")

        self.assertIsInstance(store, LocalStore)

    def test_metadata_is_mapped_to_firestore_collections(self):
        firestore = FakeFirestoreBackend()
        store = self.store(firestore=firestore)
        job_id = store.next_id("job")
        doc_id = store.next_id("doc")
        job = store.add_job(store_job(job_id))
        document = store.add_document(store_document(doc_id, job.id))
        extraction = store.add_extraction(store_extraction("ext_000001", document.id))
        review = store.add_human_review(store_review("review_000001", job.id, document.id))
        event = store.add_event(store_event("event_000001", job.id, document.id))

        self.assertEqual(job.id, "job_000001")
        self.assertEqual(document.id, "doc_000001")
        self.assertEqual(extraction.country_code, "BR")
        self.assertEqual(review.status, "OPEN")
        self.assertEqual(event.event_type, "TEST_EVENT")
        collections = [item[0] for item in firestore.upserts]
        self.assertIn("flowops_jobs", collections)
        self.assertIn("flowops_documents", collections)
        self.assertIn("flowops_extractions", collections)
        self.assertIn("flowops_human_reviews", collections)
        self.assertIn("flowops_events", collections)

    def test_object_path_is_deterministic(self):
        storage = FakeObjectStorage()
        store = self.store(storage=storage)
        document = store_document("doc_000123", "job_000001", file_name="invoice.pdf")

        self.assertEqual(store.object_path_for_document(document), "documents/default/doc_000123/invoice.pdf")
        self.assertEqual(
            store.store_document_bytes(document, b"%PDF-test"),
            "gs://flowops-test/documents/default/doc_000123/invoice.pdf",
        )
        self.assertEqual(storage.uploads[0], ("documents/default/doc_000123/invoice.pdf", b"%PDF-test", "application/pdf"))

    def test_erp_deduplication_is_idempotent(self):
        firestore = FakeFirestoreBackend()
        store = self.store(firestore=firestore)
        first = store.add_erp_record(store_erp("erp_000001", "job_000001", "doc_000001"))
        second = store.add_erp_record(store_erp("erp_000002", "job_000002", "doc_000002"))

        self.assertEqual(first.id, "erp_000001")
        self.assertEqual(second.id, "erp_000001")
        self.assertEqual(len(store.erp_records), 1)
        self.assertEqual([item[0] for item in firestore.upserts].count("flowops_erp_records"), 1)

    def test_audit_human_review_and_queries(self):
        store = self.store()
        job = store.add_job(store_job("job_000001"))
        document = store.add_document(store_document("doc_000001", job.id))
        store.add_event(store_event("event_000002", job.id, document.id, created_at="2026-01-01T00:00:02+00:00"))
        store.add_event(store_event("event_000001", job.id, document.id, created_at="2026-01-01T00:00:01+00:00"))
        store.add_human_review(store_review("review_000001", job.id, document.id))
        store.add_human_review(store_review("review_000002", job.id, document.id, status="CLOSED"))

        self.assertEqual([event.id for event in store.events_for_job(job.id)], ["event_000001", "event_000002"])
        self.assertEqual([review.id for review in store.open_human_reviews()], ["review_000001"])
        self.assertEqual([review.id for review in store.reviews_for_job(job.id)], ["review_000001", "review_000002"])

    def test_firestore_errors_are_safe(self):
        store = self.store(firestore=FakeFirestoreBackend(fail_writes=True))

        with self.assertRaisesRegex(PersistenceConfigurationError, "CloudStore write failed"):
            store.add_job(store_job("job_000001"))

    def test_cloud_storage_errors_are_safe(self):
        store = self.store(storage=FakeObjectStorage(fail_uploads=True))

        with self.assertRaisesRegex(PersistenceConfigurationError, "CloudStore object upload failed"):
            store.store_document_bytes(store_document("doc_000001", "job_000001"), b"%PDF-test")


def store_job(job_id: str) -> Job:
    return Job(id=job_id, source="test", document_count=1)


def store_document(document_id: str, job_id: str, file_name: str = "NF_TEST.pdf") -> Document:
    return Document(id=document_id, job_id=job_id, file_name=file_name, storage_path=f"uploads/{file_name}")


def store_extraction(extraction_id: str, document_id: str) -> Extraction:
    return Extraction(
        id=extraction_id,
        document_id=document_id,
        document_type="invoice",
        cnpj="12.345.678/0001-90",
        company_name="Cloud Store Test Ltda",
        invoice_number="INV-001",
        issue_date="14/08/2026",
        total_amount=100.0,
        confidence=0.99,
        country_code="BR",
        currency="BRL",
    )


def store_review(review_id: str, job_id: str, document_id: str, status: str = "OPEN") -> HumanReview:
    return HumanReview(id=review_id, job_id=job_id, document_id=document_id, reason="missing_invoice", status=status)


def store_event(
    event_id: str,
    job_id: str,
    document_id: str,
    created_at: str = "2026-01-01T00:00:00+00:00",
) -> AgentEvent:
    return AgentEvent(
        id=event_id,
        job_id=job_id,
        document_id=document_id,
        agent="TestAgent",
        event_type="TEST_EVENT",
        message="test",
        created_at=created_at,
    )


def store_erp(erp_id: str, job_id: str, document_id: str) -> ERPRecord:
    return ERPRecord(
        id=erp_id,
        job_id=job_id,
        document_id=document_id,
        invoice_number="INV-001",
        cnpj="12.345.678/0001-90",
        total_amount=100.0,
        company_name="Cloud Store Test Ltda",
        country_code="BR",
        tax_id="12.345.678/0001-90",
        currency="BRL",
    )


if __name__ == "__main__":
    unittest.main()
