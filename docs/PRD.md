# FlowOps AI v1.0 PRD

## Product Vision

FlowOps AI is an AI workforce for document operations. It receives document batches, plans the workflow, extracts structured information, validates results, decides what can be approved, escalates exceptions, registers approved records in a simulated ERP, and produces an auditable operational report.

The product does not only answer questions about documents. It completes document-processing jobs.

## Hackathon Positioning

FlowOps AI is designed for the Taskmaster category: an agentic system that executes a complete workflow with autonomous planning, tool usage, validation, recovery, human escalation, memory, and auditability.

The v1.0 demo focuses on one concrete customer scenario: Alfa Contabilidade Ltda.

## Demo Customer

Alfa Contabilidade Ltda is an accounting office with 18 employees serving about 230 companies. Customers send hundreds of fiscal PDFs every day. Employees manually download PDFs from Outlook, read invoice data, type it into an ERP, move the PDF to a processed folder, and prepare daily reports.

Daily volume:
- 300 to 500 PDFs per day
- 9,000 to 12,000 documents per month

Current problems:
- manual work
- typing errors
- delayed processing
- rework
- overtime
- customer complaints

## v1.0 Goal

Build a working vertical slice that processes fiscal documents end to end:

1. Receive a batch of PDF documents.
2. Create a job.
3. Classify and extract data from each document.
4. Validate required fields and business rules.
5. Retry recoverable extraction issues.
6. Send low-confidence documents to human review.
7. Register approved invoices in a mock ERP.
8. Persist audit events.
9. Show job status, decisions, and metrics in a dashboard.
10. Generate a final report.

## Required Extracted Fields

- file name
- company name
- CNPJ
- invoice number
- issue date
- total amount
- processing timestamp
- extraction confidence

## Out Of Scope For v1.0

- real Outlook integration
- real SAP, TOTVS, or accounting ERP integration
- payments
- WhatsApp
- mobile app
- multiple business domains
- custom model training
- destructive automated actions

The ERP is simulated through a controlled internal tool named `register_invoice`.

## Product Principle

Use Gemini for ambiguity and interpretation. Use deterministic code for objective rules.

Examples:
- Gemini decides which number in the document is the issuer CNPJ.
- Python validates whether the CNPJ format/check digits are valid.
- Gemini extracts ambiguous invoice fields.
- Python checks required fields, duplicates, date format, and currency consistency.

## Definition Of Done

FlowOps AI v1.0 is ready when a user can start a batch and watch documents move through the pipeline without manual intervention, including:

- autonomous agent execution
- structured extraction
- deterministic validation
- retry for recoverable failures
- human review for unresolved exceptions
- mock ERP registration for approved invoices
- dashboard visibility
- complete audit trail
- final report generation

