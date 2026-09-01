from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.api.processor import JobProcessor
from shared.models import AuthContext, LocalStore
from tools.email_intake import (
    EmailAttachment,
    EmailMessageMetadata,
    FakeEmailIntakeProvider,
    NormalizedEmailMessage,
)


class EmailIntakeFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = LocalStore(self.root / "state.json")
        self.adk_patch = patch(
            "agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime",
            return_value="READY",
        )
        self.adk_patch.start()

    def tearDown(self):
        self.adk_patch.stop()
        self.tmp.cleanup()

    def message(
        self,
        *,
        message_id: str = "provider-msg-001",
        attachment_id: str = "att-001",
        file_name: str = "EMAIL_VALID.pdf",
        content_type: str = "application/pdf",
        content: bytes | None = None,
    ) -> NormalizedEmailMessage:
        payload = content if content is not None else self.invoice_text("EMAIL-001").encode("utf-8")
        return NormalizedEmailMessage(
            metadata=EmailMessageMetadata(
                provider_message_id=message_id,
                provider_thread_id="thread-001",
                sender="ap@example.test",
                recipients=("invoices@flowops.test",),
                subject="Invoices",
                received_at="2026-09-01T12:00:00+00:00",
            ),
            attachments=(
                EmailAttachment(
                    attachment_id=attachment_id,
                    file_name=file_name,
                    content_type=content_type,
                    size_bytes=len(payload),
                    content=payload,
                ),
            ),
        )

    def invoice_text(self, invoice_number: str) -> str:
        return "\n".join(
            [
                "Empresa: Email Intake Test Ltda",
                "CNPJ: 12.345.678/0001-90",
                f"NF: {invoice_number}",
                "Data: 14/08/2026",
                "Valor Total: R$ 100,00",
            ]
        )

    def gemini_payload(self, invoice_number: str = "EMAIL-001") -> dict:
        return {
            "document_type": "invoice",
            "cnpj": "12.345.678/0001-90",
            "company_name": "Email Intake Test Ltda",
            "invoice_number": invoice_number,
            "issue_date": "14/08/2026",
            "total_amount": 100.0,
            "confidence": 0.98,
            "warnings": [],
            "gemini_usage_metadata": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        }

    def processor(self, tenant_id: str = "tenant_email", user_id: str = "user_email") -> JobProcessor:
        auth = AuthContext(user_id=user_id, tenant_id=tenant_id, authenticated=True)
        return JobProcessor(self.store, auth_context=auth)

    def test_fake_provider_returns_messages_without_external_calls(self):
        provider = FakeEmailIntakeProvider([self.message()])

        messages = provider.list_messages()

        self.assertEqual(len(messages), 1)
        self.assertEqual(provider.fetch_count, 1)

    def test_email_pdf_enters_existing_pipeline(self):
        processor = self.processor()

        with patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()):
            result = processor.process_email_message(self.message(), work_dir=self.root / "email")

        self.assertIsNotNone(result.submitted_job_id)
        dashboard = result.dashboard
        self.assertEqual(dashboard["job"]["source"], "email_intake")
        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        self.assertEqual(len(self.store.erp_records), 1)
        self.assertEqual(self.store.jobs[result.submitted_job_id].tenant_id, "tenant_email")

    def test_email_intake_preserves_tenant_server_side(self):
        processor = self.processor(tenant_id="tenant_secure", user_id="user_ops")

        with patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()):
            result = processor.process_email_message(self.message(), work_dir=self.root / "email")

        document = self.store.documents[result.dashboard["documents"][0]["id"]]
        self.assertEqual(self.store.jobs[result.submitted_job_id].tenant_id, "tenant_secure")
        self.assertEqual(self.store.jobs[result.submitted_job_id].user_id, "user_ops")
        self.assertEqual(document.tenant_id, "tenant_secure")

    def test_invalid_attachment_is_rejected_without_job(self):
        processor = self.processor()
        message = self.message(file_name="notes.txt", content_type="text/plain", content=b"not a pdf")

        result = processor.process_email_message(message, work_dir=self.root / "email")

        self.assertIsNone(result.submitted_job_id)
        self.assertEqual(result.rejected[0].reason, "unsupported_attachment_type")
        self.assertEqual(self.store.jobs, {})

    def test_oversized_attachment_is_rejected_without_job(self):
        processor = self.processor()
        oversized = b"x" * (processor.cost_guard.config.max_file_size_bytes + 1)
        message = self.message(content=oversized)

        result = processor.process_email_message(message, work_dir=self.root / "email")

        self.assertIsNone(result.submitted_job_id)
        self.assertEqual(result.rejected[0].reason, "file_size_limit_exceeded")
        self.assertEqual(self.store.jobs, {})

    def test_message_without_attachments_does_not_create_job(self):
        processor = self.processor()
        message = NormalizedEmailMessage(
            metadata=self.message().metadata,
            attachments=(),
        )

        result = processor.process_email_message(message, work_dir=self.root / "email")

        self.assertIsNone(result.submitted_job_id)
        self.assertEqual(self.store.jobs, {})

    def test_same_message_and_attachment_are_idempotent(self):
        processor = self.processor()
        message = self.message()

        with patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()):
            first = processor.process_email_message(message, work_dir=self.root / "email")
            second = processor.process_email_message(message, work_dir=self.root / "email")

        self.assertIsNotNone(first.submitted_job_id)
        self.assertIsNone(second.submitted_job_id)
        self.assertEqual(second.duplicate[0].reason, "email_attachment_already_processed")
        self.assertEqual(len(self.store.jobs), 1)

    def test_two_tenants_do_not_share_email_intake_state(self):
        message = self.message()
        processor_a = self.processor(tenant_id="tenant_email_a", user_id="user_a")
        processor_b = self.processor(tenant_id="tenant_email_b", user_id="user_b")

        with patch("agents.document.agent.extract_with_gemini", side_effect=[self.gemini_payload("EMAIL-A"), self.gemini_payload("EMAIL-B")]):
            result_a = processor_a.process_email_message(message, work_dir=self.root / "email")
            result_b = processor_b.process_email_message(message, work_dir=self.root / "email")

        self.assertIsNotNone(result_a.submitted_job_id)
        self.assertIsNotNone(result_b.submitted_job_id)
        self.assertEqual(self.store.jobs[result_a.submitted_job_id].tenant_id, "tenant_email_a")
        self.assertEqual(self.store.jobs[result_b.submitted_job_id].tenant_id, "tenant_email_b")

    def test_email_audit_events_are_recorded_without_email_content(self):
        processor = self.processor()

        with patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()):
            result = processor.process_email_message(self.message(), work_dir=self.root / "email")

        events = self.store.events_for_job(result.submitted_job_id)
        event_types = [event.event_type for event in events]
        self.assertIn("EMAIL_RECEIVED", event_types)
        self.assertIn("EMAIL_ATTACHMENT_ACCEPTED", event_types)
        self.assertIn("EMAIL_ATTACHMENT_SUBMITTED", event_types)
        serialized = " ".join(str(event.to_dict()) for event in events)
        self.assertNotIn(self.invoice_text("EMAIL-001"), serialized)

    def test_mixed_email_records_rejected_attachment_on_created_job(self):
        processor = self.processor()
        valid = self.message().attachments[0]
        invalid = EmailAttachment(
            attachment_id="att-invalid",
            file_name="notes.txt",
            content_type="text/plain",
            size_bytes=5,
            content=b"notes",
        )
        message = NormalizedEmailMessage(metadata=self.message().metadata, attachments=(valid, invalid))

        with patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()):
            result = processor.process_email_message(message, work_dir=self.root / "email")

        self.assertIsNotNone(result.submitted_job_id)
        self.assertEqual(result.rejected[0].reason, "unsupported_attachment_type")
        events = [event.event_type for event in self.store.events_for_job(result.submitted_job_id)]
        self.assertIn("EMAIL_ATTACHMENT_REJECTED", events)


if __name__ == "__main__":
    unittest.main()
