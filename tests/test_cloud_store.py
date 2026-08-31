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
        self.objects: dict[str, dict] = {}
        self.generation = 0

    def object_path(self, document: Document) -> str:
        return f"documents/default/{document.id}/{document.file_name}"

    def upload_bytes(self, object_path: str, content: bytes, content_type: str) -> str:
        if self.fail_uploads:
            raise RuntimeError("storage unavailable")
        self.uploads.append((object_path, content, content_type))
        self.generation += 1
        self.objects[object_path] = {
            "content": content,
            "content_type": content_type,
            "generation": self.generation,
            "metageneration": 1,
            "name": object_path,
            "size": len(content),
        }
        return f"gs://flowops-test/{object_path}"

    def metadata(self, object_path: str) -> dict:
        if object_path not in self.objects:
            raise FileNotFoundError(object_path)
        stored = self.objects[object_path]
        return {key: value for key, value in stored.items() if key != "content"}

    def download_bytes(self, object_path: str) -> bytes:
        if object_path not in self.objects:
            raise FileNotFoundError(object_path)
        return self.objects[object_path]["content"]

    def delete(self, object_path: str) -> None:
        if object_path not in self.objects:
            raise FileNotFoundError(object_path)
        del self.objects[object_path]


class GenerationBoundBucket:
    def __init__(self):
        self.name = "flowops-test-bucket"
        self.objects: dict[str, dict] = {}
        self.generation = 0
        self.blob_calls: list[str] = []

    def blob(self, object_path: str):
        self.blob_calls.append(object_path)
        return GenerationBoundBlob(self, object_path)


class GenerationBoundBlob:
    def __init__(self, bucket: GenerationBoundBucket, name: str):
        self.bucket = bucket
        self.name = name
        self.size = None
        self.content_type = None
        self.generation = None
        self.metageneration = None
        self.updated = None

    def upload_from_string(self, content: bytes, content_type: str) -> None:
        self.bucket.generation += 1
        self.bucket.objects[self.name] = {
            "content": content,
            "content_type": content_type,
            "generation": self.bucket.generation,
            "metageneration": 1,
            "size": len(content),
        }
        self._load_current()

    def reload(self) -> None:
        self._raise_if_missing_or_stale()
        self._load_current()

    def download_as_bytes(self) -> bytes:
        self._raise_if_missing_or_stale()
        self._load_current()
        return self.bucket.objects[self.name]["content"]

    def delete(self) -> None:
        self._raise_if_missing_or_stale()
        del self.bucket.objects[self.name]

    def _raise_if_missing_or_stale(self) -> None:
        stored = self.bucket.objects.get(self.name)
        if stored is None:
            raise FileNotFoundError(self.name)
        if self.generation is not None and self.generation != stored["generation"]:
            raise FileNotFoundError("stale generation")

    def _load_current(self) -> None:
        stored = self.bucket.objects[self.name]
        self.size = stored["size"]
        self.content_type = stored["content_type"]
        self.generation = stored["generation"]
        self.metageneration = stored["metageneration"]


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

    def test_storage_same_path_overwrite_uses_current_generation(self):
        from shared.models.cloud_store import CloudStorageRepository

        repository = CloudStorageRepository.__new__(CloudStorageRepository)
        repository.bucket = GenerationBoundBucket()
        object_path = "pilot-v2/smoke-test/persistence-smoke-test.txt"

        stale_blob = repository.bucket.blob(object_path)
        repository.upload_bytes(object_path, b"first", "text/plain")
        stale_blob.reload()
        self.assertEqual(stale_blob.generation, 1)

        repository.upload_bytes(object_path, b"second", "text/plain")
        with self.assertRaises(FileNotFoundError):
            stale_blob.reload()

        metadata = repository.metadata(object_path)
        self.assertEqual(metadata["generation"], 2)
        self.assertEqual(metadata["size"], len(b"second"))
        self.assertEqual(repository.download_bytes(object_path), b"second")
        self.assertEqual(list(repository.bucket.objects), [object_path])

    def test_cloud_store_same_path_same_content_is_deterministic(self):
        storage = FakeObjectStorage()
        store = self.store(storage=storage)
        document = store_document("doc_000123", "job_000001", file_name="invoice.pdf")

        first_uri = store.store_document_bytes(document, b"%PDF-test")
        first_metadata = store.document_object_metadata(document)
        second_uri = store.store_document_bytes(document, b"%PDF-test")
        second_metadata = store.document_object_metadata(document)

        self.assertEqual(first_uri, second_uri)
        self.assertEqual(store.read_document_bytes(document), b"%PDF-test")
        self.assertGreater(second_metadata["generation"], first_metadata["generation"])
        self.assertEqual(list(storage.objects), ["documents/default/doc_000123/invoice.pdf"])

    def test_cloud_store_same_path_different_content_overwrites_explicitly(self):
        storage = FakeObjectStorage()
        store = self.store(storage=storage)
        document = store_document("doc_000123", "job_000001", file_name="invoice.pdf")

        store.store_document_bytes(document, b"old")
        store.store_document_bytes(document, b"new")

        self.assertEqual(store.read_document_bytes(document), b"new")
        self.assertEqual(store.document_object_metadata(document)["size"], len(b"new"))
        self.assertEqual(len(storage.objects), 1)

    def test_cloud_store_missing_object_remains_error(self):
        store = self.store()
        document = store_document("doc_000404", "job_000001", file_name="missing.pdf")

        with self.assertRaises(FileNotFoundError):
            store.document_object_metadata(document)
        with self.assertRaises(FileNotFoundError):
            store.read_document_bytes(document)

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
