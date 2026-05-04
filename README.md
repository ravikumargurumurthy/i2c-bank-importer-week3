# I2C Bank Importer Foundation — Week 3

The data layer between the I2C cash application agent system and the data
engineering pipeline that loads bank statement files into the database.

## What this is

A real-shaped Postgres-backed integration via an HTTPS SQL gateway API:

- Pydantic schemas modeled exactly on production columns
- Read-only data access layer with typed function returns
- Defense-in-depth write blocking (regex + planned DB role enforcement)
- Smoke-tested against ~7,957 real bank rows and ~143,122 invoices

## Status

Week 3 of 12. Builds on:

- [Week 1](https://github.com/<your-username>/i2c-agent-week1) — hand-rolled
  remittance extractor (10/10 evals)
- [Week 2](https://github.com/<your-username>/i2c-agent-week2) — LangGraph
  rewrite with confidence-based routing and structured tracing (13/13 evals)

This week's deliverable is the production-shaped data foundation that
Project 1 (Week 4-5) and the matching agent (Project 2, Week 7-9) build on.

## Why no LLM this week

Bank file parsing is deterministic plumbing — structured files with
documented specs (CSV, MT940, BAI2). The data engineering team handles
parsing in their pipeline. The agent system reads the resulting database
tables. This is the architecture every production cash app vendor uses.

The agent's value-add is matching ambiguous payments to invoices — that's
where LLM reasoning earns its place. Parsing doesn't.

## Quick start

```bash
git clone https://github.com/<your-username>/i2c-bank-importer-week3.git
cd i2c-bank-importer-week3

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env  # edit if endpoint differs
python smoke_test.py  # verifies the full stack against real data
```

## What's in here

- `sql_client.py` — Thin client for the SQL gateway API; envelope handling,
  read-only enforcement, retry on transient failures
- `schemas.py` — Pydantic models mirroring `t_raw_bank_statements` and
  `t_invoice_header`; handles real data quirks (float-to-Decimal,
  empty-string-to-None, mixed type encodings)
- `db.py` — Read-only data access layer; typed function returns
- `smoke_test.py` — End-to-end verification against real data
- `INTEGRATION.md` — Design doc covering the agent boundary

## Architecture decisions

**Defense-in-depth read-only enforcement.** SQL client rejects queries
containing write keywords via regex. Production deployment also uses a
read-only database role. Both layers should agree; either alone is
sufficient.

**Empty-string-to-None at the schema boundary.** Source data uses empty
strings for missing values rather than NULL. Pydantic validators normalize
at the boundary so downstream code can use truthiness checks consistently.

**Decimal for all amounts.** API returns amounts as floats (bank table) or
strings (invoice table). Both are coerced to Decimal at the schema boundary.
Money math should never touch float.

**All invoices treated as candidates.** For this prototype, the open AR
query returns all invoices. The status semantics in the dev database
contradict each other in places (rows with both `status='open'` and
`clearing_document_number` populated). Refining this is Week 4+ work.

## What's next

Week 4-5 (Project 1 polish) — refit the Week 1+2 remittance extraction
agent to read from this real-shaped data layer instead of JSON files.
Add Streamlit HITL UI, multi-format input (PDF, Excel, email body),
and proper Postgres-backed audit log.
