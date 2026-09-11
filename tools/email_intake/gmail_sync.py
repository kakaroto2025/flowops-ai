from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from shared.models import PersistenceStore
from shared.models.entities import utc_now

from .message_gate import MessageSuccessGate, message_state_id


PROCESSED_LABEL_NAME = "FlowOps/Processed"


class GmailProcessedLabelError(RuntimeError):
    pass


class GmailProcessedLabelClient(Protocol):
    def users(self): ...


@dataclass(frozen=True)
class GmailProcessedLabelResult:
    status: str
    eligibility_state: str
    gmail_state: str | None
    label_id: str | None = None
    claimed: bool = False


class GmailProcessedLabelAdapter:
    def __init__(self, service: GmailProcessedLabelClient):
        self.service = service

    def ensure_processed_label(self) -> str:
        existing = self._find_processed_label()
        if existing:
            return existing
        payload = {
            "name": PROCESSED_LABEL_NAME,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = self.service.users().labels().create(userId="me", body=payload).execute()
        label_id = str(created.get("id") or "").strip()
        if not label_id:
            raise GmailProcessedLabelError("Gmail label creation did not return a label id.")
        return label_id

    def apply_processed_label(self, message_id: str, label_id: str) -> bool:
        current = self.service.users().messages().get(
            userId="me",
            id=message_id,
            format="minimal",
            fields="id,labelIds",
        ).execute()
        if label_id in set(current.get("labelIds") or []):
            return False
        self.service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id], "removeLabelIds": []},
        ).execute()
        return True

    def _find_processed_label(self) -> str | None:
        labels = self.service.users().labels().list(userId="me").execute().get("labels", [])
        for label in labels:
            if label.get("name") == PROCESSED_LABEL_NAME:
                label_id = str(label.get("id") or "").strip()
                if label_id:
                    return label_id
        return None


class GmailProcessedLabelSynchronizer:
    def __init__(
        self,
        store: PersistenceStore,
        service: GmailProcessedLabelClient,
        *,
        gate: MessageSuccessGate | None = None,
    ):
        self.store = store
        self.gate = gate or MessageSuccessGate(store)
        self.labels = GmailProcessedLabelAdapter(service)

    def sync_processed_label(self, tenant_id: str, mailbox: str, provider_message_id: str) -> GmailProcessedLabelResult:
        mailbox = mailbox.strip().lower()
        state_id = message_state_id(tenant_id, mailbox, provider_message_id)
        eligibility = self.gate.evaluate_message_for_processed_label(tenant_id, mailbox, provider_message_id)
        if eligibility != "ELIGIBLE":
            state = self.store.read_persisted_record("gmail_message_states", state_id)
            return GmailProcessedLabelResult(
                status="BLOCKED",
                eligibility_state=eligibility,
                gmail_state=state.get("gmail_state") if state else None,
            )

        claimed, state = self.store.claim_gmail_message_sync(
            state_id,
            tenant_id=tenant_id,
            mailbox=mailbox,
            provider_message_id=provider_message_id,
        )
        if not claimed:
            return GmailProcessedLabelResult(
                status="CLAIM_BLOCKED",
                eligibility_state=eligibility,
                gmail_state=state.get("gmail_state") if state else None,
                label_id=state.get("label_id") if state else None,
                claimed=False,
            )

        try:
            label_id = self.labels.ensure_processed_label()
            self.labels.apply_processed_label(provider_message_id, label_id)
        except Exception as exc:
            self.store.update_gmail_message_state_fields(state_id, {
                "gmail_state": "ERROR",
                "gmail_sync_error_code": type(exc).__name__,
                "last_attempt_at": utc_now(),
                "updated_at": utc_now(),
            })
            raise GmailProcessedLabelError("Gmail processed-label synchronization failed.") from exc

        self.store.update_gmail_message_state_fields(state_id, {
            "gmail_state": "SYNCED",
            "label_id": label_id,
            "gmail_synced_at": utc_now(),
            "gmail_sync_error_code": None,
            "updated_at": utc_now(),
        })
        return GmailProcessedLabelResult(
            status="SYNCED",
            eligibility_state=eligibility,
            gmail_state="SYNCED",
            label_id=label_id,
            claimed=True,
        )
