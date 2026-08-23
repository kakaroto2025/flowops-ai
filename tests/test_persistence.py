from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.api.processor import JobProcessor
from shared.models import Job, LocalStore


class LocalStorePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_normal_save_reloads_existing_data(self):
        store = LocalStore(self.state_path)
        job = Job(id=store.next_id("job"), source="unit_test")
        store.add_job(job)

        reloaded = LocalStore(self.state_path)

        self.assertIn(job.id, reloaded.jobs)
        self.assertEqual(reloaded.jobs[job.id].source, "unit_test")

    def test_multiple_sequential_writes_leave_valid_final_state(self):
        store = LocalStore(self.state_path)
        for index in range(10):
            store.add_job(Job(id=store.next_id("job"), source=f"write_{index}"))

        reloaded = LocalStore(self.state_path)

        self.assertEqual(len(reloaded.jobs), 10)
        self.assertEqual(reloaded.jobs["job_000010"].source, "write_9")

    def test_backup_is_created_when_previous_state_exists(self):
        store = LocalStore(self.state_path)
        store.add_job(Job(id=store.next_id("job"), source="first"))
        store.add_job(Job(id=store.next_id("job"), source="second"))

        backup_path = self.state_path.with_name("state.json.bak")
        backup_store = LocalStore(backup_path)

        self.assertTrue(backup_path.exists())
        self.assertIn("job_000001", backup_store.jobs)

    def test_invalid_state_recovers_from_valid_backup(self):
        store = LocalStore(self.state_path)
        store.add_job(Job(id=store.next_id("job"), source="recoverable"))
        store.add_job(Job(id=store.next_id("job"), source="latest_after_backup"))
        self.state_path.write_text("{invalid json", encoding="utf-8")

        reloaded = LocalStore(self.state_path)

        self.assertIn("job_000001", reloaded.jobs)
        self.assertEqual(reloaded.jobs["job_000001"].source, "recoverable")

    def test_operational_record_survives_reload_and_duplicate_still_blocks(self):
        docs_dir = self.root / "docs"
        docs_dir.mkdir()
        first = docs_dir / "NF_PERSIST_OPERACIONAL.pdf"
        first.write_text(
            "Empresa: Persist Operacional Ltda\n"
            "CNPJ: 12.345.678/0001-90\n"
            "NF: PERSIST-OP-001\n"
            "Data: 14/08/2026\n"
            "Valor Total: R$ 100,00",
            encoding="utf-8",
        )
        duplicate = docs_dir / "NF_PERSIST_OPERACIONAL_DUP.pdf"
        duplicate.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
        gemini_payload = {
            "document_type": "invoice",
            "cnpj": "12.345.678/0001-90",
            "company_name": "Persist Operacional Ltda",
            "invoice_number": "PERSIST-OP-001",
            "issue_date": "14/08/2026",
            "total_amount": 100.0,
            "confidence": 0.98,
            "warnings": [],
        }

        store = LocalStore(self.state_path)
        processor = JobProcessor(store)
        with (
            patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"),
            patch("agents.document.agent.extract_with_gemini", return_value=gemini_payload),
        ):
            first_dashboard = processor.run_job(processor.create_upload_job([first]).id)

        reloaded = LocalStore(self.state_path)
        self.assertEqual(len(reloaded.erp_records), 1)
        self.assertEqual(first_dashboard["documents"][0]["status"], "REGISTERED")

        processor_after_reload = JobProcessor(reloaded)
        with (
            patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"),
            patch("agents.document.agent.extract_with_gemini", return_value=gemini_payload),
        ):
            duplicate_dashboard = processor_after_reload.run_job(
                processor_after_reload.create_upload_job([duplicate]).id
            )

        self.assertEqual(len(reloaded.erp_records), 1)
        self.assertEqual(duplicate_dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        event_types = [event.event_type for event in reloaded.events_for_job(duplicate_dashboard["job"]["id"])]
        self.assertIn("DUPLICATE_DETECTED", event_types)


if __name__ == "__main__":
    unittest.main()
