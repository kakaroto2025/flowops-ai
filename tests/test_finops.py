from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api import main as api_main
from apps.api.processor import JobProcessor
from shared.models import LocalStore
from tools.documents.gemini_extractor import GeminiExtractionError
from tools.documents.gemini_extractor import _extract_usage_metadata
from tools.finops import CostGuard, FinOpsConfig, GeminiPricing, UsageRecord, UsageTracker


class _FakeUsageMetadata:
    prompt_token_count = 12
    candidates_token_count = 8
    total_token_count = 20


class FinOpsCostGuardTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "SUA_CHAVE_REAL_AQUI",
                "FREE_TIER_FIRST": "true",
                "FLOWOPS_DAILY_DOCUMENT_LIMIT": "50",
                "FLOWOPS_DAILY_GEMINI_CALL_LIMIT": "100",
                "FLOWOPS_MAX_FILE_SIZE_MB": "10",
                "FLOWOPS_MONTHLY_SOFT_BUDGET_BRL": "50",
                "FLOWOPS_USD_BRL_RATE": "",
                "FLOWOPS_GEMINI_INPUT_PRICE_PER_MILLION_TOKENS": "",
                "FLOWOPS_GEMINI_OUTPUT_PRICE_PER_MILLION_TOKENS": "",
            },
        )
        self.env_patch.start()
        self.adk_patch = patch(
            "agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime",
            return_value="READY",
        )
        self.adk_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalStore(Path(self.tmp.name) / "state.json")

    def tearDown(self):
        self.adk_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def _processor(self) -> JobProcessor:
        return JobProcessor(self.store)

    def _invoice(self, name: str = "NF_FINOPS.pdf") -> Path:
        path = Path(self.tmp.name) / name
        path.write_text(
            "\n".join(
                [
                    "Empresa: FinOps Test Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "NF: FIN001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def _gemini_payload(self) -> dict:
        return {
            "document_type": "invoice",
            "cnpj": "12.345.678/0001-90",
            "company_name": "FinOps Test Ltda",
            "invoice_number": "FIN-001",
            "issue_date": "14/08/2026",
            "total_amount": 100.0,
            "confidence": 0.98,
            "warnings": [],
            "gemini_usage_metadata": {"input_tokens": 120, "output_tokens": 40, "total_tokens": 160},
        }

    def test_finops_allows_valid_document_and_records_usage(self):
        processor = self._processor()
        with patch("agents.document.agent.extract_with_gemini", return_value=self._gemini_payload()):
            job = processor.create_upload_job([self._invoice()])
            dashboard = processor.run_job(job.id)

        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        self.assertEqual(len(self.store.finops_usage_records), 1)
        record = next(iter(self.store.finops_usage_records.values()))
        self.assertTrue(record.gemini_used)
        self.assertEqual(record.gemini_calls, 1)
        self.assertEqual(record.input_tokens, 120)
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("FINOPS_ALLOW", event_types)
        self.assertIn("GEMINI_USAGE_RECORDED", event_types)
        self.assertIn("FINOPS_USAGE_RECORDED", event_types)

    def test_file_size_limit_blocks_before_gemini(self):
        with patch.dict(os.environ, {"FLOWOPS_MAX_FILE_SIZE_MB": "0"}):
            processor = self._processor()
            with patch("agents.document.agent.extract_with_gemini") as gemini:
                job = processor.create_upload_job([self._invoice("NF_TOO_LARGE.pdf")])
                dashboard = processor.run_job(job.id)

        self.assertEqual(dashboard["documents"][0]["status"], "FAILED")
        gemini.assert_not_called()
        record = next(iter(self.store.finops_usage_records.values()))
        self.assertTrue(record.blocked_by_cost_guard)
        self.assertEqual(record.block_reason, "file_size_limit_exceeded")

    def test_daily_document_limit_blocks_next_document(self):
        self.store.add_finops_usage_record(UsageRecord(id="usage_existing", job_id="job_x", document_id="doc_x"))
        with patch.dict(os.environ, {"FLOWOPS_DAILY_DOCUMENT_LIMIT": "1"}):
            processor = self._processor()
            job = processor.create_upload_job([self._invoice("NF_DOC_LIMIT.pdf")])
            dashboard = processor.run_job(job.id)

        self.assertEqual(dashboard["documents"][0]["status"], "FAILED")
        blocked = [record for record in self.store.finops_usage_records.values() if record.document_id != "doc_x"][0]
        self.assertEqual(blocked.block_reason, "daily_document_limit_exceeded")

    def test_daily_gemini_limit_uses_local_parser_fallback(self):
        self.store.add_finops_usage_record(
            UsageRecord(id="usage_existing", job_id="job_x", document_id="doc_x", gemini_calls=1)
        )
        with patch.dict(os.environ, {"FLOWOPS_DAILY_GEMINI_CALL_LIMIT": "1"}):
            processor = self._processor()
            with patch("agents.document.agent.extract_with_gemini") as gemini:
                job = processor.create_upload_job([self._invoice("NF_GEMINI_LIMIT.pdf")])
                dashboard = processor.run_job(job.id)

        gemini.assert_not_called()
        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        record = [item for item in self.store.finops_usage_records.values() if item.document_id != "doc_x"][0]
        self.assertFalse(record.gemini_used)
        self.assertEqual(record.gemini_calls, 0)
        self.assertTrue(record.parser_fallback_used)
        self.assertEqual(record.block_reason, "daily_gemini_limit_exceeded")

    def test_failed_gemini_call_counts_attempt_and_uses_fallback(self):
        processor = self._processor()
        with patch(
            "agents.document.agent.extract_with_gemini",
            side_effect=GeminiExtractionError("gemini_request_failed:ClientError"),
        ):
            job = processor.create_upload_job([self._invoice("NF_GEMINI_FAIL.pdf")])
            dashboard = processor.run_job(job.id)

        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        record = next(iter(self.store.finops_usage_records.values()))
        self.assertFalse(record.gemini_used)
        self.assertEqual(record.gemini_calls, 1)
        self.assertTrue(record.parser_fallback_used)

    def test_usage_metadata_is_recorded_only_when_available(self):
        self.assertEqual(
            _extract_usage_metadata(_FakeUsageMetadata()),
            {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
        )
        self.assertEqual(_extract_usage_metadata(None), {})

    def test_pricing_is_null_without_explicit_prices(self):
        pricing = GeminiPricing.from_env("gemini-3.6-flash")
        self.assertIsNone(pricing.estimate_usd(100, 50))

    def test_pricing_uses_configured_values_without_external_calls(self):
        with patch.dict(
            os.environ,
            {
                "FLOWOPS_GEMINI_INPUT_PRICE_PER_MILLION_TOKENS": "0.10",
                "FLOWOPS_GEMINI_OUTPUT_PRICE_PER_MILLION_TOKENS": "0.40",
            },
        ):
            pricing = GeminiPricing.from_env("gemini-3.6-flash")

        self.assertEqual(pricing.estimate_usd(1_000_000, 500_000), 0.3)

    def test_brl_cost_stays_null_without_manual_fx_rate(self):
        with patch.dict(
            os.environ,
            {
                "FLOWOPS_GEMINI_INPUT_PRICE_PER_MILLION_TOKENS": "0.10",
                "FLOWOPS_GEMINI_OUTPUT_PRICE_PER_MILLION_TOKENS": "0.40",
                "FLOWOPS_USD_BRL_RATE": "",
            },
        ):
            processor = self._processor()
            with patch("agents.document.agent.extract_with_gemini", return_value=self._gemini_payload()):
                job = processor.create_upload_job([self._invoice("NF_NO_FX.pdf")])
                processor.run_job(job.id)

        record = next(iter(self.store.finops_usage_records.values()))
        self.assertEqual(record.estimated_ai_cost_usd, 0.000028)
        self.assertIsNone(record.estimated_ai_cost_brl)

    def test_monthly_soft_budget_blocks_gemini_but_keeps_processing(self):
        self.store.add_finops_usage_record(
            UsageRecord(id="usage_existing", job_id="job_x", document_id="doc_x", estimated_ai_cost_brl=50.0)
        )
        processor = self._processor()
        with patch("agents.document.agent.extract_with_gemini") as gemini:
            job = processor.create_upload_job([self._invoice("NF_SOFT_BUDGET.pdf")])
            dashboard = processor.run_job(job.id)

        gemini.assert_not_called()
        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        record = [item for item in self.store.finops_usage_records.values() if item.document_id != "doc_x"][0]
        self.assertEqual(record.block_reason, "monthly_soft_budget_reached")

    def test_usage_summary_aggregates_limits_and_costs(self):
        tracker = UsageTracker(self.store, FinOpsConfig(daily_document_limit=3, daily_gemini_call_limit=4))
        tracker.record_usage(
            UsageRecord(
                id="usage_a",
                job_id="job_a",
                document_id="doc_a",
                gemini_calls=1,
                input_tokens=10,
                output_tokens=5,
                estimated_ai_cost_usd=0.01,
                estimated_ai_cost_brl=0.05,
            )
        )
        tracker.record_usage(
            UsageRecord(
                id="usage_b",
                job_id="job_b",
                document_id="doc_b",
                gemini_calls=2,
                input_tokens=20,
                output_tokens=15,
                estimated_ai_cost_usd=0.03,
                estimated_ai_cost_brl=0.15,
            )
        )

        summary = tracker.get_usage_summary()

        self.assertEqual(summary["documents_today"], 2)
        self.assertEqual(summary["gemini_calls_today"], 3)
        self.assertEqual(summary["input_tokens_today"], 30)
        self.assertEqual(summary["output_tokens_today"], 20)
        self.assertEqual(summary["estimated_ai_cost_today_usd"], 0.04)
        self.assertEqual(summary["estimated_ai_cost_today_brl"], 0.2)
        self.assertEqual(summary["daily_document_limit"], 3)
        self.assertEqual(summary["daily_gemini_call_limit"], 4)

    def test_finops_usage_endpoint_is_read_only_and_secret_free(self):
        original_store = api_main.store
        original_processor = api_main.processor
        api_main.store = self.store
        api_main.processor = self._processor()
        try:
            client = TestClient(api_main.app)
            response = client.get("/api/finops/usage")
        finally:
            api_main.store = original_store
            api_main.processor = original_processor

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("free_tier_first", payload)
        forbidden = {"GEMINI_API_KEY", "api_key", "secret", "credentials", "billing_account"}
        self.assertTrue(forbidden.isdisjoint(payload.keys()))

    def test_finops_frontend_and_docs_are_present(self):
        html = Path("apps/web/index.html").read_text(encoding="utf-8")
        app_js = Path("apps/web/app.js").read_text(encoding="utf-8")
        docs = Path("docs/pilot_v2_finops.md").read_text(encoding="utf-8")

        self.assertIn("Pilot v2 - Cost Guard", html)
        self.assertIn('request("/api/finops/usage")', app_js)
        self.assertIn("FREE_TIER_FIRST=true", docs)
        self.assertIn("does not create a hard spending cap", docs)


if __name__ == "__main__":
    unittest.main()
