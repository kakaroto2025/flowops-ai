from dataclasses import replace
from unittest.mock import patch

import pytest

from apps.api.processor import JobProcessor
from shared.models import AuthContext, CloudStore, CloudStoreConfig, Document, ERPRecord, Extraction, Job, LocalStore
from tools.email_intake.message_gate import MessageSuccessGate, message_state_id
import test_email_intake as email_helpers
from test_cloud_store import FakeFirestoreBackend, FakeObjectStorage


TENANT = "tenant_gate"
MAILBOX = "gate@example.test"
MESSAGE = "controlled-message"


@pytest.fixture(params=["local", "cloud"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalStore(tmp_path / "state.json")
    return CloudStore(CloudStoreConfig("test", "(default)", "test"), FakeFirestoreBackend(), FakeObjectStorage())


def seed(store, tmp_path, outcomes=("REGISTERED",)):
    receipts = []
    for i, outcome in enumerate(outcomes):
        suffix = str(i)
        receipt_id = "receipt-" + suffix
        job = Job("job-" + suffix, "test", status="COMPLETED", tenant_id=TENANT)
        path = tmp_path / (suffix + ".pdf")
        path.write_bytes(b"fictitious document")
        doc = Document("doc-" + suffix, job.id, path.name, str(path), tenant_id=TENANT,
                       status=outcome, email_receipt_id=receipt_id)
        store.add_job(job)
        store.add_document(doc)
        uri = store.store_document_bytes(doc, path.read_bytes())
        ext = Extraction("ext-" + suffix, doc.id, "invoice", None, "Test", suffix, "2026-09-10", 1, .99, tenant_id=TENANT)
        store.add_extraction(ext)
        erp = ERPRecord("erp-" + suffix, job.id, doc.id, suffix, "", 1, tenant_id=TENANT)
        if outcome == "REGISTERED":
            store.add_erp_record(erp)
        store.claim_email_intake_receipt(receipt_id, {
            "id": receipt_id, "status": "PROCESSING", "tenant_id": TENANT,
            "mailbox": MAILBOX, "provider": "gmail", "provider_message_id": MESSAGE,
            "attachment_id": "attachment-" + suffix,
        })
        store.complete_email_intake_receipt(receipt_id, {
            "job_id": job.id, "document_id": doc.id, "extraction_id": ext.id, "erp_record_id": erp.id,
            "processing_state": "PROCESSING" if outcome == "PROCESSING" else "COMPLETED",
            "business_outcome": outcome, "pdf_persisted": True, "object_uri": uri,
        })
        receipts.append(receipt_id)
    gate = MessageSuccessGate(store)
    gate.prepare(TENANT, MAILBOX, MESSAGE, receipts, ["attachment-" + str(i) for i in range(len(receipts))])
    return gate


@pytest.mark.parametrize("outcomes,expected", [
    (("REGISTERED",), "ELIGIBLE"),
    (("REGISTERED", "REGISTERED"), "ELIGIBLE"),
    (("REGISTERED", "HUMAN_REVIEW"), "INELIGIBLE"),
    (("REGISTERED", "FAILED"), "INELIGIBLE"),
    (("REGISTERED", "PROCESSING"), "NOT_READY"),
    (("REJECTED",), "INELIGIBLE"),
    (("DUPLICATE_BLOCKED",), "INELIGIBLE"),
    ((), "INELIGIBLE"),
])
def test_business_outcomes(store, tmp_path, outcomes, expected):
    gate = seed(store, tmp_path, outcomes)
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == expected
    state = store.read_persisted_record("gmail_message_states", message_state_id(TENANT, MAILBOX, MESSAGE))
    assert state["eligibility_state"] == expected
    assert state["gmail_state"] != "SYNCED"


@pytest.mark.parametrize("gmail_state", ["PENDING", "SYNCING", "SYNCED", "ERROR"])
def test_eligibility_reevaluation_preserves_gmail_sync_state(store, tmp_path, gmail_state):
    gate = seed(store, tmp_path)
    state_id = message_state_id(TENANT, MAILBOX, MESSAGE)
    state = store.read_persisted_record("gmail_message_states", state_id)
    state.update(
        eligibility_state="ELIGIBLE",
        gmail_state=gmail_state,
        label_id="Label_123",
        gmail_synced_at="2026-09-11T10:00:00+00:00",
        gmail_sync_error_code="previous-error",
        attempt_count=3,
        last_attempt_at="2026-09-11T10:01:00+00:00",
    )
    store.put_gmail_message_state(state_id, state)

    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "ELIGIBLE"

    updated = store.read_persisted_record("gmail_message_states", state_id)
    assert updated["eligibility_state"] == "ELIGIBLE"
    assert updated["gmail_state"] == gmail_state
    assert updated["label_id"] == "Label_123"
    assert updated["gmail_synced_at"] == "2026-09-11T10:00:00+00:00"
    assert updated["gmail_sync_error_code"] == "previous-error"
    assert updated["attempt_count"] == 3
    assert updated["last_attempt_at"] == "2026-09-11T10:01:00+00:00"


@pytest.mark.parametrize("outcomes,expected", [
    (("REGISTERED",), "ELIGIBLE"),
    (("HUMAN_REVIEW",), "INELIGIBLE"),
    (("PROCESSING",), "NOT_READY"),
])
def test_eligibility_changes_without_resetting_sync_state(store, tmp_path, outcomes, expected):
    gate = seed(store, tmp_path, outcomes)
    state_id = message_state_id(TENANT, MAILBOX, MESSAGE)
    state = store.read_persisted_record("gmail_message_states", state_id)
    state.update(eligibility_state="NOT_READY", gmail_state="SYNCING")
    store.put_gmail_message_state(state_id, state)

    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == expected

    updated = store.read_persisted_record("gmail_message_states", state_id)
    assert updated["eligibility_state"] == expected
    assert updated["gmail_state"] == "SYNCING"


def test_new_message_state_defaults_gmail_sync_pending(store):
    state_id = MessageSuccessGate(store).prepare(TENANT, MAILBOX, MESSAGE, [], [])
    state = store.read_persisted_record("gmail_message_states", state_id)
    assert state["gmail_state"] == "PENDING"


def test_cloud_eligibility_update_does_not_overwrite_concurrent_sync_transition(tmp_path):
    backend = FakeFirestoreBackend()
    store = CloudStore(CloudStoreConfig("test", "(default)", "test"), backend, FakeObjectStorage())
    gate = seed(store, tmp_path)
    state_id = message_state_id(TENANT, MAILBOX, MESSAGE)
    collection = store.COLLECTIONS["gmail_message_states"]
    key = f"{collection}/{state_id}"
    original_update = backend.update

    def sync_then_update(collection_name, document_id, changes):
        backend.documents[f"{collection_name}/{document_id}"]["gmail_state"] = "SYNCING"
        backend.documents[f"{collection_name}/{document_id}"]["attempt_count"] = 1
        original_update(collection_name, document_id, changes)

    backend.update = sync_then_update

    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "ELIGIBLE"

    state = backend.documents[key]
    assert state["eligibility_state"] == "ELIGIBLE"
    assert state["gmail_state"] == "SYNCING"
    assert state["attempt_count"] == 1
    assert backend.updates
    assert all("gmail_state" not in changes for _, _, changes in backend.updates)


@pytest.mark.parametrize("changes", [
    {"document_id": None}, {"job_id": None}, {"mailbox": "wrong@example.test"},
    {"tenant_id": "wrong"}, {"attachment_id": "wrong"}, {"business_outcome": "UNKNOWN"},
])
def test_linkage_fails_closed(store, tmp_path, changes):
    gate = seed(store, tmp_path)
    store.update_email_intake_receipt("receipt-0", changes)
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "INELIGIBLE"


def test_ambiguous_document_link(store, tmp_path):
    gate = seed(store, tmp_path, ("REGISTERED", "REGISTERED"))
    store.update_email_intake_receipt("receipt-1", {"document_id": "doc-0"})
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "INELIGIBLE"


def test_failed_job_never_registered(store, tmp_path):
    gate = seed(store, tmp_path)
    store.update_job("job-0", status="FAILED")
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "INELIGIBLE"


def test_pdf_missing_blocks_success(store, tmp_path):
    gate = seed(store, tmp_path)
    store.delete_document_object(store.documents["doc-0"])
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "NOT_READY"


def test_cache_not_authoritative(store, tmp_path):
    gate = seed(store, tmp_path, ("HUMAN_REVIEW",))
    store.documents["doc-0"].status = "REGISTERED"
    store.email_intake_receipts["receipt-0"]["business_outcome"] = "REGISTERED"
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "INELIGIBLE"


def test_persistence_failure_never_eligible(store, tmp_path):
    gate = seed(store, tmp_path)
    with patch.object(store, "update_gmail_message_state_fields", side_effect=OSError("unavailable")):
        assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "NOT_READY"
    with patch.object(store, "read_persisted_record", side_effect=OSError("unavailable")):
        assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "NOT_READY"


def test_identities_and_wrong_tenant(store, tmp_path):
    gate = seed(store, tmp_path)
    assert len({message_state_id(t, m, MESSAGE) for t in (TENANT, "other")
                for m in (MAILBOX, "other@example.test")}) == 4
    assert gate.evaluate_message_for_processed_label("other", MAILBOX, MESSAGE) == "INELIGIBLE"
    assert gate.evaluate_message_for_processed_label(TENANT, "other@example.test", MESSAGE) == "INELIGIBLE"


def test_manifest_cannot_shrink(store, tmp_path):
    gate = seed(store, tmp_path, ("REGISTERED", "HUMAN_REVIEW"))
    gate.prepare(TENANT, MAILBOX, MESSAGE, ["receipt-0"], ["attachment-0"])
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "INELIGIBLE"


def test_restart_duplicate_re_evaluates_without_pipeline(store, tmp_path):
    helper = email_helpers.EmailIntakeFoundationTests()
    message = helper.message()
    auth = AuthContext("test-user", TENANT, authenticated=True)
    processor = JobProcessor(store, auth)
    processor.email_intake.work_dir = tmp_path / "intake"
    with patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"), \
         patch("agents.document.agent.extract_with_gemini", return_value=helper.gemini_payload()) as gemini:
        result = processor.email_intake.process_message(message, provider_name="gmail", verified_mailbox=MAILBOX)
        assert result.submitted_job_id
        assert gemini.call_count == 1
    if isinstance(store, LocalStore):
        reloaded = LocalStore(store.path)
    else:
        reloaded = CloudStore(store.config, store.firestore, store.object_storage)
    processor = JobProcessor(reloaded, auth)
    with patch.object(processor.email_intake, "submit_files", side_effect=AssertionError("new job")), \
         patch.object(processor.email_intake, "run_job", side_effect=AssertionError("reprocessing")), \
         patch.object(reloaded, "store_document_bytes", side_effect=AssertionError("upload")):
        duplicate = processor.email_intake.process_message(message, provider_name="gmail", verified_mailbox=MAILBOX)
    assert duplicate.submitted_job_id is None
    gate = MessageSuccessGate(reloaded)
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, message.metadata.provider_message_id) == "ELIGIBLE"


@pytest.mark.parametrize("job_status,outcome,expected", [
    ("COMPLETED", "HUMAN_REVIEW", "HUMAN_REVIEW"), ("FAILED", "QUEUED", "FAILED")])
def test_receipt_reports_returned_business_failure(store, tmp_path, job_status, outcome, expected):
    helper = email_helpers.EmailIntakeFoundationTests()
    processor = JobProcessor(store, AuthContext("test-user", TENANT, authenticated=True))
    processor.email_intake.work_dir = tmp_path / "intake"
    def run(job_id):
        store.update_job(job_id, status=job_status)
        for doc in store.documents_for_job(job_id):
            store.update_document(doc.id, status=outcome)
        return {}
    processor.email_intake.run_job = run
    processor.email_intake.process_message(helper.message(), provider_name="gmail", verified_mailbox=MAILBOX)
    receipt = next(iter(store.email_intake_receipts.values()))
    assert receipt["status"] == "COMPLETED"
    assert receipt["business_outcome"] == expected
    assert MessageSuccessGate(store).evaluate_message_for_processed_label(
        TENANT, MAILBOX, helper.message().metadata.provider_message_id) == "INELIGIBLE"


def test_same_filename_has_explicit_distinct_links(store, tmp_path):
    helper = email_helpers.EmailIntakeFoundationTests()
    first = helper.message()
    message = replace(first, attachments=(first.attachments[0], replace(first.attachments[0], attachment_id="second")))
    processor = JobProcessor(store, AuthContext("test-user", TENANT, authenticated=True))
    processor.email_intake.work_dir = tmp_path / "intake"
    with patch("agents.adk.orchestrator.FlowOpsAdkOrchestrator._confirm_adk_runtime", return_value="READY"), \
         patch("agents.document.agent.extract_with_gemini", side_effect=[helper.gemini_payload("INV-101"), helper.gemini_payload("INV-102")]):
        processor.email_intake.process_message(message, provider_name="gmail", verified_mailbox=MAILBOX)
    receipts = list(store.email_intake_receipts.values())
    assert len({r["document_id"] for r in receipts}) == 2
    assert {r["attachment_id"] for r in receipts} == {"att-001", "second"}
    for receipt in receipts:
        assert store.read_persisted_record("documents", receipt["document_id"])["email_receipt_id"] == receipt["id"]
    assert MessageSuccessGate(store).evaluate_message_for_processed_label(TENANT, MAILBOX, message.metadata.provider_message_id) == "ELIGIBLE"


def test_manifest_mailbox_and_tenant_records_do_not_collide(store):
    gate = MessageSuccessGate(store)
    ids = [gate.prepare(t, m, MESSAGE, [], []) for t in (TENANT, "other")
           for m in (MAILBOX, "other@example.test")]
    assert len(set(ids)) == 4
    for state_id in ids:
        assert store.read_persisted_record("gmail_message_states", state_id)["id"] == state_id


def test_legacy_receipt_blocks_new_mailbox_intake(store, tmp_path):
    helper = email_helpers.EmailIntakeFoundationTests()
    message = helper.message()
    processor = JobProcessor(store, AuthContext("test-user", TENANT, authenticated=True))
    intake = processor.email_intake
    legacy_id = intake._receipt_id(TENANT, "gmail", message, message.attachments[0])
    store.claim_email_intake_receipt(legacy_id, {"id": legacy_id, "tenant_id": TENANT, "status": "COMPLETED"})
    with patch.object(intake, "submit_files", side_effect=AssertionError("legacy reprocessed")):
        result = intake.process_message(message, provider_name="gmail", verified_mailbox=MAILBOX)
    assert result.duplicate
    assert MessageSuccessGate(store).evaluate_message_for_processed_label(TENANT, MAILBOX, message.metadata.provider_message_id) == "INELIGIBLE"


def test_cloud_failed_receipt_write_preserves_persisted_state(tmp_path):
    backend = FakeFirestoreBackend()
    store = CloudStore(CloudStoreConfig("test", "(default)", "test"), backend, FakeObjectStorage())
    gate = seed(store, tmp_path, ("PROCESSING",))
    backend.fail_writes = True
    with pytest.raises(Exception, match="CloudStore write failed"):
        store.complete_email_intake_receipt("receipt-0", {"processing_state": "COMPLETED", "business_outcome": "REGISTERED"})
    assert store.email_intake_receipts["receipt-0"]["processing_state"] == "PROCESSING"
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "NOT_READY"


def test_gate_before_receipt_commit_not_ready(store, tmp_path):
    gate = seed(store, tmp_path)
    store.update_email_intake_receipt("receipt-0", {"status": "PROCESSING", "processing_state": "PROCESSING"})
    assert gate.evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "NOT_READY"


def test_gmail_provider_only_reads_and_requires_trusted_mailbox_for_state(store, tmp_path):
    from tools.email_intake.gmail_provider import GmailEmailIntakeProvider
    class ReadOnlyClient:
        def fetch_message(self, message_id):
            return {"id": message_id, "payload": {"headers": [{"name": "To", "value": MAILBOX}]}}
        def __getattr__(self, name):
            raise AssertionError("Unexpected Gmail operation: " + name)
    processor = JobProcessor(store, AuthContext("test-user", TENANT, authenticated=True))
    processor.process_email_provider(GmailEmailIntakeProvider(ReadOnlyClient(), [MESSAGE]), work_dir=tmp_path)
    assert not store.gmail_message_states
    processor.process_email_provider(GmailEmailIntakeProvider(ReadOnlyClient(), [MESSAGE], verified_mailbox=MAILBOX), work_dir=tmp_path)
    assert MessageSuccessGate(store).evaluate_message_for_processed_label(TENANT, MAILBOX, MESSAGE) == "INELIGIBLE"
