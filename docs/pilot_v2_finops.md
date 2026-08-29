# FlowOps AI Pilot v2 FinOps Cost Guard

This document describes the Pilot v2 cost guard. It is an MVP control layer for safe experimentation and does not create a hard spending cap.

## Goals

- Keep Pilot v2 free-tier-first by default.
- Prevent oversized uploads before processing.
- Limit daily document volume.
- Limit daily Gemini calls.
- Track available Gemini usage metadata without inventing token counts.
- Estimate AI cost only when pricing configuration is explicitly provided.

## Budget Alert vs Internal Cost Guard

The Google Cloud Billing Budget Alert created in Phase 0A is only a billing notification layer. It is not a hard spending cap and it does not automatically stop Cloud Run, Gemini or any Google Cloud service.

The FlowOps Cost Guard is an application-level control. It can block oversized documents, stop additional Gemini calls, or continue with local parser fallback when internal limits are reached. It does not modify the Billing Account and does not directly control Google Cloud billing.

## Environment

```env
FREE_TIER_FIRST=true
FLOWOPS_DAILY_DOCUMENT_LIMIT=50
FLOWOPS_DAILY_GEMINI_CALL_LIMIT=100
FLOWOPS_MAX_FILE_SIZE_MB=10
FLOWOPS_MONTHLY_SOFT_BUDGET_BRL=50
FLOWOPS_USD_BRL_RATE=
FLOWOPS_GEMINI_INPUT_PRICE_PER_MILLION_TOKENS=
FLOWOPS_GEMINI_OUTPUT_PRICE_PER_MILLION_TOKENS=
```

`FLOWOPS_USD_BRL_RATE` is optional and manual. The application does not fetch exchange rates.

## What Is Tracked

- tenant id
- job id
- document id
- timestamp
- document type
- country
- file size
- Gemini model
- Gemini call count
- input, output and total tokens when provided by Gemini
- estimated AI cost in USD when token pricing is configured
- estimated AI cost in BRL when USD cost and manual FX rate are configured
- fallback usage
- processing status
- cost guard blocks and warnings

## Guard Behavior

- File size above `FLOWOPS_MAX_FILE_SIZE_MB` blocks the document before processing.
- Daily documents at or above `FLOWOPS_DAILY_DOCUMENT_LIMIT` block additional documents.
- Daily Gemini calls at or above `FLOWOPS_DAILY_GEMINI_CALL_LIMIT` block Gemini and keep the local parser fallback available.
- Monthly BRL soft budget at or above `FLOWOPS_MONTHLY_SOFT_BUDGET_BRL` blocks Gemini calls. This is an internal software guard, not a Google Cloud billing cap.

## Endpoint

`GET /api/finops/usage`

The endpoint is read-only and returns aggregate usage. It does not expose secrets, API keys, billing account identifiers or credentials.

## Audit Events

- `FINOPS_ALLOW`
- `FINOPS_WARNING`
- `FINOPS_BLOCK`
- `GEMINI_USAGE_RECORDED`
- `FINOPS_USAGE_RECORDED`

## MVP Limitation

Current persistence uses `LocalStore/state.json`. In Cloud Run this state is ephemeral. Firestore or another managed datastore remains the recommended production persistence layer.

Future phases may connect these usage records to managed persistence and official billing exports, but Phase 0B deliberately avoids creating new Cloud resources.
