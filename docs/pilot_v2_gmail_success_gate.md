# Durable Gmail message success gate

`MessageSuccessGate` evaluates persisted records only. It never calls Gmail.
The trusted OAuth caller supplies `verified_mailbox` to `GmailEmailIntakeProvider`
after checking the authenticated profile. Email sender/recipient headers cannot
establish mailbox identity. Providers without that context retain intake support,
but cannot create an eligible Gmail message state.

## Receipt semantics

`status` retains the Phase 3B3 duplicate-claim contract. `COMPLETED` means the
attempt is closed to automatic reprocessing, not that its invoice was approved.
`processing_state` and `business_outcome` describe the actual pipeline outcome.
Human review, rejection, duplicate block and failures never count as REGISTERED.
Exceptions after side effects retain a blocked receipt with FAILED processing and
business states. Automatic recovery of partially processed attempts is out of scope.

Intake binds each source Path to a receipt ID before document creation. Each
document persists `email_receipt_id`; its receipt records the document/job IDs
and successful PDF storage reference. No filename lookup establishes identity.
After execution the receipt records exact extraction and Mock ERP IDs.

New receipt identities include tenant, verified mailbox, provider, message and
attachment. Historical mailbox-less receipts are left untouched and conservatively
block reprocessing; they cannot prove eligibility. This can block matching IDs in
another mailbox until an explicit future migration resolves their ownership.

## Message state and ordering

`flowops_gmail_message_states` uses a SHA256 identity over a JSON tuple of tenant,
normalized mailbox and message ID. It stores an attachment/receipt manifest, not
email content or credentials. A changed or ambiguous manifest is ineligible.
Rejected attachments conservatively block the entire message, including non-PDFs.

The gate reads exact durable receipt, document, job, extraction and ERP records,
checks bidirectional ownership/linkage, and verifies PDF metadata. It requires
REGISTERED outcomes and COMPLETED job/receipt processing. Missing final records
or unavailable persistence yields NOT_READY; unsafe outcomes/links yield INELIGIBLE.
All attachments must qualify for ELIGIBLE. A pending attachment cannot hide a
different failed attachment. Durable state writes must succeed before returning
ELIGIBLE. Local state writes do not flush uncommitted business caches.

`eligibility_state` and `gmail_state` are separate. ELIGIBLE maps to PENDING,
never SYNCED. Duplicate submissions may reevaluate the manifest by exact IDs
without running the document pipeline again. The eligibility field is evidence
from the last evaluation, not a perpetual authorization: future writers must
invoke the gate again. If persistence is unavailable an old stored positive value
may remain, but the gate returns NOT_READY.

There is no distributed transaction across Firestore, object storage and Gmail.
Concurrent manifest/state updates and human corrections will require a versioned
claim before future Gmail writes. No scheduler, Gmail sync retry, OAuth migration,
label creation or Gmail write capability is added in this phase.
