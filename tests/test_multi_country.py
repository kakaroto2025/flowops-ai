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
from tools.documents.normalization import business_key
from tools.reporting import build_global_erp_records, build_global_human_review_queue


class MultiCountryDocumentIntelligenceTests(unittest.TestCase):
    def setUp(self):
        self.env_patch = patch.dict(os.environ, {"GEMINI_API_KEY": "SUA_CHAVE_REAL_AQUI"})
        self.env_patch.start()
        self.adk_patch = patch(
            "agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime",
            return_value="READY",
        )
        self.adk_patch.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = LocalStore(self.root / "state.json")
        self.processor = JobProcessor(self.store)

    def tearDown(self):
        self.adk_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def write_doc(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def br_valid_text(self, invoice: str = "000501234", cnpj: str = "31.415.926/0001-71") -> str:
        return "\n".join(
            [
                "NOTA FISCAL ELETRONICA",
                "Razao Social: Aurora Sistemas Ltda",
                f"CNPJ: {cnpj}",
                f"Numero da Nota: {invoice}",
                "Data de Emissao: 23/08/2026",
                "Valor Total: R$ 8.750,00",
            ]
        )

    def br_batch_text(
        self,
        invoice: str | None = "000501234",
        cnpj: str | None = "12.345.678/0001-95",
        amount: str | None = "R$ 8.750,00",
    ) -> str:
        lines = [
            "NOTA FISCAL ELETRONICA",
            "Razao Social: Brasil Controle Operacional Ltda",
        ]
        if cnpj is not None:
            lines.append(f"CNPJ: {cnpj}")
        if invoice is not None:
            lines.append(f"Numero da Nota: {invoice}")
        lines.append("Data de Emissao: 24/08/2026")
        if amount is not None:
            lines.append(f"Valor Total: {amount}")
        return "\n".join(lines)

    def us_valid_text(self, invoice: str = "INV-2026-1042", ein: str = "12-3456789") -> str:
        return "\n".join(
            [
                "Invoice",
                "Company: Northstar Technologies LLC",
                f"EIN: {ein}",
                f"Invoice Number: {invoice}",
                "Invoice Date: 08/23/2026",
                "Total Amount: $8,750.00",
                "Currency: USD",
            ]
        )

    def us_pdf_layout_text(
        self,
        invoice: str | None = "INV-2026-1042",
        ein: str | None = "12-3456789",
        amount: str | None = "$8,750.00",
    ) -> str:
        lines = [
            "Northstar Technologies LLC",
            "INVOICE",
            "1200 Market Street, Suite 410",
            "Austin, TX 78701",
            "United States",
        ]
        if ein is not None:
            lines.extend(["Tax ID Type", "EIN", "EIN", ein])
        if invoice is not None:
            lines.extend(["Invoice Number", invoice])
        lines.extend(
            [
                "Issue Date",
                "08/24/2026",
                "Currency",
                "USD",
                "Bill To",
                "Blue Ridge Operations Inc.",
                "455 Innovation Drive",
                "Denver, CO 80202",
                "United States",
                "Description",
                "Qty",
                "Unit Price",
                "Amount",
                "AI document operations platform",
            ]
        )
        if amount is not None:
            lines.extend(["Total Amount", amount])
        return "\n".join(lines)

    def run_single(self, path: Path, region: str = "AUTO") -> dict:
        job = self.processor.create_upload_job([path], processing_region=region)
        return self.processor.run_job(job.id)

    def test_br_valid_registers(self):
        dashboard = self.run_single(self.write_doc("BR_VALID.pdf", self.br_valid_text()))
        record = dashboard["erp_records"][0]

        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        self.assertEqual(record["country_code"], "BR")
        self.assertEqual(record["tax_id_type"], "CNPJ")
        self.assertEqual(record["currency"], "BRL")
        self.assertEqual(record["normalized_tax_id"], "31415926000171")
        self.assertEqual(record["normalized_invoice_number"], "000501234")

    def test_br_missing_cnpj_goes_to_human_review(self):
        dashboard = self.run_single(
            self.write_doc(
                "BR_MISSING_CNPJ.pdf",
                self.br_valid_text().replace("CNPJ: 31.415.926/0001-71\n", ""),
            ),
            region="BR",
        )

        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertIn("missing_tax_id", dashboard["human_reviews"][0]["reason"])
        self.assertIn("missing_cnpj", dashboard["human_reviews"][0]["reason"])

    def test_br_missing_invoice_number_goes_to_human_review(self):
        dashboard = self.run_single(
            self.write_doc("BR_MISSING_INVOICE.pdf", self.br_valid_text().replace("Numero da Nota: 000501234\n", "")),
            region="BR",
        )

        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertIn("missing_invoice_number", dashboard["human_reviews"][0]["reason"])

    def test_br_missing_amount_goes_to_human_review(self):
        dashboard = self.run_single(
            self.write_doc("BR_MISSING_AMOUNT.pdf", self.br_valid_text().replace("Valor Total: R$ 8.750,00", "")),
            region="BR",
        )

        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertIn("missing_total_amount", dashboard["human_reviews"][0]["reason"])

    def test_br_duplicate_is_blocked(self):
        first = self.write_doc("BR_ORIGINAL.pdf", self.br_valid_text())
        duplicate = self.write_doc("BR_DUPLICATE.pdf", self.br_valid_text())

        self.run_single(first, region="BR")
        dashboard = self.run_single(duplicate, region="BR")

        self.assertEqual(dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(self.store.erp_records), 1)
        self.assertEqual(build_global_human_review_queue(self.store), [])

    def test_us_valid_registers(self):
        dashboard = self.run_single(self.write_doc("US_VALID.pdf", self.us_valid_text()))
        record = dashboard["erp_records"][0]

        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        self.assertEqual(record["country_code"], "US")
        self.assertEqual(record["tax_id_type"], "EIN")
        self.assertEqual(record["currency"], "USD")
        self.assertEqual(record["normalized_tax_id"], "123456789")
        self.assertEqual(record["normalized_invoice_number"], "INV20261042")
        self.assertEqual(record["issue_date"], "2026-08-23")

    def test_us_missing_ein_goes_to_human_review(self):
        dashboard = self.run_single(
            self.write_doc("US_MISSING_EIN.pdf", self.us_valid_text().replace("EIN: 12-3456789\n", "")),
            region="US",
        )

        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertIn("missing_tax_id", dashboard["human_reviews"][0]["reason"])

    def test_us_missing_invoice_number_goes_to_human_review(self):
        dashboard = self.run_single(
            self.write_doc("US_MISSING_INVOICE.pdf", self.us_valid_text().replace("Invoice Number: INV-2026-1042\n", "")),
            region="US",
        )

        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertIn("missing_invoice_number", dashboard["human_reviews"][0]["reason"])

    def test_us_missing_amount_goes_to_human_review(self):
        dashboard = self.run_single(
            self.write_doc("US_MISSING_AMOUNT.pdf", self.us_valid_text().replace("Total Amount: $8,750.00", "")),
            region="US",
        )

        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertIn("missing_total_amount", dashboard["human_reviews"][0]["reason"])

    def test_us_duplicate_is_blocked(self):
        first = self.write_doc("US_ORIGINAL.pdf", self.us_valid_text())
        duplicate = self.write_doc("US_DUPLICATE.pdf", self.us_valid_text())

        self.run_single(first, region="US")
        dashboard = self.run_single(duplicate, region="US")

        self.assertEqual(dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(self.store.erp_records), 1)
        self.assertEqual(build_global_human_review_queue(self.store), [])

    def test_auto_detect_br_us_and_unknown(self):
        br = self.run_single(self.write_doc("AUTO_BR.pdf", self.br_valid_text()))
        us = self.run_single(self.write_doc("AUTO_US.pdf", self.us_valid_text()))
        unknown = self.run_single(self.write_doc("AUTO_UNKNOWN.pdf", "Packing memo\nReference only\nAmount pending"))

        self.assertEqual(br["erp_records"][0]["country_code"], "BR")
        self.assertEqual(us["erp_records"][0]["country_code"], "US")
        self.assertEqual(unknown["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertIn("unknown_country", unknown["human_reviews"][0]["reason"])

    def test_user_selected_region_overrides_detection_without_inventing_tax_id(self):
        br = self.run_single(self.write_doc("SELECT_BR.pdf", self.br_valid_text()), region="BR")
        us_missing = self.run_single(
            self.write_doc("SELECT_US_MISSING_EIN.pdf", "Invoice\nCompany: Selected US LLC\nInvoice Number: S-1\nTotal: $100.00"),
            region="US",
        )

        self.assertEqual(br["erp_records"][0]["country_code"], "BR")
        self.assertEqual(us_missing["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertIn("missing_tax_id", us_missing["human_reviews"][0]["reason"])

    def test_cross_country_collision_is_not_duplicate(self):
        br = self.write_doc("BR_COLLISION.pdf", self.br_valid_text(invoice="1001", cnpj="31.415.926/0001-71"))
        us = self.write_doc("US_COLLISION.pdf", self.us_valid_text(invoice="1001", ein="12-3456789"))

        self.run_single(br, region="BR")
        dashboard = self.run_single(us, region="US")

        self.assertEqual(dashboard["documents"][0]["status"], "REGISTERED")
        self.assertEqual(len(self.store.erp_records), 2)
        keys = {business_key(record) for record in self.store.erp_records.values()}
        self.assertEqual(keys, {"BR|31415926000171|1001", "US|123456789|1001"})

    def test_batch_multi_country_mixed_outcomes(self):
        files = [
            self.write_doc("BATCH_BR_VALID.pdf", self.br_valid_text(invoice="000700001")),
            self.write_doc("BATCH_US_VALID.pdf", self.us_valid_text(invoice="US-700001")),
            self.write_doc("BATCH_BR_INCOMPLETE.pdf", self.br_valid_text(invoice="000700002").replace("CNPJ: 31.415.926/0001-71\n", "")),
            self.write_doc("BATCH_US_INCOMPLETE.pdf", self.us_valid_text(invoice="US-700002").replace("EIN: 12-3456789\n", "")),
            self.write_doc("BATCH_BR_DUP.pdf", self.br_valid_text(invoice="000700001")),
            self.write_doc("BATCH_US_DUP.pdf", self.us_valid_text(invoice="US-700001")),
        ]

        job = self.processor.create_upload_job(files, processing_region="AUTO")
        dashboard = self.processor.run_job(job.id)
        statuses = {doc["file_name"]: doc["status"] for doc in dashboard["documents"]}

        self.assertEqual(statuses["BATCH_BR_VALID.pdf"], "REGISTERED")
        self.assertEqual(statuses["BATCH_US_VALID.pdf"], "REGISTERED")
        self.assertEqual(statuses["BATCH_BR_INCOMPLETE.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["BATCH_US_INCOMPLETE.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["BATCH_BR_DUP.pdf"], "DUPLICATE_BLOCKED")
        self.assertEqual(statuses["BATCH_US_DUP.pdf"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(self.store.erp_records_for_job(job.id)), 2)

    def test_persistence_keeps_br_us_and_duplicate_blocks_after_reload(self):
        br = self.write_doc("PERSIST_BR.pdf", self.br_valid_text(invoice="000800001"))
        us = self.write_doc("PERSIST_US.pdf", self.us_valid_text(invoice="P-US-800001"))

        self.run_single(br, region="BR")
        self.run_single(us, region="US")
        reloaded = LocalStore(self.root / "state.json")
        processor = JobProcessor(reloaded)

        self.assertEqual(len(reloaded.erp_records), 2)
        br_dup = self.write_doc("PERSIST_BR_DUP.pdf", self.br_valid_text(invoice="000800001"))
        us_dup = self.write_doc("PERSIST_US_DUP.pdf", self.us_valid_text(invoice="P-US-800001"))
        br_dashboard = processor.run_job(processor.create_upload_job([br_dup], processing_region="BR").id)
        us_dashboard = processor.run_job(processor.create_upload_job([us_dup], processing_region="US").id)

        self.assertEqual(br_dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(us_dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(reloaded.erp_records), 2)

    def test_gemini_failure_falls_back_for_br_and_us(self):
        with patch(
            "agents.document.agent.extract_with_gemini",
            side_effect=GeminiExtractionError("gemini_request_failed:ClientError", {"http_status": 429}),
        ):
            br = self.run_single(self.write_doc("FALLBACK_BR.pdf", self.br_valid_text(invoice="000900001")), region="BR")
            us = self.run_single(self.write_doc("FALLBACK_US.pdf", self.us_valid_text(invoice="FB-US-900001")), region="US")

        self.assertEqual(br["documents"][0]["status"], "REGISTERED")
        self.assertEqual(us["documents"][0]["status"], "REGISTERED")
        event_types = [event.event_type for event in self.store.events.values()]
        self.assertGreaterEqual(event_types.count("LOCAL_PARSER_FALLBACK"), 2)

    def test_global_erp_contains_br_and_us_records(self):
        self.run_single(self.write_doc("ERP_BR.pdf", self.br_valid_text(invoice="000910001")), region="BR")
        self.run_single(self.write_doc("ERP_US.pdf", self.us_valid_text(invoice="ERP-US-910001")), region="US")

        records = build_global_erp_records(self.store)

        self.assertEqual({record["country_code"] for record in records}, {"BR", "US"})
        self.assertEqual({record["currency"] for record in records}, {"BRL", "USD"})

    def test_manual_acceptance_auto_br_valid_invoice_propagates_country(self):
        dashboard = self.run_single(self.write_doc("MANUAL_AUTO_BR.pdf", self.br_valid_text(invoice="000920001")))

        doc = dashboard["documents"][0]
        record = dashboard["erp_records"][0]
        self.assertEqual(doc["country_code"], "BR")
        self.assertEqual(doc["tax_id_type"], "CNPJ")
        self.assertEqual(doc["currency"], "BRL")
        self.assertEqual(record["country_code"], "BR")
        self.assertEqual(record["tax_id_type"], "CNPJ")
        self.assertEqual(record["currency"], "BRL")

    def test_manual_acceptance_auto_br_missing_invoice_review_preserves_known_fields(self):
        dashboard = self.run_single(
            self.write_doc(
                "MANUAL_BR_REVIEW.pdf",
                self.br_valid_text(invoice="000920002").replace("Numero da Nota: 000920002\n", ""),
            )
        )

        review = dashboard["human_reviews"][0]
        fields = review["suggested_fields"]
        self.assertEqual(dashboard["documents"][0]["status"], "HUMAN_REVIEW")
        self.assertEqual(fields["country_code"], "BR")
        self.assertEqual(fields["tax_id_type"], "CNPJ")
        self.assertEqual(fields["tax_id"], "31.415.926/0001-71")
        self.assertEqual(fields["normalized_tax_id"], "31415926000171")
        self.assertEqual(fields["currency"], "BRL")

    def test_manual_acceptance_br_review_correction_preserves_country_in_erp(self):
        dashboard = self.run_single(
            self.write_doc(
                "MANUAL_BR_REVIEW_FIX.pdf",
                self.br_valid_text(invoice="000920003").replace("Numero da Nota: 000920003\n", ""),
            )
        )
        review_id = dashboard["human_reviews"][0]["id"]

        resolved = self.processor.resolve_review(review_id, {"invoice_number": "000920003"}, reviewer="unit_test")
        record = build_global_erp_records(self.store)[0]

        self.assertEqual(resolved["documents"][0]["status"], "REGISTERED")
        self.assertEqual(record["country_code"], "BR")
        self.assertEqual(record["tax_id_type"], "CNPJ")
        self.assertEqual(record["normalized_tax_id"], "31415926000171")
        self.assertEqual(record["currency"], "BRL")

    def test_manual_acceptance_auto_us_valid_invoice_propagates_country(self):
        dashboard = self.run_single(self.write_doc("MANUAL_AUTO_US.pdf", self.us_valid_text(invoice="US-920001")))

        doc = dashboard["documents"][0]
        record = dashboard["erp_records"][0]
        self.assertEqual(doc["country_code"], "US")
        self.assertEqual(doc["tax_id_type"], "EIN")
        self.assertEqual(doc["currency"], "USD")
        self.assertEqual(record["country_code"], "US")
        self.assertEqual(record["tax_id_type"], "EIN")
        self.assertEqual(record["currency"], "USD")

    def test_manual_acceptance_us_review_correction_preserves_country_in_erp(self):
        dashboard = self.run_single(
            self.write_doc(
                "MANUAL_US_REVIEW_FIX.pdf",
                self.us_valid_text(invoice="US-920002").replace("Invoice Number: US-920002\n", ""),
            )
        )
        review = dashboard["human_reviews"][0]
        fields = review["suggested_fields"]

        self.assertEqual(fields["country_code"], "US")
        self.assertEqual(fields["tax_id_type"], "EIN")
        self.assertEqual(fields["tax_id"], "12-3456789")
        self.assertEqual(fields["currency"], "USD")

        self.processor.resolve_review(review["id"], {"invoice_number": "US-920002"}, reviewer="unit_test")
        record = build_global_erp_records(self.store)[0]
        self.assertEqual(record["country_code"], "US")
        self.assertEqual(record["tax_id_type"], "EIN")
        self.assertEqual(record["normalized_tax_id"], "123456789")
        self.assertEqual(record["currency"], "USD")

    def test_manual_acceptance_dashboard_api_returns_country_for_documents(self):
        job = self.processor.create_upload_job(
            [
                self.write_doc("MANUAL_DASH_BR.pdf", self.br_valid_text(invoice="000920004")),
                self.write_doc("MANUAL_DASH_US.pdf", self.us_valid_text(invoice="US-920004")),
            ],
            processing_region="AUTO",
        )
        dashboard = self.processor.run_job(job.id)
        by_file = {doc["file_name"]: doc for doc in dashboard["documents"]}

        self.assertEqual(by_file["MANUAL_DASH_BR.pdf"]["country_code"], "BR")
        self.assertEqual(by_file["MANUAL_DASH_US.pdf"]["country_code"], "US")

    def test_manual_acceptance_persistence_preserves_country_and_currency(self):
        self.run_single(self.write_doc("MANUAL_PERSIST_BR.pdf", self.br_valid_text(invoice="000920005")))
        self.run_single(self.write_doc("MANUAL_PERSIST_US.pdf", self.us_valid_text(invoice="US-920005")))

        reloaded = LocalStore(self.root / "state.json")
        erp_records = list(reloaded.erp_records.values())
        extractions = list(reloaded.extractions.values())

        self.assertEqual({record.country_code for record in erp_records}, {"BR", "US"})
        self.assertEqual({record.currency for record in erp_records}, {"BRL", "USD"})
        self.assertIn("BR", {extraction.country_code for extraction in extractions})
        self.assertIn("US", {extraction.country_code for extraction in extractions})

    def test_manual_acceptance_br_duplicate_still_blocked(self):
        self.run_single(self.write_doc("MANUAL_BR_DUP_ORIGINAL.pdf", self.br_valid_text(invoice="000920006")))
        dashboard = self.run_single(self.write_doc("MANUAL_BR_DUP_COPY.pdf", self.br_valid_text(invoice="000920006")))

        self.assertEqual(dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(self.store.erp_records), 1)

    def test_manual_acceptance_us_duplicate_still_blocked(self):
        self.run_single(self.write_doc("MANUAL_US_DUP_ORIGINAL.pdf", self.us_valid_text(invoice="US-920006")))
        dashboard = self.run_single(self.write_doc("MANUAL_US_DUP_COPY.pdf", self.us_valid_text(invoice="US-920006")))

        self.assertEqual(dashboard["documents"][0]["status"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(self.store.erp_records), 1)

    def test_regression_us_pdf_layout_auto_registers_valid_invoice(self):
        dashboard = self.run_single(self.write_doc("US_LAYOUT_VALID.pdf", self.us_pdf_layout_text()))
        doc = dashboard["documents"][0]
        record = dashboard["erp_records"][0]

        self.assertEqual(doc["status"], "REGISTERED")
        self.assertEqual(doc["country_code"], "US")
        self.assertEqual(doc["tax_id_type"], "EIN")
        self.assertEqual(doc["invoice_number"], "INV-2026-1042")
        self.assertEqual(doc["currency"], "USD")
        self.assertEqual(record["normalized_tax_id"], "123456789")
        self.assertEqual(record["issue_date"], "2026-08-24")
        self.assertEqual(record["total_amount"], 8750.0)

    def test_regression_us_pdf_layout_batch_acceptance(self):
        files = [
            self.write_doc("US_01_VALID_INVOICE.pdf", self.us_pdf_layout_text()),
            self.write_doc("US_02_MISSING_EIN.pdf", self.us_pdf_layout_text(invoice="INV-2026-1043", ein=None, amount="$4,280.00")),
            self.write_doc("US_03_MISSING_INVOICE_NUMBER.pdf", self.us_pdf_layout_text(invoice=None, amount="$11,340.00")),
            self.write_doc("US_04_MISSING_AMOUNT.pdf", self.us_pdf_layout_text(invoice="INV-2026-1044", amount=None)),
            self.write_doc(
                "US_05_NON_INVOICE_DOCUMENT.pdf",
                "\n".join(
                    [
                        "DELIVERY MEMORANDUM",
                        "Operational document - not an invoice",
                        "Northstar Technologies LLC",
                        "1200 Market Street, Suite 410",
                        "Austin, TX 78701",
                        "United States",
                        "Delivery Reference",
                        "DLV-2026-7781",
                        "Date",
                        "08/24/2026",
                    ]
                ),
            ),
            self.write_doc("US_06_DUPLICATE_INVOICE.pdf", self.us_pdf_layout_text()),
        ]
        dashboard = self.processor.run_job(self.processor.create_upload_job(files, processing_region="AUTO").id)
        statuses = {doc["file_name"]: doc["status"] for doc in dashboard["documents"]}
        reviews = {review["file_name"]: review["reason"] for review in build_global_human_review_queue(self.store)}

        self.assertEqual(statuses["US_01_VALID_INVOICE.pdf"], "REGISTERED")
        self.assertEqual(statuses["US_02_MISSING_EIN.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["US_03_MISSING_INVOICE_NUMBER.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["US_04_MISSING_AMOUNT.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["US_05_NON_INVOICE_DOCUMENT.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["US_06_DUPLICATE_INVOICE.pdf"], "DUPLICATE_BLOCKED")
        self.assertIn("missing_tax_id", reviews["US_02_MISSING_EIN.pdf"])
        self.assertNotIn("missing_cnpj", reviews["US_02_MISSING_EIN.pdf"])
        self.assertIn("missing_invoice_number", reviews["US_03_MISSING_INVOICE_NUMBER.pdf"])
        self.assertIn("missing_total_amount", reviews["US_04_MISSING_AMOUNT.pdf"])
        self.assertEqual(len(self.store.erp_records), 1)

    def test_regression_country_aware_date_normalization(self):
        br = self.run_single(self.write_doc("DATE_BR.pdf", self.br_valid_text(invoice="000930001")))
        us = self.run_single(self.write_doc("DATE_US.pdf", self.us_pdf_layout_text(invoice="INV-930001")))

        self.assertEqual(br["erp_records"][0]["issue_date"], "2026-08-23")
        self.assertEqual(us["erp_records"][0]["issue_date"], "2026-08-24")

    def test_real_upload_endpoint_us_batch_auto_matches_dashboard_path(self):
        original_store = api_main.store
        original_processor = api_main.processor
        api_main.store = self.store
        api_main.processor = self.processor
        files = [
            ("files", ("US_01_VALID_INVOICE.pdf", self.us_pdf_layout_text().encode("utf-8"), "application/pdf")),
            (
                "files",
                (
                    "US_02_MISSING_EIN.pdf",
                    self.us_pdf_layout_text(invoice="INV-2026-1043", ein=None, amount="$4,280.00").encode("utf-8"),
                    "application/pdf",
                ),
            ),
            (
                "files",
                (
                    "US_03_MISSING_INVOICE_NUMBER.pdf",
                    self.us_pdf_layout_text(invoice=None, amount="$11,340.00").encode("utf-8"),
                    "application/pdf",
                ),
            ),
            (
                "files",
                (
                    "US_04_MISSING_AMOUNT.pdf",
                    self.us_pdf_layout_text(invoice="INV-2026-1044", amount=None).encode("utf-8"),
                    "application/pdf",
                ),
            ),
            (
                "files",
                (
                    "US_05_NON_INVOICE_DOCUMENT.pdf",
                    "DELIVERY MEMORANDUM\nOperational document - not an invoice\nUnited States".encode("utf-8"),
                    "application/pdf",
                ),
            ),
            ("files", ("US_06_DUPLICATE_INVOICE.pdf", self.us_pdf_layout_text().encode("utf-8"), "application/pdf")),
        ]

        try:
            with patch(
                "agents.document.agent.extract_with_gemini",
                side_effect=GeminiExtractionError("forced_endpoint_fallback"),
            ):
                response = TestClient(api_main.app).post(
                    "/jobs/upload/run",
                    data={"processing_region": "AUTO"},
                    files=files,
                )
        finally:
            api_main.store = original_store
            api_main.processor = original_processor

        self.assertEqual(response.status_code, 200)
        dashboard = response.json()
        statuses = {doc["file_name"]: doc["status"] for doc in dashboard["documents"]}
        docs = {doc["file_name"]: doc for doc in dashboard["documents"]}
        reviews = {review["file_name"]: review["reason"] for review in build_global_human_review_queue(self.store)}

        self.assertEqual(dashboard["job"]["processing_region"], "AUTO")
        self.assertEqual(statuses["US_01_VALID_INVOICE.pdf"], "REGISTERED")
        self.assertEqual(statuses["US_02_MISSING_EIN.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["US_03_MISSING_INVOICE_NUMBER.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["US_04_MISSING_AMOUNT.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["US_05_NON_INVOICE_DOCUMENT.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["US_06_DUPLICATE_INVOICE.pdf"], "DUPLICATE_BLOCKED")
        self.assertEqual(docs["US_01_VALID_INVOICE.pdf"]["country_code"], "US")
        self.assertEqual(docs["US_01_VALID_INVOICE.pdf"]["currency"], "USD")
        self.assertEqual(docs["US_01_VALID_INVOICE.pdf"]["tax_id_type"], "EIN")
        self.assertEqual(len(self.store.erp_records), 1)
        self.assertIn("missing_tax_id", reviews["US_02_MISSING_EIN.pdf"])
        self.assertNotIn("missing_cnpj", reviews["US_02_MISSING_EIN.pdf"])

    def test_real_upload_endpoint_br_batch_preserves_order_and_blocks_later_duplicate(self):
        original_store = api_main.store
        original_processor = api_main.processor
        api_main.store = self.store
        api_main.processor = self.processor
        files = [
            ("files", ("01_NF_VALIDA_CONTROLE.pdf", self.br_batch_text().encode("utf-8"), "application/pdf")),
            (
                "files",
                (
                    "02_NF_SEM_CNPJ.pdf",
                    self.br_batch_text(invoice="000501235", cnpj=None, amount="R$ 4.280,00").encode("utf-8"),
                    "application/pdf",
                ),
            ),
            (
                "files",
                (
                    "03_NF_SEM_NUMERO.pdf",
                    self.br_batch_text(invoice=None, cnpj="45.678.901/0001-22", amount="R$ 11.340,00").encode("utf-8"),
                    "application/pdf",
                ),
            ),
            (
                "files",
                (
                    "04_NF_SEM_VALOR.pdf",
                    self.br_batch_text(invoice="000501237", cnpj="56.789.012/0001-33", amount=None).encode("utf-8"),
                    "application/pdf",
                ),
            ),
            (
                "files",
                (
                    "05_DOCUMENTO_NAO_FISCAL.pdf",
                    "COMUNICADO INTERNO\nDocumento operacional sem valor fiscal\nBrasil".encode("utf-8"),
                    "application/pdf",
                ),
            ),
            ("files", ("06_NF_DUPLICADA_CONTROLE.pdf", self.br_batch_text().encode("utf-8"), "application/pdf")),
        ]

        try:
            with patch(
                "agents.document.agent.extract_with_gemini",
                side_effect=GeminiExtractionError("forced_endpoint_fallback"),
            ):
                response = TestClient(api_main.app).post(
                    "/jobs/upload/run",
                    data={"processing_region": "AUTO"},
                    files=files,
                )
        finally:
            api_main.store = original_store
            api_main.processor = original_processor

        self.assertEqual(response.status_code, 200)
        dashboard = response.json()
        documents = dashboard["documents"]
        statuses = {doc["file_name"]: doc["status"] for doc in documents}
        docs = {doc["file_name"]: doc for doc in documents}
        document_order = [doc["file_name"] for doc in documents]

        self.assertEqual(document_order, [item[1][0] for item in files])
        self.assertEqual(statuses["01_NF_VALIDA_CONTROLE.pdf"], "REGISTERED")
        self.assertEqual(statuses["02_NF_SEM_CNPJ.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["03_NF_SEM_NUMERO.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["04_NF_SEM_VALOR.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["05_DOCUMENTO_NAO_FISCAL.pdf"], "HUMAN_REVIEW")
        self.assertEqual(statuses["06_NF_DUPLICADA_CONTROLE.pdf"], "DUPLICATE_BLOCKED")
        self.assertEqual(docs["01_NF_VALIDA_CONTROLE.pdf"]["country_code"], "BR")
        self.assertEqual(docs["01_NF_VALIDA_CONTROLE.pdf"]["tax_id_type"], "CNPJ")
        self.assertEqual(docs["01_NF_VALIDA_CONTROLE.pdf"]["currency"], "BRL")

        records = list(self.store.erp_records.values())
        duplicate_events = [event for event in self.store.events.values() if event.event_type == "DUPLICATE_DETECTED"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].document_id, docs["01_NF_VALIDA_CONTROLE.pdf"]["id"])
        self.assertEqual(duplicate_events[0].document_id, docs["06_NF_DUPLICADA_CONTROLE.pdf"]["id"])
        self.assertEqual(
            duplicate_events[0].data["original_document_id"],
            docs["01_NF_VALIDA_CONTROLE.pdf"]["id"],
        )
        self.assertEqual(
            duplicate_events[0].data["original_erp_record_id"],
            records[0].id,
        )


if __name__ == "__main__":
    unittest.main()
