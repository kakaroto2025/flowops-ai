import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from apps.api import main as api_main
from apps.api.processor import JobProcessor
from shared.models import LocalStore
from tools.documents.gemini_extractor import GeminiExtractionError
from tools.documents.extractor import extract_document
from tools.reporting import build_dashboard, build_global_erp_records, build_global_human_review_queue, build_job_history
from tools.validation.rules import validate_extraction, validate_invoice_number


class VerticalSliceTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {"GEMINI_API_KEY": "SUA_CHAVE_REAL_AQUI"})
        self.env_patch.start()
        self.adk_patch = patch(
            "agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime",
            return_value="READY",
        )
        self.adk_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalStore(Path(self.tmp.name) / "state.json")
        self.processor = JobProcessor(self.store)

    def tearDown(self):
        self.adk_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_demo_job_processes_documents_and_generates_dashboard(self):
        job = self.processor.create_demo_job()
        dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["job"]["document_count"], 5)
        self.assertGreaterEqual(dashboard["kpis"]["documents_processed"], 5)
        self.assertGreaterEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertGreaterEqual(len(dashboard["recent_events"]), 5)
        self.assertGreaterEqual(len(dashboard["documents"]), 5)
        self.assertGreaterEqual(len(dashboard["human_reviews"]), 1)

    def test_human_review_can_be_resolved(self):
        job = self.processor.create_demo_job()
        self.processor.run_job(job.id)
        open_reviews = [review for review in self.store.reviews_for_job(job.id) if review.status == "OPEN"]
        self.assertTrue(open_reviews)

        dashboard = self.processor.resolve_review(
            open_reviews[0].id,
            {
                "cnpj": "33.444.555/0001-66",
                "company_name": "Gamma Revisada Ltda",
                "invoice_number": "99100",
                "issue_date": "2026-08-11",
                "total_amount": 3100.0,
            },
        )

        self.assertGreaterEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(self.store.human_reviews[open_reviews[0].id].status, "RESOLVED")

    def test_dashboard_report_shape(self):
        job = self.processor.create_demo_job()
        self.processor.run_job(job.id)
        dashboard = build_dashboard(self.store, job.id)

        self.assertIn("kpis", dashboard)
        self.assertIn("status_counts", dashboard)
        self.assertIn("recent_events", dashboard)

    def test_uploaded_files_can_create_job(self):
        upload_dir = Path(self.tmp.name) / "uploads"
        upload_dir.mkdir()
        file_path = upload_dir / "NF_UPLOAD.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: Upload Teste Ltda",
                    "CNPJ: 44.555.666/0001-77",
                    "NF: UP-001",
                    "Data: 2026-08-14",
                    "Valor Total: R$ 450,25",
                ]
            ),
            encoding="utf-8",
        )

        job = self.processor.create_upload_job([file_path])
        dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["job"]["source"], "manual_upload")
        self.assertEqual(dashboard["kpis"]["documents_processed"], 1)
        self.assertEqual(dashboard["kpis"]["erp_records"], 1)

    def test_binary_pdf_upload_is_extracted(self):
        pdf_path = Path(self.tmp.name) / "NF_REAL.pdf"
        pdf = canvas.Canvas(str(pdf_path))
        pdf.drawString(72, 760, "Empresa: PDF Real Ltda")
        pdf.drawString(72, 740, "CNPJ: 55.666.777/0001-88")
        pdf.drawString(72, 720, "NF: REAL-001")
        pdf.drawString(72, 700, "Data: 2026-08-14")
        pdf.drawString(72, 680, "Valor Total: R$ 1.234,56")
        pdf.save()

        job = self.processor.create_upload_job([pdf_path])
        dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["kpis"]["documents_processed"], 1)
        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        record = dashboard["erp_records"][0]
        self.assertEqual(record["invoice_number"], "REAL-001")
        self.assertEqual(record["cnpj"], "55.666.777/0001-88")

    def test_invoice_layout_with_labels_on_next_line_is_extracted(self):
        pdf_path = Path(self.tmp.name) / "NF_TESTE_ORION_001.pdf"
        pdf = canvas.Canvas(str(pdf_path))
        y = 760
        for line in [
            "NOTA FISCAL ELETRONICA - TESTE",
            "N 000145781",
            "EMITENTE",
            "Razao Social",
            "ORION TECNOLOGIA EMPRESARIAL LTDA",
            "CNPJ",
            "31.415.926/0001-71",
            "Data de Emissao",
            "14/08/2026",
            "Numero da Nota",
            "000145781",
            "DESTINATARIO",
            "Razao Social",
            "NOVA ERA COMERCIO E SERVICOS LTDA",
            "CNPJ destinatario",
            "45.678.901/0001-55",
            "Valor Total",
            "R$ 12.600,00",
            "VALOR TOTAL DA NOTA",
            "R$ 13.950,00",
        ]:
            pdf.drawString(72, y, line)
            y -= 18
        pdf.save()

        job = self.processor.create_upload_job([pdf_path])
        dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(dashboard["kpis"]["human_reviews"], 0)
        extraction = self.store.extraction_for_document(dashboard["documents"][0]["id"])
        self.assertIsNotNone(extraction)
        self.assertEqual(extraction.company_name, "ORION TECNOLOGIA EMPRESARIAL LTDA")
        self.assertEqual(extraction.cnpj, "31.415.926/0001-71")
        self.assertEqual(extraction.invoice_number, "000145781")
        self.assertEqual(extraction.issue_date, "14/08/2026")
        self.assertEqual(extraction.total_amount, 13950.0)

    def test_nf_ficticia_invoice_number_is_not_misread_as_ta(self):
        pdf_path = Path("local_data/uploads/upload_000006/NF_FICTICIA_FLOWOPS_001.pdf")
        if not pdf_path.exists():
            self.skipTest("NF_FICTICIA_FLOWOPS_001.pdf fixture not available")

        result = extract_document(pdf_path)

        self.assertEqual(result["invoice_number"], "000098342")

    def test_numero_da_nota_symbol_label_on_next_line_is_extracted(self):
        pdf_path = Path(self.tmp.name) / "01_NF_VALIDA_CONTROLE.pdf"
        pdf = canvas.Canvas(str(pdf_path))
        y = 760
        for line in [
            "NOTA FISCAL ELETRONICA - TESTE FLOWOPS",
            "Razao Social",
            "NEXA SERVICOS DIGITAIS LTDA",
            "CNPJ",
            "12.345.678/0001-95",
            "Nº da Nota",
            "000501234",
            "Data de Emissao",
            "19/08/2026",
            "Valor Total",
            "R$ 8.750,00",
        ]:
            pdf.drawString(72, y, line)
            y -= 20
        pdf.save()

        result = extract_document(pdf_path)

        self.assertEqual(result["invoice_number"], "000501234")

    def test_invalid_invoice_number_tokens_are_rejected(self):
        for value in ("TA", "NF", "NOTA", "NFE"):
            with self.subTest(value=value):
                self.assertFalse(validate_invoice_number(value))
                validation = validate_extraction(
                    {
                        "cnpj": "12.345.678/0001-90",
                        "company_name": "Empresa Teste Ltda",
                        "invoice_number": value,
                        "issue_date": "14/08/2026",
                        "total_amount": 100.0,
                        "confidence": 0.96,
                        "warnings": [],
                    }
                )
                self.assertIn("invalid_invoice_number", validation["errors"])
                self.assertTrue(validation["retry_recommended"])

    def test_invalid_invoice_number_retries_then_goes_to_human_review_without_erp(self):
        file_path = Path(self.tmp.name) / "NF_INVALIDA.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: Invalid Invoice Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "Nota: TA",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )

        job = self.processor.create_upload_job([file_path])
        dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["kpis"]["erp_records"], 0)
        self.assertEqual(dashboard["kpis"]["human_reviews"], 1)
        self.assertEqual(dashboard["documents"][0]["retry_count"], 1)
        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")

    def test_document_agent_uses_gemini_when_available(self):
        file_path = Path(self.tmp.name) / "NF_GEMINI.pdf"
        file_path.write_text("dummy text for gemini", encoding="utf-8")
        gemini_result = {
            "document_type": "invoice",
            "cnpj": "31.415.926/0001-71",
            "company_name": "ORION TECNOLOGIA EMPRESARIAL LTDA",
            "invoice_number": "000145781",
            "issue_date": "14/08/2026",
            "total_amount": 13950.0,
            "confidence": 0.98,
            "warnings": [],
        }

        with patch("agents.document.agent.extract_with_gemini", return_value=gemini_result) as gemini_call:
            job = self.processor.create_upload_job([file_path])
            dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(dashboard["kpis"]["human_reviews"], 0)
        self.assertEqual(dashboard["erp_records"][0]["invoice_number"], "000145781")
        self.assertEqual(gemini_call.call_count, 1)
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("GEMINI_EXTRACTION", event_types)

    def test_document_agent_falls_back_when_gemini_unavailable(self):
        file_path = Path(self.tmp.name) / "NF_FALLBACK.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: Fallback Teste Ltda",
                    "CNPJ: 44.555.666/0001-77",
                    "NF: FB-001",
                    "Data: 2026-08-14",
                    "Valor Total: R$ 450,25",
                ]
            ),
            encoding="utf-8",
        )

        with patch(
            "agents.document.agent.extract_with_gemini",
            side_effect=GeminiExtractionError("gemini_unavailable"),
        ):
            job = self.processor.create_upload_job([file_path])
            dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(dashboard["erp_records"][0]["invoice_number"], "FB-001")
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("LOCAL_PARSER_FALLBACK", event_types)

    def test_document_agent_falls_back_when_gemini_response_is_invalid(self):
        file_path = Path(self.tmp.name) / "NF_INVALID_GEMINI_RESPONSE.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: Invalid Gemini Response Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "NF: IGR-001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )

        with patch(
            "agents.document.agent.extract_with_gemini",
            side_effect=GeminiExtractionError("model_response_was_not_json"),
        ):
            job = self.processor.create_upload_job([file_path])
            dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(dashboard["erp_records"][0]["invoice_number"], "IGR-001")
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("LOCAL_PARSER_FALLBACK", event_types)

    def test_job_runs_through_adk_orchestrator(self):
        file_path = Path(self.tmp.name) / "NF_ADK.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: ADK Pipeline Ltda",
                    "CNPJ: 98.765.432/0001-10",
                    "NF: ADK-001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 321,00",
                ]
            ),
            encoding="utf-8",
        )

        job = self.processor.create_upload_job([file_path])
        dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("ADK_WORKFLOW_STARTED", event_types)
        self.assertIn("ADK_ORCHESTRATOR_READY", event_types)
        self.assertIn("ADK_WORKFLOW_COMPLETED", event_types)

    def test_adk_failure_is_recorded_safely(self):
        self.adk_patch.stop()
        file_path = Path(self.tmp.name) / "NF_ADK_FAIL.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: ADK Failure Ltda",
                    "CNPJ: 98.765.432/0001-10",
                    "NF: ADK-FAIL-001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 321,00",
                ]
            ),
            encoding="utf-8",
        )

        with patch(
            "agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime",
            side_effect=RuntimeError("simulated_adk_failure"),
        ):
            job = self.processor.create_upload_job([file_path])
            dashboard = self.processor.run_job(job.id)
        self.adk_patch.start()

        self.assertEqual(dashboard["job"]["status"], "FAILED")
        self.assertEqual(dashboard["kpis"]["erp_records"], 0)
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("ADK_WORKFLOW_STARTED", event_types)
        self.assertIn("ADK_WORKFLOW_FAILED", event_types)

    def test_adk_quota_error_continues_existing_agent_workflow(self):
        self.adk_patch.stop()
        file_path = Path(self.tmp.name) / "NF_ADK_QUOTA.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: ADK Quota Ltda",
                    "CNPJ: 98.765.432/0001-10",
                    "NF: ADK-Q-001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 321,00",
                ]
            ),
            encoding="utf-8",
        )

        with patch(
            "agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime",
            side_effect=RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded"),
        ):
            job = self.processor.create_upload_job([file_path])
            dashboard = self.processor.run_job(job.id)
        self.adk_patch.start()

        self.assertEqual(dashboard["job"]["status"], "COMPLETED")
        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("ADK_WORKFLOW_STARTED", event_types)
        self.assertIn("ADK_ORCHESTRATOR_UNAVAILABLE", event_types)
        self.assertIn("ADK_WORKFLOW_COMPLETED", event_types)
        self.assertNotIn("ADK_WORKFLOW_FAILED", event_types)

    def test_upload_endpoint_runs_adk_outside_async_event_loop(self):
        pdf_path = Path(self.tmp.name) / "01_NF_VALIDA_CONTROLE.pdf"
        pdf = canvas.Canvas(str(pdf_path))
        pdf.drawString(72, 760, "NOTA FISCAL ELETRONICA - TESTE FLOWOPS")
        pdf.drawString(72, 740, "Razao Social")
        pdf.drawString(72, 720, "NEXA SERVICOS DIGITAIS LTDA")
        pdf.drawString(72, 700, "CNPJ")
        pdf.drawString(72, 680, "12.345.678/0001-95")
        pdf.drawString(72, 660, "Numero da Nota")
        pdf.drawString(72, 640, "000501234")
        pdf.drawString(72, 620, "Data de Emissao")
        pdf.drawString(72, 600, "19/08/2026")
        pdf.drawString(72, 580, "Valor Total")
        pdf.drawString(72, 560, "R$ 8.750,00")
        pdf.save()

        original_store = api_main.store
        original_processor = api_main.processor
        api_main.store = self.store
        api_main.processor = self.processor
        try:
            client = TestClient(api_main.app)
            with pdf_path.open("rb") as handle:
                response = client.post(
                    "/jobs/upload/run",
                    files={"files": ("01_NF_VALIDA_CONTROLE.pdf", handle, "application/pdf")},
                )
        finally:
            api_main.store = original_store
            api_main.processor = original_processor

        self.assertEqual(response.status_code, 200)
        dashboard = response.json()
        self.assertEqual(dashboard["job"]["status"], "COMPLETED")
        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        event_types = [event.event_type for event in self.store.events_for_job(dashboard["job"]["id"])]
        self.assertIn("ADK_WORKFLOW_STARTED", event_types)
        self.assertIn("EXTRACTION_STARTED", event_types)
        self.assertIn("VALIDATION_COMPLETED", event_types)
        self.assertIn("DECISION_APPROVED", event_types)
        self.assertIn("ADK_WORKFLOW_COMPLETED", event_types)

    def test_job_history_keeps_multiple_processed_jobs(self):
        first = Path(self.tmp.name) / "NF_HISTORY_1.pdf"
        first.write_text(
            "\n".join(
                [
                    "Empresa: History One Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "NF: H-001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )
        second = Path(self.tmp.name) / "NF_HISTORY_2.pdf"
        second.write_text(
            "\n".join(
                [
                    "Empresa: History Two Ltda",
                    "CNPJ: 98.765.432/0001-10",
                    "NF: H-002",
                    "Data: 15/08/2026",
                    "Valor Total: R$ 200,00",
                ]
            ),
            encoding="utf-8",
        )

        first_job = self.processor.create_upload_job([first])
        first_dashboard = self.processor.run_job(first_job.id)
        second_job = self.processor.create_upload_job([second])
        second_dashboard = self.processor.run_job(second_job.id)
        history = build_job_history(self.store)

        self.assertEqual(len(history), 2)
        self.assertEqual({row["job_id"] for row in history}, {first_job.id, second_job.id})
        self.assertEqual(first_dashboard["documents"][0]["file_name"], "NF_HISTORY_1.pdf")
        self.assertEqual(second_dashboard["documents"][0]["file_name"], "NF_HISTORY_2.pdf")
        old_dashboard = build_dashboard(self.store, first_job.id)
        self.assertEqual(old_dashboard["documents"][0]["file_name"], "NF_HISTORY_1.pdf")
        self.assertGreaterEqual(len(old_dashboard["recent_events"]), 1)

    def test_jobs_endpoint_returns_history_summary(self):
        original_store = api_main.store
        original_processor = api_main.processor
        api_main.store = self.store
        api_main.processor = self.processor
        first = Path(self.tmp.name) / "NF_ENDPOINT_HISTORY_1.pdf"
        second = Path(self.tmp.name) / "NF_ENDPOINT_HISTORY_2.pdf"
        first.write_text(
            "Empresa: Endpoint One Ltda\nCNPJ: 12.345.678/0001-90\nNF: E-001\nData: 14/08/2026\nValor Total: R$ 100,00",
            encoding="utf-8",
        )
        second.write_text(
            "Empresa: Endpoint Two Ltda\nCNPJ: 98.765.432/0001-10\nNF: E-002\nData: 15/08/2026\nValor Total: R$ 200,00",
            encoding="utf-8",
        )
        try:
            self.processor.run_job(self.processor.create_upload_job([first]).id)
            self.processor.run_job(self.processor.create_upload_job([second]).id)
            client = TestClient(api_main.app)
            response = client.get("/jobs")
        finally:
            api_main.store = original_store
            api_main.processor = original_processor

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 2)
        self.assertIn("job_id", rows[0])
        self.assertIn("approved", rows[0])
        self.assertIn("human_reviews", rows[0])
        self.assertIn("rejected", rows[0])

    def test_dashboard_uses_versioned_app_asset(self):
        html = Path("apps/web/index.html").read_text(encoding="utf-8")

        self.assertIn('/assets/app.js?v=', html)

    def test_human_review_correction_approves_and_registers_erp(self):
        file_path = Path(self.tmp.name) / "NF_REVIEW_FIX.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: Review Fix Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "Nota: TA",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )

        job = self.processor.create_upload_job([file_path])
        dashboard = self.processor.run_job(job.id)
        review = self.store.reviews_for_job(job.id)[0]

        dashboard = self.processor.resolve_review(
            review.id,
            {
                "company_name": "Review Fix Ltda",
                "cnpj": "12.345.678/0001-90",
                "invoice_number": "000100",
                "issue_date": "14/08/2026",
                "total_amount": 100.0,
            },
            reviewer="unit_test",
        )

        self.assertEqual(dashboard["kpis"]["human_reviews"], 0)
        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(self.store.human_reviews[review.id].status, "RESOLVED")
        self.assertEqual(self.store.documents[review.document_id].status, "REGISTERED")
        self.assertEqual(dashboard["erp_records"][0]["invoice_number"], "000100")
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("HUMAN_REVIEW_CORRECTED", event_types)
        self.assertIn("HUMAN_REVIEW_APPROVED", event_types)

    def test_invalid_human_review_correction_stays_open_without_erp(self):
        file_path = Path(self.tmp.name) / "NF_REVIEW_STILL_INVALID.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: Review Invalid Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "Nota: TA",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )

        job = self.processor.create_upload_job([file_path])
        self.processor.run_job(job.id)
        review = self.store.reviews_for_job(job.id)[0]
        dashboard = self.processor.resolve_review(
            review.id,
            {
                "company_name": "Review Invalid Ltda",
                "cnpj": "12.345.678/0001-90",
                "invoice_number": "TA",
                "issue_date": "14/08/2026",
                "total_amount": 100.0,
            },
            reviewer="unit_test",
        )

        self.assertEqual(dashboard["kpis"]["human_reviews"], 1)
        self.assertEqual(dashboard["kpis"]["erp_records"], 0)
        self.assertEqual(self.store.human_reviews[review.id].status, "OPEN")
        self.assertEqual(self.store.documents[review.document_id].status, "HUMAN_REVIEW")
        self.assertIn("invalid_invoice_number", self.store.human_reviews[review.id].reason)

    def test_human_review_rejects_without_erp(self):
        file_path = Path(self.tmp.name) / "NF_REVIEW_REJECT.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: Review Reject Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "Nota: TA",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )

        job = self.processor.create_upload_job([file_path])
        self.processor.run_job(job.id)
        review = self.store.reviews_for_job(job.id)[0]
        dashboard = self.processor.reject_review(review.id, reviewer="unit_test")

        self.assertEqual(dashboard["kpis"]["human_reviews"], 0)
        self.assertEqual(dashboard["kpis"]["erp_records"], 0)
        self.assertEqual(self.store.human_reviews[review.id].status, "REJECTED")
        self.assertEqual(self.store.documents[review.document_id].status, "REJECTED")
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("HUMAN_REVIEW_REJECTED", event_types)

    def test_resolved_human_review_cannot_register_duplicate_erp(self):
        file_path = Path(self.tmp.name) / "NF_REVIEW_DUPLICATE.pdf"
        file_path.write_text(
            "\n".join(
                [
                    "Empresa: Review Duplicate Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "Nota: TA",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )

        job = self.processor.create_upload_job([file_path])
        self.processor.run_job(job.id)
        review = self.store.reviews_for_job(job.id)[0]
        corrected = {
            "company_name": "Review Duplicate Ltda",
            "cnpj": "12.345.678/0001-90",
            "invoice_number": "000200",
            "issue_date": "14/08/2026",
            "total_amount": 100.0,
        }

        self.processor.resolve_review(review.id, corrected, reviewer="unit_test")

        with self.assertRaises(ValueError):
            self.processor.resolve_review(review.id, corrected, reviewer="unit_test")
        self.assertEqual(len(self.store.erp_records_for_job(job.id)), 1)

    def test_duplicate_invoice_across_jobs_is_blocked_before_erp(self):
        first = Path(self.tmp.name) / "NF_ORIGINAL.pdf"
        duplicate = Path(self.tmp.name) / "NF_DUPLICATE_OTHER_NAME.pdf"
        content = "\n".join(
            [
                "Empresa: Duplicate Global Ltda",
                "CNPJ: 12.345.678/0001-90",
                "NF: DUP-001",
                "Data: 14/08/2026",
                "Valor Total: R$ 100,00",
            ]
        )
        first.write_text(content, encoding="utf-8")
        duplicate.write_text(content.replace("R$ 100,00", "R$ 100,00"), encoding="utf-8")

        first_job = self.processor.create_upload_job([first])
        first_dashboard = self.processor.run_job(first_job.id)
        duplicate_job = self.processor.create_upload_job([duplicate])
        duplicate_dashboard = self.processor.run_job(duplicate_job.id)

        self.assertEqual(first_dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(duplicate_dashboard["kpis"]["erp_records"], 0)
        self.assertEqual(duplicate_dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(self.store.erp_records), 1)
        duplicate_events = self.store.events_for_job(duplicate_job.id)
        duplicate_event = [event for event in duplicate_events if event.event_type == "DUPLICATE_DETECTED"][0]
        self.assertEqual(duplicate_event.data["original_job_id"], first_job.id)
        self.assertEqual(duplicate_event.data["original_document_id"], first_dashboard["documents"][0]["id"])

    def test_duplicate_invoice_in_same_job_is_blocked_without_human_review_or_second_erp(self):
        first = Path(self.tmp.name) / "NF_SAME_JOB_ORIGINAL.pdf"
        duplicate = Path(self.tmp.name) / "NF_SAME_JOB_DUPLICATE.pdf"
        content = "\n".join(
            [
                "Empresa: Same Job Duplicate Ltda",
                "CNPJ: 12.345.678/0001-90",
                "NF: SAME-001",
                "Data: 14/08/2026",
                "Valor Total: R$ 100,00",
            ]
        )
        first.write_text(content, encoding="utf-8")
        duplicate.write_text(content, encoding="utf-8")

        job = self.processor.create_upload_job([first, duplicate])
        dashboard = self.processor.run_job(job.id)

        statuses = {doc["file_name"]: doc["status"] for doc in dashboard["documents"]}
        self.assertEqual(statuses["NF_SAME_JOB_ORIGINAL.pdf"], "REGISTERED")
        self.assertEqual(statuses["NF_SAME_JOB_DUPLICATE.pdf"], "DUPLICATE_BLOCKED")
        self.assertEqual(dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(dashboard["kpis"]["human_reviews"], 0)
        self.assertEqual(len(self.store.erp_records_for_job(job.id)), 1)
        self.assertEqual(len(self.store.reviews_for_job(job.id)), 0)
        duplicate_events = [event for event in self.store.events_for_job(job.id) if event.event_type == "DUPLICATE_DETECTED"]
        self.assertEqual(len(duplicate_events), 1)
        self.assertEqual(duplicate_events[0].data["original_job_id"], job.id)

    def test_non_duplicate_validation_fail_still_goes_to_human_review(self):
        invalid = Path(self.tmp.name) / "NF_MISSING_CNPJ_REVIEW.pdf"
        invalid.write_text(
            "\n".join(
                [
                    "Empresa: Missing CNPJ Review Ltda",
                    "NF: MISS-CNPJ-001",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )

        job = self.processor.create_upload_job([invalid])
        dashboard = self.processor.run_job(job.id)

        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertEqual(dashboard["kpis"]["human_reviews"], 1)
        self.assertEqual(dashboard["kpis"]["erp_records"], 0)
        event_types = [event.event_type for event in self.store.events_for_job(job.id)]
        self.assertIn("DECISION_HUMAN_REVIEW", event_types)
        self.assertNotIn("DUPLICATE_DETECTED", event_types)

    def test_global_human_review_queue_keeps_old_review_after_new_job(self):
        invalid = Path(self.tmp.name) / "NF_OLD_REVIEW.pdf"
        invalid.write_text(
            "\n".join(
                [
                    "Empresa: Old Review Ltda",
                    "CNPJ: 12.345.678/0001-90",
                    "Nota: TA",
                    "Data: 14/08/2026",
                    "Valor Total: R$ 100,00",
                ]
            ),
            encoding="utf-8",
        )
        valid = Path(self.tmp.name) / "NF_NEW_VALID.pdf"
        valid.write_text(
            "\n".join(
                [
                    "Empresa: New Valid Ltda",
                    "CNPJ: 98.765.432/0001-10",
                    "NF: NEW-001",
                    "Data: 15/08/2026",
                    "Valor Total: R$ 200,00",
                ]
            ),
            encoding="utf-8",
        )

        old_job = self.processor.create_upload_job([invalid])
        self.processor.run_job(old_job.id)
        new_job = self.processor.create_upload_job([valid])
        self.processor.run_job(new_job.id)
        queue = build_global_human_review_queue(self.store)

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["job_id"], old_job.id)
        self.assertEqual(queue[0]["file_name"], "NF_OLD_REVIEW.pdf")
        self.assertEqual(queue[0]["status"], "OPEN")

    def test_global_human_reviews_endpoint_returns_open_reviews(self):
        original_store = api_main.store
        original_processor = api_main.processor
        api_main.store = self.store
        api_main.processor = self.processor
        invalid = Path(self.tmp.name) / "NF_ENDPOINT_REVIEW.pdf"
        invalid.write_text(
            "Empresa: Endpoint Review Ltda\nCNPJ: 12.345.678/0001-90\nNota: TA\nData: 14/08/2026\nValor Total: R$ 100,00",
            encoding="utf-8",
        )
        try:
            job = self.processor.create_upload_job([invalid])
            self.processor.run_job(job.id)
            client = TestClient(api_main.app)
            response = client.get("/human-reviews")
        finally:
            api_main.store = original_store
            api_main.processor = original_processor

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], job.id)
        self.assertIn("document_id", rows[0])
        self.assertIn("reason", rows[0])

    def test_old_human_review_can_be_corrected_after_new_job(self):
        invalid = Path(self.tmp.name) / "NF_OLD_REVIEW_FIX.pdf"
        invalid.write_text(
            "Empresa: Old Review Fix Ltda\nCNPJ: 12.345.678/0001-90\nNota: TA\nData: 14/08/2026\nValor Total: R$ 100,00",
            encoding="utf-8",
        )
        valid = Path(self.tmp.name) / "NF_AFTER_REVIEW_FIX.pdf"
        valid.write_text(
            "Empresa: After Review Fix Ltda\nCNPJ: 98.765.432/0001-10\nNF: AFTER-001\nData: 15/08/2026\nValor Total: R$ 200,00",
            encoding="utf-8",
        )

        old_job = self.processor.create_upload_job([invalid])
        self.processor.run_job(old_job.id)
        review = self.store.reviews_for_job(old_job.id)[0]
        self.processor.run_job(self.processor.create_upload_job([valid]).id)
        dashboard = self.processor.resolve_review(
            review.id,
            {
                "company_name": "Old Review Fix Ltda",
                "cnpj": "12.345.678/0001-90",
                "invoice_number": "OLD-001",
                "issue_date": "14/08/2026",
                "total_amount": 100.0,
            },
            reviewer="unit_test",
        )

        self.assertEqual(dashboard["job"]["id"], old_job.id)
        self.assertEqual(self.store.human_reviews[review.id].status, "RESOLVED")
        self.assertEqual(build_global_human_review_queue(self.store), [])
        self.assertEqual(self.store.documents[review.document_id].status, "REGISTERED")

    def test_old_human_review_can_be_rejected_after_new_job(self):
        invalid = Path(self.tmp.name) / "NF_OLD_REVIEW_REJECT_GLOBAL.pdf"
        invalid.write_text(
            "Empresa: Old Review Reject Ltda\nCNPJ: 12.345.678/0001-90\nNota: TA\nData: 14/08/2026\nValor Total: R$ 100,00",
            encoding="utf-8",
        )
        valid = Path(self.tmp.name) / "NF_AFTER_REVIEW_REJECT.pdf"
        valid.write_text(
            "Empresa: After Review Reject Ltda\nCNPJ: 98.765.432/0001-10\nNF: AFTER-R-001\nData: 15/08/2026\nValor Total: R$ 200,00",
            encoding="utf-8",
        )

        old_job = self.processor.create_upload_job([invalid])
        self.processor.run_job(old_job.id)
        review = self.store.reviews_for_job(old_job.id)[0]
        self.processor.run_job(self.processor.create_upload_job([valid]).id)
        dashboard = self.processor.reject_review(review.id, reviewer="unit_test")

        self.assertEqual(dashboard["job"]["id"], old_job.id)
        self.assertEqual(self.store.human_reviews[review.id].status, "REJECTED")
        self.assertEqual(build_global_human_review_queue(self.store), [])
        self.assertEqual(self.store.documents[review.document_id].status, "REJECTED")

    def test_two_global_reviews_remain_editable_across_jobs_and_count_down(self):
        first = Path(self.tmp.name) / "NF_GLOBAL_REVIEW_A.pdf"
        first.write_text(
            "Empresa: Global Review A Ltda\nCNPJ: 12.345.678/0001-90\nNota: TA\nData: 14/08/2026\nValor Total: R$ 100,00",
            encoding="utf-8",
        )
        second = Path(self.tmp.name) / "NF_GLOBAL_REVIEW_B.pdf"
        second.write_text(
            "Empresa: Global Review B Ltda\nCNPJ: 98.765.432/0001-10\nNota: NF\nData: 15/08/2026\nValor Total: R$ 200,00",
            encoding="utf-8",
        )

        first_job = self.processor.create_upload_job([first])
        self.processor.run_job(first_job.id)
        second_job = self.processor.create_upload_job([second])
        self.processor.run_job(second_job.id)

        queue = build_global_human_review_queue(self.store)
        self.assertEqual(len(queue), 2)
        self.assertEqual({row["job_id"] for row in queue}, {first_job.id, second_job.id})

        first_review = self.store.reviews_for_job(first_job.id)[0]
        self.processor.resolve_review(
            first_review.id,
            {
                "company_name": "Global Review A Ltda",
                "cnpj": "12.345.678/0001-90",
                "invoice_number": "A-001",
                "issue_date": "14/08/2026",
                "total_amount": 100.0,
            },
            reviewer="unit_test",
        )
        queue = build_global_human_review_queue(self.store)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["job_id"], second_job.id)

        second_review = self.store.reviews_for_job(second_job.id)[0]
        self.processor.reject_review(second_review.id, reviewer="unit_test")
        self.assertEqual(build_global_human_review_queue(self.store), [])

    def test_global_erp_records_remain_visible_after_processing_and_switching_jobs(self):
        first = Path(self.tmp.name) / "NF_ERP_GLOBAL_A.pdf"
        first.write_text(
            "Empresa: ERP Global A Ltda\nCNPJ: 12.345.678/0001-90\nNF: ERP-A-001\nData: 14/08/2026\nValor Total: R$ 100,00",
            encoding="utf-8",
        )
        second = Path(self.tmp.name) / "NF_ERP_GLOBAL_B.pdf"
        second.write_text(
            "Empresa: ERP Global B Ltda\nCNPJ: 98.765.432/0001-10\nNF: ERP-B-001\nData: 15/08/2026\nValor Total: R$ 200,00",
            encoding="utf-8",
        )

        first_job = self.processor.create_upload_job([first])
        first_dashboard = self.processor.run_job(first_job.id)
        second_job = self.processor.create_upload_job([second])
        second_dashboard = self.processor.run_job(second_job.id)
        first_job_dashboard_after_switch = build_dashboard(self.store, first_job.id)
        global_records = build_global_erp_records(self.store)

        self.assertEqual(first_dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(second_dashboard["kpis"]["erp_records"], 1)
        self.assertEqual(first_job_dashboard_after_switch["kpis"]["erp_records"], 1)
        self.assertEqual(len(global_records), 2)
        self.assertEqual({record["invoice_number"] for record in global_records}, {"ERP-A-001", "ERP-B-001"})

    def test_global_erp_endpoint_returns_all_records(self):
        original_store = api_main.store
        original_processor = api_main.processor
        api_main.store = self.store
        api_main.processor = self.processor
        first = Path(self.tmp.name) / "NF_ERP_ENDPOINT_A.pdf"
        first.write_text(
            "Empresa: ERP Endpoint A Ltda\nCNPJ: 12.345.678/0001-90\nNF: E-A-001\nData: 14/08/2026\nValor Total: R$ 100,00",
            encoding="utf-8",
        )
        second = Path(self.tmp.name) / "NF_ERP_ENDPOINT_B.pdf"
        second.write_text(
            "Empresa: ERP Endpoint B Ltda\nCNPJ: 98.765.432/0001-10\nNF: E-B-001\nData: 15/08/2026\nValor Total: R$ 200,00",
            encoding="utf-8",
        )
        try:
            self.processor.run_job(self.processor.create_upload_job([first]).id)
            self.processor.run_job(self.processor.create_upload_job([second]).id)
            client = TestClient(api_main.app)
            response = client.get("/erp-records")
        finally:
            api_main.store = original_store
            api_main.processor = original_processor

        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["invoice_number"] for row in rows}, {"E-A-001", "E-B-001"})

    def test_job_history_keeps_per_job_review_totals_and_open_counts(self):
        invalid = Path(self.tmp.name) / "NF_HISTORY_REVIEW_COUNTS.pdf"
        invalid.write_text(
            "Empresa: History Review Counts Ltda\nCNPJ: 12.345.678/0001-90\nNota: TA\nData: 14/08/2026\nValor Total: R$ 100,00",
            encoding="utf-8",
        )
        job = self.processor.create_upload_job([invalid])
        self.processor.run_job(job.id)
        row = build_job_history(self.store)[0]

        self.assertEqual(row["human_reviews"], 1)
        self.assertEqual(row["human_reviews_open"], 1)
        self.assertEqual(row["human_reviews_total"], 1)

        review = self.store.reviews_for_job(job.id)[0]
        self.processor.reject_review(review.id, reviewer="unit_test")
        row = build_job_history(self.store)[0]

        self.assertEqual(row["human_reviews"], 0)
        self.assertEqual(row["human_reviews_open"], 0)
        self.assertEqual(row["human_reviews_total"], 1)

    def test_frontend_uses_global_review_and_erp_endpoints(self):
        app_js = Path("apps/web/app.js").read_text(encoding="utf-8")
        html = Path("apps/web/index.html").read_text(encoding="utf-8")

        self.assertIn('request("/human-reviews")', app_js)
        self.assertIn('request("/erp-records")', app_js)
        self.assertNotIn("renderReviews(data.human_reviews)", app_js)
        self.assertNotIn("renderErp(data.erp_records)", app_js)
        self.assertIn("Mock ERP Global", html)
        self.assertIn("/assets/app.js?v=global-state-", html)


if __name__ == "__main__":
    unittest.main()
