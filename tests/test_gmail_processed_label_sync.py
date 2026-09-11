from __future__ import annotations

import pytest

from shared.models import CloudStore, CloudStoreConfig, LocalStore
from test_cloud_store import FakeFirestoreBackend, FakeObjectStorage
from test_gmail_message_gate import MAILBOX, MESSAGE, TENANT, seed
from tools.email_intake.gmail_sync import (
    PROCESSED_LABEL_NAME,
    GmailProcessedLabelAdapter,
    GmailProcessedLabelError,
    GmailProcessedLabelSynchronizer,
)
from tools.email_intake.message_gate import message_state_id


@pytest.fixture(params=["local", "cloud"])
def store(request, tmp_path):
    if request.param == "local":
        return LocalStore(tmp_path / "state.json")
    return CloudStore(CloudStoreConfig("test", "(default)", "test"), FakeFirestoreBackend(), FakeObjectStorage())


class FakeGmailService:
    def __init__(self, *, labels=None, message_labels=None, fail_modify=False, fail_create=False):
        self.label_payloads = list(labels or [])
        self.message_labels = list(message_labels or [])
        self.fail_modify = fail_modify
        self.fail_create = fail_create
        self.label_list_calls = 0
        self.label_create_bodies = []
        self.message_get_calls = []
        self.message_modify_bodies = []
        self.archive_calls = 0
        self.trash_calls = 0
        self.delete_calls = 0
        self.send_calls = 0

    def users(self):
        return self

    def labels(self):
        return self

    def messages(self):
        return self

    def list(self, userId):
        self.label_list_calls += 1
        self._operation = "list_labels"
        return self

    def create(self, userId, body):
        if self.fail_create:
            raise RuntimeError("create failed")
        self.label_create_bodies.append(dict(body))
        self._created = {"id": "Label_Created", **body}
        self.label_payloads.append(self._created)
        self._operation = "create_label"
        return self

    def get(self, userId, id, format=None, fields=None):
        self.message_get_calls.append((id, format, fields))
        self._operation = "get_message"
        return self

    def modify(self, userId, id, body):
        if self.fail_modify:
            raise RuntimeError("modify failed")
        self.message_modify_bodies.append(dict(body))
        for label_id in body.get("addLabelIds", []):
            if label_id not in self.message_labels:
                self.message_labels.append(label_id)
        self._operation = "modify_message"
        return self

    def trash(self, *args, **kwargs):
        self.trash_calls += 1
        raise AssertionError("trash is not allowed")

    def delete(self, *args, **kwargs):
        self.delete_calls += 1
        raise AssertionError("delete is not allowed")

    def send(self, *args, **kwargs):
        self.send_calls += 1
        raise AssertionError("send is not allowed")

    def execute(self):
        if self._operation == "list_labels":
            return {"labels": self.label_payloads}
        if self._operation == "create_label":
            return self._created
        if self._operation == "get_message":
            return {"id": MESSAGE, "labelIds": list(self.message_labels)}
        if self._operation == "modify_message":
            return {"id": MESSAGE, "labelIds": list(self.message_labels)}
        raise AssertionError("unexpected Gmail operation")


def test_exact_processed_label_reused_without_creation():
    service = FakeGmailService(labels=[{"id": "Label_Processed", "name": PROCESSED_LABEL_NAME}])

    assert GmailProcessedLabelAdapter(service).ensure_processed_label() == "Label_Processed"

    assert service.label_list_calls == 1
    assert service.label_create_bodies == []


def test_missing_processed_label_creation_uses_exact_policy():
    service = FakeGmailService()

    label_id = GmailProcessedLabelAdapter(service).ensure_processed_label()

    assert label_id == "Label_Created"
    assert service.label_create_bodies == [{
        "name": PROCESSED_LABEL_NAME,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }]


def test_label_application_only_adds_expected_label_and_removes_nothing():
    service = FakeGmailService(labels=[{"id": "Label_Processed", "name": PROCESSED_LABEL_NAME}])

    changed = GmailProcessedLabelAdapter(service).apply_processed_label(MESSAGE, "Label_Processed")

    assert changed is True
    assert service.message_modify_bodies == [{"addLabelIds": ["Label_Processed"], "removeLabelIds": []}]
    assert "UNREAD" not in service.message_modify_bodies[0]["removeLabelIds"]
    assert "INBOX" not in service.message_modify_bodies[0]["removeLabelIds"]
    assert service.trash_calls == 0
    assert service.delete_calls == 0
    assert service.send_calls == 0


def test_existing_message_label_is_idempotent_success():
    service = FakeGmailService(
        labels=[{"id": "Label_Processed", "name": PROCESSED_LABEL_NAME}],
        message_labels=["Label_Processed", "UNREAD", "INBOX"],
    )

    changed = GmailProcessedLabelAdapter(service).apply_processed_label(MESSAGE, "Label_Processed")

    assert changed is False
    assert service.message_modify_bodies == []
    assert service.message_labels == ["Label_Processed", "UNREAD", "INBOX"]


def test_arbitrary_gmail_mutation_api_is_not_exposed():
    adapter = GmailProcessedLabelAdapter(FakeGmailService())

    assert not hasattr(adapter, "modify_message")
    assert not hasattr(adapter, "modify_labels")
    assert not hasattr(adapter, "send")
    assert not hasattr(adapter, "trash")
    assert not hasattr(adapter, "delete")
    assert not hasattr(adapter, "archive")


@pytest.mark.parametrize("outcome,expected", [("PROCESSING", "NOT_READY"), ("HUMAN_REVIEW", "INELIGIBLE")])
def test_non_eligible_states_block_before_gmail_write(store, tmp_path, outcome, expected):
    seed(store, tmp_path, (outcome,))
    service = FakeGmailService(labels=[{"id": "Label_Processed", "name": PROCESSED_LABEL_NAME}])

    result = GmailProcessedLabelSynchronizer(store, service).sync_processed_label(TENANT, MAILBOX, MESSAGE)

    assert result.status == "BLOCKED"
    assert result.eligibility_state == expected
    assert service.label_list_calls == 0
    assert service.message_modify_bodies == []


def test_eligible_message_claims_and_syncs_processed_label(store, tmp_path):
    seed(store, tmp_path)
    service = FakeGmailService(labels=[{"id": "Label_Processed", "name": PROCESSED_LABEL_NAME}])

    result = GmailProcessedLabelSynchronizer(store, service).sync_processed_label(TENANT, MAILBOX, MESSAGE)

    assert result.status == "SYNCED"
    assert result.claimed is True
    state = store.read_persisted_record("gmail_message_states", message_state_id(TENANT, MAILBOX, MESSAGE))
    assert state["gmail_state"] == "SYNCED"
    assert state["label_id"] == "Label_Processed"
    assert state["attempt_count"] == 1
    assert service.message_modify_bodies == [{"addLabelIds": ["Label_Processed"], "removeLabelIds": []}]


def test_second_concurrent_worker_is_blocked_by_atomic_claim(store, tmp_path):
    seed(store, tmp_path)
    state_id = message_state_id(TENANT, MAILBOX, MESSAGE)

    first, first_state = store.claim_gmail_message_sync(
        state_id,
        tenant_id=TENANT,
        mailbox=MAILBOX,
        provider_message_id=MESSAGE,
    )
    second, second_state = store.claim_gmail_message_sync(
        state_id,
        tenant_id=TENANT,
        mailbox=MAILBOX,
        provider_message_id=MESSAGE,
    )

    assert first is True
    assert first_state["gmail_state"] == "SYNCING"
    assert second is False
    assert second_state["gmail_state"] == "SYNCING"


def test_gmail_api_failure_marks_sync_error_without_business_reprocessing(store, tmp_path):
    seed(store, tmp_path)
    before = (len(store.jobs), len(store.documents), len(store.extractions), len(store.erp_records))
    service = FakeGmailService(
        labels=[{"id": "Label_Processed", "name": PROCESSED_LABEL_NAME}],
        fail_modify=True,
    )

    with pytest.raises(GmailProcessedLabelError):
        GmailProcessedLabelSynchronizer(store, service).sync_processed_label(TENANT, MAILBOX, MESSAGE)

    after = (len(store.jobs), len(store.documents), len(store.extractions), len(store.erp_records))
    state = store.read_persisted_record("gmail_message_states", message_state_id(TENANT, MAILBOX, MESSAGE))
    assert after == before
    assert state["gmail_state"] == "ERROR"
    assert state["gmail_sync_error_code"] == "RuntimeError"


def test_retry_from_error_runs_gmail_sync_only(store, tmp_path):
    seed(store, tmp_path)
    state_id = message_state_id(TENANT, MAILBOX, MESSAGE)
    store.update_gmail_message_state_fields(state_id, {"gmail_state": "ERROR", "gmail_sync_error_code": "RuntimeError"})
    before = (len(store.jobs), len(store.documents), len(store.extractions), len(store.erp_records))
    service = FakeGmailService(labels=[{"id": "Label_Processed", "name": PROCESSED_LABEL_NAME}])

    result = GmailProcessedLabelSynchronizer(store, service).sync_processed_label(TENANT, MAILBOX, MESSAGE)

    after = (len(store.jobs), len(store.documents), len(store.extractions), len(store.erp_records))
    assert result.status == "SYNCED"
    assert after == before
    assert service.message_modify_bodies == [{"addLabelIds": ["Label_Processed"], "removeLabelIds": []}]


def test_tenant_and_mailbox_isolation_block_sync_claim(store, tmp_path):
    seed(store, tmp_path)

    wrong_tenant = GmailProcessedLabelSynchronizer(store, FakeGmailService()).sync_processed_label(
        "tenant_other", MAILBOX, MESSAGE
    )
    wrong_mailbox = GmailProcessedLabelSynchronizer(store, FakeGmailService()).sync_processed_label(
        TENANT, "other@example.test", MESSAGE
    )

    assert wrong_tenant.status == "BLOCKED"
    assert wrong_mailbox.status == "BLOCKED"
