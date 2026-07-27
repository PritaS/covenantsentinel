# CovenantSentinel — MVP Core

Autonomous debt covenant, DSCR, and deadline monitoring for private real
estate operators. This repo is the **working backend core**: the covenant
engine (the IP), data model, API, extraction prompt system, and the three
MVP-critical n8n workflows.

## What's built and verified

| Component | Status |
|---|---|
| Data model (tenants → properties → loans → versioned covenant terms with source citations, financials, test results, alerts, append-only audit log) | ✅ `backend/app/models.py` |
| **Covenant engine**: per-loan NOI definitions (basis, exclusions, imposed reserves, mgmt-fee floors), fixed/floating + IO/amortizing debt service, DSCR / debt-yield / LTV / occupancy tests, trend → projected-breach dating | ✅ `backend/app/covenant_engine.py` · 7 passing unit tests |
| FastAPI: `/financials/ingest`, `/covenants/run-tests`, `/monitoring/upcoming` (deadline clock + **stale-data detection**), `/alerts/{id}/acknowledge`, `/audit`, `/loans/extract` | ✅ `backend/app/main.py` |
| Claude extraction prompt + citation-anchored schema (all extractions land `pending_verification`) | ✅ `backend/app/extraction/prompts.py` |
| Live Claude extraction pipeline: chunking, structured-output extraction, multi-chunk reconciliation, `/loans/extract` persistence | ✅ `backend/app/extraction/pipeline.py` |
| n8n workflows: W1 email ingestion, W2 nightly clock, W5 escalation + 24h ack loop | ✅ `n8n/*.json` (import into n8n, set env vars) |
| Seeded demo: 3-loan portfolio, live DSCR breach + projected occupancy breach + rate-cap clock | ✅ `backend/seed_demo.py` |

## Run the demo

```bash
cd backend
pip install fastapi sqlalchemy python-dateutil httpx uvicorn pytest anthropic
export ANTHROPIC_API_KEY=sk-ant-...   # required only for /loans/extract
python seed_demo.py        # end-to-end covenant run with alerts
python -m pytest tests/    # engine math tests
uvicorn app.main:app --reload   # serve the API for n8n
```

Note: the seed data is intentionally stressed (Marina Vista's tax/insurance
drift is aggressive) so a demo shows every alert type in one run.

## Not built yet (production path, in order)

1. **Postgres + RLS** — swap `DATABASE_URL`, add per-tenant row-level security.
2. **Next.js frontend** — Debt Command Center, Exceptions Queue, Verification
   Workbench, Loan Detail, Audit Trail (screens specced in the thesis doc).
3. **Clerk auth** with mandatory MFA, org-per-sponsor tenancy.
4. **Async extraction jobs** — `/loans/extract` calls Claude synchronously
   today; move it behind a Celery/RQ worker so large PDFs don't block the
   request, and add a PDF/DOCX-to-text step ahead of the pipeline (it
   currently takes plain `document_text`).
5. **S3 document storage** (SSE-KMS, per-tenant prefixes) + presigned uploads.
6. **`/financials/parse-attachment`** — AppFolio/Yardi/Excel format parsers
   feeding the canonical chart of accounts (W1 depends on this).
7. Deploy: FastAPI on Fargate/Railway, n8n self-hosted in the same VPC,
   Sentry + Grafana. Start the SOC2 Type I process.

## Design invariants (do not break these)

- Every number shown to a user persists its **full input vector** — see
  `CovenantTestResult.input_vector`.
- Extracted covenant terms are never active without **human verification**.
- The audit log is **append-only**; no update/delete endpoints exist.
- Missing financials are an **alert, not an absence** — see
  `financials_stale` in `/monitoring/upcoming`.
- Critical alerts **re-escalate until acknowledged** (W5 wait-loop).
