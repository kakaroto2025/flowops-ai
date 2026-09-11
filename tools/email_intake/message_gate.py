"""Durable eligibility only. This module has no Gmail client or write capability."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from shared.models import Document, PersistenceStore
from shared.models.entities import utc_now


def message_state_id(tenant_id: str, mailbox: str, message_id: str) -> str:
    return hashlib.sha256(json.dumps(
        [tenant_id, mailbox.strip().lower(), message_id], separators=(",", ":")
    ).encode()).hexdigest()


class MessageSuccessGate:
    def __init__(self, store: PersistenceStore):
        self.store = store

    def prepare(self, tenant_id: str, mailbox: str, message_id: str,
                receipt_ids: list[str], attachment_ids: list[str], *, rejected: bool = False) -> str:
        mailbox = mailbox.strip().lower()
        if not tenant_id or not mailbox or "@" not in mailbox or not message_id:
            raise ValueError("Verified mailbox and tenant/message identity required")
        state_id = message_state_id(tenant_id, mailbox, message_id)
        existing = self.store.read_persisted_record("gmail_message_states", state_id)
        manifest = sorted(zip(receipt_ids, attachment_ids))
        ambiguous = len(set(receipt_ids)) != len(receipt_ids) or len(set(attachment_ids)) != len(attachment_ids)
        if existing:
            previous = sorted(zip(existing["receipt_ids"], existing["attachment_ids"]))
            if previous != manifest or rejected or ambiguous:
                self._update_eligibility_fields(state_id, {
                    "manifest_unsafe": True,
                    "eligibility_state": "INELIGIBLE",
                    "eligibility_reason": "manifest_unsafe",
                })
            return state_id
        now = utc_now()
        self.store.put_gmail_message_state(state_id, {
            "id": state_id, "tenant_id": tenant_id, "mailbox": mailbox,
            "provider_message_id": message_id,
            "receipt_ids": receipt_ids, "attachment_ids": attachment_ids,
            "manifest_unsafe": rejected or ambiguous or not receipt_ids,
            "eligibility_state": "NOT_READY", "gmail_state": "PENDING",
            "created_at": now, "updated_at": now,
        })
        return state_id

    def evaluate_message_for_processed_label(self, tenant_id: str, mailbox: str, message_id: str) -> str:
        mailbox = mailbox.strip().lower()
        state_id = message_state_id(tenant_id, mailbox, message_id)
        try:
            state = self.store.read_persisted_record("gmail_message_states", state_id)
            if not state or (state.get("tenant_id"), state.get("mailbox"), state.get("provider_message_id")) != (
                tenant_id, mailbox, message_id
            ):
                return "INELIGIBLE"
            # Revoke stale positive eligibility before checking current durable evidence.
            self._update_eligibility_fields(state_id, {"eligibility_state": "NOT_READY"})
            verdict = self._evaluate(state)
            self._update_eligibility_fields(state_id, {"eligibility_state": verdict})
            return verdict
        except Exception:
            # Never trust a cache, fallback backup, or old eligibility after an I/O failure.
            return "NOT_READY"

    def _update_eligibility_fields(self, state_id: str, changes: dict[str, Any]) -> None:
        # The success gate owns eligibility. Gmail synchronization state is updated by a separate workflow.
        self.store.update_gmail_message_state_fields(state_id, {**changes, "eligibility_evaluated_at": utc_now(), "updated_at": utc_now()})

    def _evaluate(self, state: dict[str, Any]) -> str:
        receipts = state.get("receipt_ids", [])
        attachments = state.get("attachment_ids", [])
        if (state.get("manifest_unsafe") or not receipts or len(receipts) != len(attachments)
                or len(set(receipts)) != len(receipts) or len(set(attachments)) != len(attachments)):
            return "INELIGIBLE"
        results = []
        document_ids: set[str] = set()
        for receipt_id, attachment_id in zip(receipts, attachments):
            receipt = self.store.read_persisted_record("email_intake_receipts", receipt_id)
            if not receipt or any(receipt.get(k) != state[k] for k in (
                "tenant_id", "mailbox", "provider_message_id"
            )) or receipt.get("attachment_id") != attachment_id or receipt.get("provider") != "gmail":
                return "INELIGIBLE"
            if receipt.get("document_id") in document_ids:
                return "INELIGIBLE"
            if receipt.get("document_id"):
                document_ids.add(receipt["document_id"])
            results.append(self._attachment(receipt_id, receipt))
        if "INELIGIBLE" in results:
            return "INELIGIBLE"
        return "NOT_READY" if "NOT_READY" in results else "ELIGIBLE"

    def _attachment(self, receipt_id: str, receipt: dict[str, Any]) -> str:
        processing = receipt.get("processing_state")
        outcome = receipt.get("business_outcome")
        if processing == "FAILED" or outcome in {"FAILED", "HUMAN_REVIEW", "REJECTED", "DUPLICATE_BLOCKED"}:
            return "INELIGIBLE"
        if processing == "PROCESSING":
            return "NOT_READY"
        if not receipt.get("job_id") or not receipt.get("document_id"):
            return "INELIGIBLE"
        job = self.store.read_persisted_record("jobs", receipt["job_id"])
        doc = self.store.read_persisted_record("documents", receipt["document_id"])
        if not job or not doc:
            return "NOT_READY"
        if (job.get("tenant_id") != receipt["tenant_id"] or doc.get("tenant_id") != receipt["tenant_id"]
                or doc.get("job_id") != job.get("id") or doc.get("email_receipt_id") != receipt_id
                or doc.get("id") != receipt["document_id"]):
            return "INELIGIBLE"
        if job.get("status") == "FAILED" or doc.get("status") in {
            "FAILED", "HUMAN_REVIEW", "REJECTED", "DUPLICATE_BLOCKED"
        }:
            return "INELIGIBLE"
        if job.get("status") != "COMPLETED" or doc.get("status") in {
            "UPLOADED", "QUEUED", "PROCESSING", "EXTRACTED", "VALIDATING", "RETRY", "APPROVED"
        }:
            return "NOT_READY"
        if (receipt.get("status") != "COMPLETED" or processing != "COMPLETED"
                or outcome != "REGISTERED" or doc.get("status") != "REGISTERED"):
            return "INELIGIBLE"
        for entity, key in (("extractions", "extraction_id"), ("erp_records", "erp_record_id")):
            if not receipt.get(key):
                return "NOT_READY"
            record = self.store.read_persisted_record(entity, receipt[key])
            if not record:
                return "NOT_READY"
            if (record.get("id") != receipt[key] or record.get("document_id") != doc["id"]
                    or record.get("tenant_id") != receipt["tenant_id"]):
                return "INELIGIBLE"
            if entity == "erp_records" and (record.get("job_id") != job["id"] or record.get("status") != "REGISTERED"):
                return "INELIGIBLE"
        if not receipt.get("pdf_persisted") or not receipt.get("object_uri"):
            return "NOT_READY"
        metadata = self.store.document_object_metadata(Document(**doc))
        if not metadata or metadata.get("size", 0) <= 0:
            return "NOT_READY"
        return "ELIGIBLE"
