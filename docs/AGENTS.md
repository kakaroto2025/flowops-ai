# FlowOps AI Agent Specifications v1.0

## Common Rules

All agents must:

- operate only inside a valid job context
- emit structured `agent_events`
- call only allowlisted tools
- return JSON-compatible outputs
- never invent missing required data
- escalate low-confidence or suspicious documents

## Intake Agent

Mission:
Create jobs and register incoming documents.

Input:
- uploaded files
- source metadata
- user id

Output:
- job id
- document records
- queued document list

Allowed tools:
- `create_job`
- `store_document`
- `enqueue_document`
- `log_agent_event`

Forbidden actions:
- extracting invoice data
- approving documents
- registering ERP records

## Document Agent

Mission:
Classify each document and extract structured fiscal data.

Input:
- document id
- document file path/storage uri
- optional retry context

Output:
- document type
- extracted fields
- confidence score
- extraction warnings

Allowed tools:
- `read_pdf`
- `ocr_document`
- `extract_document_with_gemini`
- `save_extraction`
- `log_agent_event`

Forbidden actions:
- approving documents
- registering ERP records
- suppressing low confidence warnings

## Validation Agent

Mission:
Validate extracted data using deterministic rules and business checks.

Input:
- extraction id
- extracted fields
- customer/job context

Output:
- validation status
- field errors
- anomaly warnings
- retry recommendation

Allowed tools:
- `validate_cnpj`
- `validate_date`
- `validate_currency`
- `check_required_fields`
- `check_duplicate_invoice`
- `detect_amount_anomaly`
- `log_agent_event`

Forbidden actions:
- changing extracted values without creating a validation note
- approving documents
- registering ERP records

## Decision Agent

Mission:
Decide the next step for each document.

Input:
- extraction result
- validation result
- retry count
- confidence score

Output:
- `APPROVED`
- `RETRY`
- `HUMAN_REVIEW_REQUIRED`
- `REJECTED`

Allowed tools:
- `retry_document`
- `create_human_review`
- `register_invoice`
- `update_document_state`
- `log_agent_event`

Forbidden actions:
- approving documents with missing required fields
- retrying indefinitely
- registering suspicious documents

Decision rules:
- approve when required fields are present, deterministic validations pass, and confidence is above threshold
- retry once or twice when the failure is recoverable
- escalate to human review when confidence remains low or critical fields conflict
- reject only when the document is unsupported or unreadable after retries

## Reporting Agent

Mission:
Aggregate job results and produce operational reports.

Input:
- job id
- document states
- ERP records
- human review outcomes
- agent events

Output:
- dashboard metrics
- job summary
- CSV/XLSX report
- audit summary

Allowed tools:
- `calculate_job_metrics`
- `generate_csv_report`
- `generate_xlsx_report`
- `update_dashboard_metrics`
- `log_agent_event`

Forbidden actions:
- modifying extraction data
- changing validation decisions
- registering ERP records

