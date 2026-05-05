# I2C Bank Payment Extraction Agent — Week 3

Bank-side extraction agent for an Indian shipping/logistics cash application
system. Built on top of a SQL gateway data layer connecting to ~7,957 real
bank rows + ~143,122 invoices + ~3,907 customers.

## Status

Week 3 of 12. Day 1 deliverable. 4/4 evals passing against real data.

## Design — SME-informed deferred matching

The agent does NOT attempt customer identification at this stage when no VIN
is present. Per cash app SME workflow:

1. **VIN-first:** If the bank narrative contains a virtual account number
   (ZLAD pattern), look up customer deterministically in customer master.
2. **No VIN → defer:** If no VIN, emit structured signals (UTR, payment mode,
   amount, narrative) but leave customer null. The downstream matching agent
   uses the remittance document to identify the customer via UTR / customer
   number / invoice numbers.
3. **VIN unmatched (~5% of bank VINs):** Real-world data quality issue.
   Routes to HITL review.
4. **Unparseable narrative:** True exception.

This deferred-matching design reflects the actual operational workflow used
by cash app analysts. Single-stage customer identification at parse-time is
a synthetic-data simplification that misses real production complexity.

## Eval results

\`\`\`
test_extraction[ev_B001_van_clean_match_hapag] PASSED
test_extraction[ev_B002_van_clean_match_hapag_alt] PASSED
test_extraction[ev_B003_no_van_awaiting_remittance] PASSED
test_extraction[ev_B004_no_van_awaiting_remittance_alt] PASSED
\`\`\`

4/4 against real customer master, real bank narratives, real customer numbers.

## Architecture

LangGraph state machine with method-driven routing:

\`\`\`
START → call_llm ↔ execute_tools
              ↓
       validate_output
              ↓
       route_by_confidence
              ↓
       ┌──────┼──────┬──────────┐
       ↓      ↓      ↓          ↓
  auto_apply  awaiting  hitl   exception
              remittance review
       ↓      ↓      ↓          ↓
              END
\`\`\`

Routing is method-driven, not pure confidence-driven:
- `vin_exact` + confidence ≥ 0.95 → auto_apply
- `awaiting_remittance` → awaiting_remittance (NORMAL flow, not failure)
- `vin_unmatched` → hitl_review
- `unparseable` → exception

## Quick start

\`\`\`bash
git clone https://github.com/<your-username>/i2c-bank-importer-week3.git
cd i2c-bank-importer-week3

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env  # add Azure credentials and SQL gateway endpoint

python smoke_test.py    # verifies data layer
python agent.py         # demo with VIN and no-VIN cases
pytest test_agent.py -v # 4/4 evals
\`\`\`

## What's in here

- `sql_client.py` — SQL gateway HTTP client with read-only enforcement
- `schemas.py` — Pydantic models for BankPaymentLine, OpenInvoice, Customer
- `db.py` — typed data access functions
- `tools.py` — three deterministic tools (narrative parser, VIN lookup, invoice lookup)
- `agent.py` — LangGraph state machine with four-band routing
- `eval_data.py` — 4 hand-labeled cases covering VIN and no-VIN paths
- `test_agent.py` — pytest harness running evals against live SQL gateway
- `INTEGRATION.md` — design doc for the data layer boundary
- `FINDINGS.md` — engineering decisions and SME consultation log

## What's next

Week 3 Day 2 (~1.5 hours):
- Find a real "exception" eval case (unparseable narrative)
- Multi-run eval methodology (each case 5x for stability check)
- Wire structured tracing (carry forward from Week 2)

Then Project 1 (Weeks 4-5) refits the Week 1+2 remittance extraction agent
against this real data layer, adds Streamlit HITL UI, multi-format input.

Project 2 (Weeks 7-9) is the matching agent — implements the SME's cascade
logic: VIN → remittance → invoice-to-customer lookup. This is where the hard
problem lives.