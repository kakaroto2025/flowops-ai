from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.api.processor import JobProcessor
from shared.models import (
    CloudStore,
    LocalStore,
    PersistenceConfigurationError,
    PersistenceStore,
    create_persistence_store,
    normalize_storage_mode,
)
from tools.reporting import build_global_human_review_queue


class TrackingStore(LocalStore):
    def __init__(self, path: Path):
        super().__init__(path)
        self.object_uploads: list[dict] = []

    def store_document_bytes(self, document, content: bytes, content_type: str = "application/pdf") -> str:
        self.object_uploads.append(
            {
                "document_id": document.id,
                "storage_path": document.storage_path,
                "content": content,
                "content_type": content_type,
            }
        )
        return f"tracked://{document.id}/{document.file_name}"


class FailingObjectStore(TrackingStore):
    def store_document_bytes(self, document, content: bytes, content_type: str = "application/pdf") -> str:
        self.object_uploads.append(
            {
                "document_id": document.id,
                "storage_path": document.storage_path,
                "content": content,
                "content_type": content_type,
            }
        )
        raise PersistenceConfigurationError("object storage unavailable")


class PersistenceAbstractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def invoice(self, name: str = "NF_ABSTRACTION.pdf", invoice_number: str = "ABS-001") -> Path:
        path = self.root / name
        path.write_text(
            "\n".join(
                [
                    "Empresa: Abstraction Test Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    f"NF: {invoice_number}",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def invalid_invoice(self) -> Path:
        path = self.root / "NF_ABSTRACTION_REVIEW.pdf"
        path.write_text(
            "\n".join(
                [
                    "Empresa: Abstraction Review Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def gemini_payload(self, invoice_number: str = "ABS-001") -> dict:
        return {
            "document_type": "invoice",
            "cnpj": "12.345.678/0001-90",
            "company_name": "Abstraction Test Ltda",
            "invoice_number": invoice_number,
            "issue_date": "14/08/2026",
            "total_amount": 100.0,
            "confidence": 0.98,
            "warnings": [],
            "gemini_usage_metadata": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        }

    def processor(self, store: PersistenceStore) -> JobProcessor:
        return JobProcessor(store)

    def test_cloud_mode_creates_cloud_store_when_config_is_valid(self):
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
                patch("shared.models.cloud_store.FirestoreRepository"),
                patch("shared.models.cloud_store.CloudStorageRepository"),
            ):
                store = create_persistence_store()

        self.assertIsInstance(store, CloudStore)

    def test_default_storage_mode_is_local(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(normalize_storage_mode(), "local")

    def test_explicit_local_storage_creates_local_store(self):
        path = self.root / "state.json"
        store = create_persistence_store("local", path)

        self.assertIsInstance(store, LocalStore)
        self.assertIsInstance(store, PersistenceStore)
        self.assertEqual(store.path, path)

    def test_storage_mode_env_selects_local(self):
        with patch.dict(os.environ, {"STORAGE_MODE": "local"}):
            store = create_persistence_store(path=self.root / "state.json")

        self.assertIsInstance(store, LocalStore)

    def test_cloud_storage_mode_requires_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(PersistenceConfigurationError, "GOOGLE_CLOUD_PROJECT"):
                create_persistence_store("cloud", self.root / "state.json")

    def test_unknown_storage_mode_fails_safely(self):
        with self.assertRaisesRegex(PersistenceConfigurationError, "Unsupported STORAGE_MODE"):
            create_persistence_store("memory", self.root / "state.json")

    def test_pipeline_dedup_human_review_audit_and_finops_use_persistence_store(self):
        store = create_persistence_store("local", self.root / "state.json")
        processor = self.processor(store)

        with (
            patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"),
            patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()),
        ):
            first_job = processor.create_upload_job([self.invoice("NF_ABSTRACTION_1.pdf")])
            first_dashboard = processor.run_job(first_job.id)

        self.assertEqual(first_dashboard["documents"][0]["status"], "REGISTERED")
        self.assertEqual(len(store.erp_records), 1)
        self.assertGreaterEqual(len(store.finops_usage_records), 1)

        with (
            patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"),
            patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()),
        ):
            duplicate_job = processor.create_upload_job([self.invoice("NF_ABSTRACTION_DUP.pdf")])
            duplicate_dashboard = processor.run_job(duplicate_job.id)

        self.assertEqual(duplicate_dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(store.erp_records), 1)
        duplicate_events = [event.event_type for event in store.events_for_job(duplicate_job.id)]
        self.assertIn("DUPLICATE_DETECTED", duplicate_events)

        invalid_payload = self.gemini_payload(invoice_number=None)
        with (
            patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"),
            patch("agents.document.agent.extract_with_gemini", return_value=invalid_payload),
        ):
            review_job = processor.create_upload_job([self.invalid_invoice()])
            review_dashboard = processor.run_job(review_job.id)

        self.assertEqual(review_dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertEqual(len(build_global_human_review_queue(store)), 1)
        review_events = [event.event_type for event in store.events_for_job(review_job.id)]
        self.assertIn("ADK_WORKFLOW_STARTED", review_events)
        self.assertIn("DECISION_HUMAN_REVIEW", review_events)

    def test_pipeline_stores_uploaded_document_through_persistence_abstraction(self):
        store = TrackingStore(self.root / "state.json")
        processor = self.processor(store)

        job = processor.create_upload_job([self.invoice("NF_ABSTRACTION_STORAGE.pdf")])

        self.assertEqual(len(store.object_uploads), 1)
        upload = store.object_uploads[0]
        self.assertEqual(upload["document_id"], "doc_000001")
        self.assertEqual(upload["content_type"], "application/pdf")
        self.assertIn(b"Abstraction Test Ltda", upload["content"])
        events = [event.event_type for event in store.events_for_job(job.id)]
        self.assertIn("DOCUMENT_STORED", events)

    def test_pipeline_does_not_silently_fallback_when_storage_backend_fails(self):
        store = FailingObjectStore(self.root / "state.json")
        processor = self.processor(store)

        with self.assertRaisesRegex(PersistenceConfigurationError, "object storage unavailable"):
            processor.create_upload_job([self.invoice("NF_ABSTRACTION_STORAGE_FAIL.pdf")])

        self.assertEqual(len(store.object_uploads), 1)


if __name__ == "__main__":
    unittest.main()
