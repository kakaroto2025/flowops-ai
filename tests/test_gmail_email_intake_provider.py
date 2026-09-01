from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.api.processor import JobProcessor
from shared.models import AuthContext, LocalStore
from tools.email_intake import (
    GmailAttachmentFetchError,
    GmailEmailIntakeProvider,
    GmailMalformedPayloadError,
    GmailMessageNotFoundError,
)


class FakeGmailClient:
    def __init__(self, messages: dict[str, dict] | None = None, attachments: dict[tuple[str, str], dict] | None = None):
        self.messages = messages or {}
        self.attachments = attachments or {}
        self.message_fetches: list[str] = []
        self.attachment_fetches: list[tuple[str, str]] = []

    def fetch_message(self, message_id: str) -> dict | None:
        self.message_fetches.append(message_id)
        return self.messages.get(message_id)

    def fetch_attachment(self, message_id: str, attachment_id: str) -> dict | None:
        self.attachment_fetches.append((message_id, attachment_id))
        return self.attachments.get((message_id, attachment_id))


class GmailEmailIntakeProviderTests(unittest.TestCase):
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

    def encoded(self, content: bytes = b"%PDF-flowops-test") -> str:
        return base64.urlsafe_b64encode(content).decode("ascii").rstrip("=")

    def gmail_message(self, *, message_id: str = "gmail-msg-001", attachment_id: str = "gmail-att-001") -> dict:
        return {
            "id": message_id,
            "threadId": "thread-abc",
            "internalDate": "1788288000000",
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [
                    {"name": "From", "value": "sender@example.test"},
                    {"name": "To", "value": "invoices@example.test, ops@example.test"},
                    {"name": "Subject", "value": "Invoice attached"},
                    {"name": "Date", "value": "Tue, 01 Sep 2026 12:00:00 +0000"},
                ],
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": self.encoded(b"hello")}},
                    {
                        "filename": "invoice.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": attachment_id, "size": 18},
                    },
                ],
            },
        }

    def nested_gmail_message(self) -> dict:
        message = self.gmail_message()
        message["payload"]["parts"] = [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": self.encoded(b"plain")}},
                    {"mimeType": "text/html", "body": {"data": self.encoded(b"<p>html</p>")}},
                ],
            },
            {
                "mimeType": "multipart/related",
                "parts": [
                    {"filename": "logo.png", "mimeType": "image/png", "body": {"data": self.encoded(b"png")}},
                    {
                        "filename": "nested.pdf",
                        "mimeType": "application/pdf",
                        "body": {"attachmentId": "gmail-att-nested", "size": 16},
                    },
                ],
            },
        ]
        return message

    def invoice_text(self) -> bytes:
        return "\n".join(
            [
                "Empresa: Gmail Intake Test Ltda",
                "CNPJ: 12.345.678/0001-90",
                "NF: GMAIL-001",
                "Data: 14/08/2026",
                "Valor Total: R$ 100,00",
            ]
        ).encode("utf-8")

    def gemini_payload(self) -> dict:
        return {
            "document_type": "invoice",
            "cnpj": "12.345.678/0001-90",
            "company_name": "Gmail Intake Test Ltda",
            "invoice_number": "GMAIL-001",
            "issue_date": "14/08/2026",
            "total_amount": 100.0,
            "confidence": 0.98,
            "warnings": [],
            "gemini_usage_metadata": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
        }

    def test_simple_gmail_message_with_pdf_attachment_maps_correctly(self):
        client = FakeGmailClient(
            messages={"gmail-msg-001": self.gmail_message()},
            attachments={("gmail-msg-001", "gmail-att-001"): {"data": self.encoded(b"%PDF-flowops-test")}},
        )
        provider = GmailEmailIntakeProvider(client, ["gmail-msg-001"])

        message = provider.list_messages()[0]

        self.assertEqual(message.metadata.provider_message_id, "gmail-msg-001")
        self.assertEqual(message.metadata.provider_thread_id, "thread-abc")
        self.assertEqual(message.metadata.sender, "sender@example.test")
        self.assertEqual(message.metadata.recipients, ("invoices@example.test", "ops@example.test"))
        self.assertEqual(message.metadata.subject, "Invoice attached")
        self.assertEqual(message.attachments[0].attachment_id, "gmail-att-001")
        self.assertEqual(message.attachments[0].file_name, "invoice.pdf")
        self.assertEqual(message.attachments[0].content_type, "application/pdf")
        self.assertEqual(message.attachments[0].content, b"%PDF-flowops-test")

    def test_nested_multipart_message_finds_pdf_attachment(self):
        client = FakeGmailClient(
            messages={"gmail-msg-001": self.nested_gmail_message()},
            attachments={("gmail-msg-001", "gmail-att-nested"): {"data": self.encoded(b"%PDF-nested-test")}},
        )

        message = GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()[0]

        self.assertEqual([attachment.file_name for attachment in message.attachments], ["logo.png", "nested.pdf"])
        self.assertEqual(message.attachments[1].attachment_id, "gmail-att-nested")
        self.assertEqual(message.attachments[1].content, b"%PDF-nested-test")

    def test_message_id_and_attachment_id_map_to_existing_idempotency_identifiers(self):
        client = FakeGmailClient(
            messages={"gmail-msg-123": self.gmail_message(message_id="gmail-msg-123", attachment_id="gmail-att-999")},
            attachments={("gmail-msg-123", "gmail-att-999"): {"data": self.encoded()}},
        )

        message = GmailEmailIntakeProvider(client, ["gmail-msg-123"]).list_messages()[0]

        self.assertEqual(message.metadata.provider_message_id, "gmail-msg-123")
        self.assertEqual(message.attachments[0].attachment_id, "gmail-att-999")

    def test_missing_optional_headers_do_not_crash(self):
        raw = self.gmail_message()
        raw["payload"]["headers"] = [{"name": "FROM", "value": "sender@example.test"}]
        client = FakeGmailClient(
            messages={"gmail-msg-001": raw},
            attachments={("gmail-msg-001", "gmail-att-001"): {"data": self.encoded()}},
        )

        message = GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()[0]

        self.assertEqual(message.metadata.sender, "sender@example.test")
        self.assertEqual(message.metadata.recipients, ())
        self.assertEqual(message.metadata.subject, "")
        self.assertTrue(message.metadata.received_at)

    def test_base64url_inline_attachment_decoding_works(self):
        raw = self.gmail_message()
        raw["payload"]["parts"][1]["body"] = {"data": self.encoded(b"%PDF-inline"), "size": 11}
        client = FakeGmailClient(messages={"gmail-msg-001": raw})

        message = GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()[0]

        self.assertEqual(message.attachments[0].content, b"%PDF-inline")
        self.assertEqual(client.attachment_fetches, [])
        self.assertTrue(message.attachments[0].attachment_id.startswith("inline:"))

    def test_invalid_base64url_produces_controlled_failure(self):
        raw = self.gmail_message()
        raw["payload"]["parts"][1]["body"] = {"data": "not valid ***", "size": 10}
        client = FakeGmailClient(messages={"gmail-msg-001": raw})

        with self.assertRaisesRegex(GmailMalformedPayloadError, "base64url"):
            GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()

    def test_message_not_found_is_controlled(self):
        with self.assertRaisesRegex(GmailMessageNotFoundError, "missing-msg"):
            GmailEmailIntakeProvider(FakeGmailClient(), ["missing-msg"]).list_messages()

    def test_missing_message_id_is_controlled(self):
        raw = self.gmail_message()
        raw.pop("id")
        client = FakeGmailClient(messages={"gmail-msg-001": raw})

        with self.assertRaisesRegex(GmailMalformedPayloadError, "missing id"):
            GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()

    def test_malformed_payload_is_controlled(self):
        raw = self.gmail_message()
        raw["payload"] = None
        client = FakeGmailClient(messages={"gmail-msg-001": raw})

        with self.assertRaisesRegex(GmailMalformedPayloadError, "payload"):
            GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()

    def test_attachment_fetch_failure_is_controlled(self):
        client = FakeGmailClient(messages={"gmail-msg-001": self.gmail_message()})

        with self.assertRaisesRegex(GmailAttachmentFetchError, "gmail-att-001"):
            GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()

    def test_non_pdf_attachment_integrates_with_existing_validation(self):
        raw = self.gmail_message()
        raw["payload"]["parts"][1] = {
            "filename": "notes.txt",
            "mimeType": "text/plain",
            "body": {"attachmentId": "gmail-att-text", "size": 5},
        }
        client = FakeGmailClient(
            messages={"gmail-msg-001": raw},
            attachments={("gmail-msg-001", "gmail-att-text"): {"data": self.encoded(b"notes")}},
        )
        message = GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()[0]
        processor = JobProcessor(self.store, AuthContext("user_email", "tenant_email", authenticated=True))

        result = processor.process_email_message(message, work_dir=self.root / "email")

        self.assertIsNone(result.submitted_job_id)
        self.assertEqual(result.rejected[0].reason, "unsupported_attachment_type")

    def test_provider_never_creates_tenant_context_from_email_data(self):
        raw = self.gmail_message()
        raw["payload"]["headers"].append({"name": "X-Tenant-Id", "value": "tenant_attacker"})
        client = FakeGmailClient(
            messages={"gmail-msg-001": raw},
            attachments={("gmail-msg-001", "gmail-att-001"): {"data": self.encoded()}},
        )

        message = GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()[0]

        self.assertFalse(hasattr(message, "tenant_id"))
        self.assertNotIn("tenant_attacker", str(message.to_dict()))

    def test_gmail_adapter_integrates_with_email_intake_service_and_pipeline(self):
        client = FakeGmailClient(
            messages={"gmail-msg-001": self.gmail_message()},
            attachments={("gmail-msg-001", "gmail-att-001"): {"data": self.encoded(self.invoice_text())}},
        )
        provider = GmailEmailIntakeProvider(client, ["gmail-msg-001"])
        processor = JobProcessor(self.store, AuthContext("user_email", "tenant_email", authenticated=True))

        with patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()):
            result = processor.process_email_provider(provider, work_dir=self.root / "email")[0]

        self.assertEqual(result.dashboard["job"]["source"], "email_intake")
        self.assertEqual(result.dashboard["documents"][0]["status"], "REGISTERED")
        self.assertEqual(self.store.jobs[result.submitted_job_id].tenant_id, "tenant_email")

    def test_gmail_message_and_attachment_ids_reuse_email_idempotency(self):
        client = FakeGmailClient(
            messages={"gmail-msg-001": self.gmail_message()},
            attachments={("gmail-msg-001", "gmail-att-001"): {"data": self.encoded(self.invoice_text())}},
        )
        provider = GmailEmailIntakeProvider(client, ["gmail-msg-001"])
        processor = JobProcessor(self.store, AuthContext("user_email", "tenant_email", authenticated=True))

        with patch("agents.document.agent.extract_with_gemini", return_value=self.gemini_payload()):
            first = processor.process_email_provider(provider, work_dir=self.root / "email")[0]
            second = processor.process_email_provider(provider, work_dir=self.root / "email")[0]

        self.assertIsNotNone(first.submitted_job_id)
        self.assertIsNone(second.submitted_job_id)
        self.assertEqual(second.duplicate[0].reason, "email_attachment_already_processed")
        self.assertEqual(len(self.store.jobs), 1)

    def test_two_tenants_remain_isolated_with_same_fake_gmail_provider(self):
        client = FakeGmailClient(
            messages={"gmail-msg-001": self.gmail_message()},
            attachments={("gmail-msg-001", "gmail-att-001"): {"data": self.encoded(self.invoice_text())}},
        )
        provider = GmailEmailIntakeProvider(client, ["gmail-msg-001"])
        processor_a = JobProcessor(self.store, AuthContext("user_a", "tenant_email_a", authenticated=True))
        processor_b = JobProcessor(self.store, AuthContext("user_b", "tenant_email_b", authenticated=True))

        with patch("agents.document.agent.extract_with_gemini", side_effect=[self.gemini_payload(), self.gemini_payload()]):
            result_a = processor_a.process_email_provider(provider, work_dir=self.root / "email")[0]
            result_b = processor_b.process_email_provider(provider, work_dir=self.root / "email")[0]

        self.assertIsNotNone(result_a.submitted_job_id)
        self.assertIsNotNone(result_b.submitted_job_id)
        self.assertEqual(self.store.jobs[result_a.submitted_job_id].tenant_id, "tenant_email_a")
        self.assertEqual(self.store.jobs[result_b.submitted_job_id].tenant_id, "tenant_email_b")

    def test_fake_client_is_the_only_gmail_boundary_called(self):
        client = FakeGmailClient(
            messages={"gmail-msg-001": self.gmail_message()},
            attachments={("gmail-msg-001", "gmail-att-001"): {"data": self.encoded()}},
        )

        GmailEmailIntakeProvider(client, ["gmail-msg-001"]).list_messages()

        self.assertEqual(client.message_fetches, ["gmail-msg-001"])
        self.assertEqual(client.attachment_fetches, [("gmail-msg-001", "gmail-att-001")])


if __name__ == "__main__":
    unittest.main()
