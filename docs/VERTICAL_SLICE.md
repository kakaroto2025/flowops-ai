# Vertical Slice Implementation Plan

The first implementation goal is a local end-to-end workflow that proves the product.

## Scope

Build this path first:

```text
Upload PDF
  -> create job
  -> create document record
  -> extract fields
  -> validate fields
  -> decide outcome
  -> register approved invoice in mock ERP
  -> save audit trail
  -> show dashboard metrics
```

## Local Components

Backend:
- FastAPI
- in-memory or local JSON repository first
- file upload endpoint
- job status endpoint
- audit events endpoint
- report endpoint

Frontend:
- Next.js dashboard
- upload/start demo button
- KPI cards
- job table
- document queue
- agent events
- human review panel

Agent runtime:
- Python services with agent-like contracts first
- Google ADK integration after the local workflow works

LLM:
- Gemini extraction adapter
- fallback mock extractor for development without API key

## Implementation Order

1. Define shared schemas.
2. Build local repositories.
3. Build validation tools.
4. Build mock ERP tool.
5. Build agent service classes.
6. Build FastAPI endpoints.
7. Build seeded sample data.
8. Build dashboard.
9. Add Gemini adapter.
10. Add Google ADK orchestration.

## First API Endpoints

```text
POST /jobs
GET  /jobs
GET  /jobs/{job_id}
POST /jobs/{job_id}/documents
POST /jobs/{job_id}/run
GET  /jobs/{job_id}/events
GET  /jobs/{job_id}/report
POST /human-reviews/{review_id}/resolve
```

## Acceptance Criteria

- user can start a demo job
- at least 5 sample documents are processed
- at least 1 approved document is registered in mock ERP
- at least 1 document is retried
- at least 1 document goes to human review
- audit events are visible in the dashboard
- final report can be generated

