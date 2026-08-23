# FlowOps AI Demo Script

Target duration: 4 minutes.

## Story

Alfa Contabilidade receives hundreds of fiscal PDFs every day. Before FlowOps AI, employees manually downloaded, opened, read, typed, moved, and reported each document.

FlowOps AI turns that workflow into an auditable AI workforce.

## Demo Flow

### 0:00 - 0:30 Opening

Show the dashboard.

Narration:

```text
FlowOps AI is an AI workforce for document operations.
It does not only read documents. It completes operational jobs.
```

### 0:30 - 1:00 Start A Batch

Upload or start a seeded batch for Alfa Contabilidade.

Show:

- job created
- documents queued
- agent activity begins

### 1:00 - 2:00 Agents Working

Show document cards moving through states:

```text
UPLOADED -> QUEUED -> PROCESSING -> EXTRACTED -> VALIDATING
```

Show agent logs:

- Intake Agent created job
- Document Agent extracted fields
- Validation Agent checked CNPJ/date/amount
- Decision Agent approved a document
- Mock ERP registered invoice

### 2:00 - 2:50 Exception Handling

Show one document with an extraction anomaly.

Example:

```text
Extracted amount: 187500.00
Expected pattern suggests: 18750.00
Decision: RETRY
```

Then show retry success.

Show another document going to human review:

```text
Reason: low confidence total_amount
Decision: HUMAN_REVIEW_REQUIRED
```

Human corrects one field. Document is approved.

### 2:50 - 3:30 Reporting

Show:

- processed documents
- success rate
- human reviews
- average processing time
- active jobs
- recent decisions
- audit trail

Generate CSV/XLSX report.

### 3:30 - 4:00 Closing

Show architecture slide or page.

Narration:

```text
Every action is tool-based, validated, and auditable.
Gemini handles ambiguity. Deterministic code handles business rules.
FlowOps AI is built to run on Google Cloud with ADK, Gemini, Firestore,
Cloud Storage, Pub/Sub, and Cloud Run.
```

Final line:

```text
FlowOps AI turns document chaos into autonomous, auditable operations.
```

