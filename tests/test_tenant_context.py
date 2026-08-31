from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.intake import IntakeAgent
from apps.api.processor import JobProcessor
from shared.models import (
    AuthContext,
    DEVELOPMENT_TENANT_ID,
    DEVELOPMENT_USER_ID,
    Document,
    ERPRecord,
    HumanReview,
    Job,
    LocalStore,
    TenantContext,
    TenantContextError,
    development_auth_context,
    require_tenant_id,
)
from tools.reporting import build_global_human_review_queue


class TenantContextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = LocalStore(self.root / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def invoice(self, name: str = "NF_TENANT.pdf", invoice_number: str = "TEN-001") -> Path:
        path = self.root / name
        path.write_text(
            "\n".join(
                [
                    "Empresa: Tenant Test Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    f"NF: {invoice_number}",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def gemini_payload(self, invoice_number: str | None = "TEN-001") -> dict:
        return {
            "document_type": "invoice",
            "cnpj": "12.345.678/0001-90",
            "company_name": "Tenant Test Ltda",
            "invoice_number": invoice_number,
            "issue_date": "14/08/2026",
            "total_amount": 100.0,
            "confidence": 0.98,
            "warnings": [],
            "gemini_usage_metadata": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        }

    def test_development_context_is_explicit(self):
        auth = development_auth_context()

        self.assertEqual(auth.tenant_id, DEVELOPMENT_TENANT_ID)
        self.assertEqual(auth.user_id, DEVELOPMENT_USER_ID)
        self.assertTrue(auth.authenticated)
        self.assertEqual(auth.require_tenant_id(), DEVELOPMENT_TENANT_ID)

    def test_context_ids_must_be_safe(self):
        TenantContext("tenant_acme", "Acme")
        AuthContext("user_admin", "tenant_acme", authenticated=True)

        with self.assertRaises(TenantContextError):
            TenantContext("../tenant", "Unsafe")
        with self.assertRaises(TenantContextError):
            AuthContext("user/admin", "tenant_acme", authenticated=True)

    def test_missing_or_unauthenticated_context_fails_closed(self):
        with self.assertRaises(TenantContextError):
            require_tenant_id(None)
        with self.assertRaises(TenantContextError):
            require_tenant_id(AuthContext("local_dev_user", "tenant_default", authenticated=False))
        with self.assertRaises(TenantContextError):
            IntakeAgent(self.store).create_job([self.invoice()], auth_context=None)

    def test_processor_propagates_tenant_to_pipeline_records(self):
        auth = AuthContext("user_ops", "tenant_acme", authenticated=True)
        processor = JobProcessor(self.store, auth_context=auth)

        with (
            patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"),
            patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()),
        ):
            job = processor.create_upload_job([self.invoice()])
            dashboard = processor.run_job(job.id)

        document = self.store.documents[dashboard["documents"][0]["id"]]
        extraction = self.store.extraction_for_document(document.id)
        self.assertEqual(job.tenant_id, "tenant_acme")
        self.assertEqual(job.user_id, "user_ops")
        self.assertEqual(document.tenant_id, "tenant_acme")
        self.assertEqual(extraction.tenant_id, "tenant_acme")
        self.assertEqual(next(iter(self.store.erp_records.values())).tenant_id, "tenant_acme")
        self.assertEqual(next(iter(self.store.finops_usage_records.values())).tenant_id, "tenant_acme")
        self.assertTrue(all(event.tenant_id == "tenant_acme" for event in self.store.events_for_job(job.id)))

    def test_human_review_keeps_tenant_context(self):
        auth = AuthContext("user_ops", "tenant_acme", authenticated=True)
        processor = JobProcessor(self.store, auth_context=auth)

        with (
            patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"),
            patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload(invoice_number=None)),
        ):
            job = processor.create_upload_job([self.invoice("NF_TENANT_REVIEW.pdf")])
            processor.run_job(job.id)

        review = build_global_human_review_queue(self.store)[0]
        self.assertEqual(review["tenant_id"], "tenant_acme")
        self.assertEqual(self.store.human_reviews[review["id"]].tenant_id, "tenant_acme")

    def test_known_id_lookup_from_wrong_tenant_is_blocked(self):
        job = self.store.add_job(
            Job(
                id="job_000001",
                source="test",
                tenant_id="tenant_a",
                user_id="user_a",
                document_count=1,
            )
        )
        document = self.store.add_document(
            Document(id="doc_000001", job_id=job.id, file_name="invoice.pdf", storage_path="uploads/invoice.pdf", tenant_id="tenant_a")
        )
        review = self.store.add_human_review(
            HumanReview(id="review_000001", job_id=job.id, document_id=document.id, reason="missing_invoice", tenant_id="tenant_a")
        )

        self.assertEqual(self.store.job_for_tenant("tenant_a", job.id), job)
        self.assertEqual(self.store.document_for_tenant("tenant_a", document.id), document)
        self.assertEqual(self.store.review_for_tenant("tenant_a", review.id), review)

        with self.assertRaises(TenantContextError):
            self.store.job_for_tenant("tenant_b", job.id)
        with self.assertRaises(TenantContextError):
            self.store.document_for_tenant("tenant_b", document.id)
        with self.assertRaises(TenantContextError):
            self.store.review_for_tenant("tenant_b", review.id)

    def test_deduplication_is_tenant_scoped(self):
        record_a = ERPRecord(
            id="erp_000001",
            job_id="job_000001",
            document_id="doc_000001",
            invoice_number="TEN-001",
            cnpj="12.345.678/0001-90",
            total_amount=100.0,
            company_name="Tenant A",
            country_code="BR",
            tax_id="12.345.678/0001-90",
            currency="BRL",
            tenant_id="tenant_a",
        )
        self.store.add_erp_record(record_a)

        self.assertIsNotNone(
            self.store.find_registered_invoice(
                cnpj=record_a.cnpj,
                invoice_number=record_a.invoice_number,
                country_code=record_a.country_code,
                tax_id=record_a.tax_id,
                tenant_id="tenant_a",
            )
        )
        self.assertIsNone(
            self.store.find_registered_invoice(
                cnpj=record_a.cnpj,
                invoice_number=record_a.invoice_number,
                country_code=record_a.country_code,
                tax_id=record_a.tax_id,
                tenant_id="tenant_b",
            )
        )


if __name__ == "__main__":
    unittest.main()
