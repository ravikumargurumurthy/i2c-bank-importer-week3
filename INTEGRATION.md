# I2C Bank Importer — Integration Design

## Purpose

This module is the data layer between the I2C cash application agent system
and the data engineering pipeline that owns bank file parsing.

The agent system reads from two tables in the `cashapp` schema:

- `t_raw_bank_statements` — incoming bank payment lines, post-parsing
- `t_invoice_header` — accounts receivable

The agent system does NOT parse bank files (CSV, MT940, BAI2, proprietary
bank exports). That responsibility belongs to the data engineering pipeline,
which already handles file ingestion, format normalization, and database
loading.

## Why this split

Bank file parsing is **deterministic plumbing**: structured files with
documented specifications. A Python parser handles 100% of well-formed files
in milliseconds at near-zero cost per file.

LLM-based parsing of structured files is slower, more expensive, and less
reliable. The principle: **the LLM should never do what code can do better.**

The agent's value-add is matching ambiguous payments to invoices — that's
where ambiguity lives, that's where LLM reasoning earns its place. Parsing
does not.

This split mirrors every production cash app vendor (HighRadius, BlackLine,
Versapay): n bank-format adapters produce a normalized canonical structure,
then the matching engine operates on that canonical form.

## Connection model

The agent system reads from the database via an HTTPS SQL gateway:

- Endpoint: `https://i2c-api-dev.fractal.ai/v1/db/sql`
- Method: POST
- Request: `{"query": "SELECT ..."}`
- Response: `{"status": bool, "data": [...], "message": str, "error": null|str}`

This pattern is common in enterprise environments where direct database
access is restricted. Benefits:

- Network-level access controls (only authorized hosts can reach the gateway)
- Audit logging at the gateway layer
- Rate limiting and query monitoring
- Database can be migrated/scaled without changing application code

The agent system never opens a database connection directly.

## Read-only enforcement

The data layer enforces read-only access through two layers:

1. **Application layer (this module):** all queries pass through
   `sql_client.query_sql()`, which rejects any SQL containing write
   keywords (INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE, GRANT,
   REVOKE, REPLACE, MERGE, UPSERT, COPY, VACUUM, REINDEX). False positives
   are acceptable; we'd rather refuse a legitimate query than slip through
   a destructive one.

2. **Database role (recommended for production):** the credentials used by
   the SQL gateway should map to a Postgres role with SELECT-only privileges
   on the `cashapp` schema. This is a layer beyond the application's control
   but should agree with application-level enforcement.

Both layers should agree; either alone would be sufficient; together they're
robust.

## Data model

### `BankPaymentLine` (Pydantic schema mirroring `t_raw_bank_statements`)

| Field | Type | Notes |
|---|---|---|
| `bank_txn_id` | str | Primary key from DE pipeline |
| `amount` | Decimal | Coerced from float at boundary; safe for arithmetic |
| `narrative` | Optional[str] | Free-text bank memo; primary parsing target |
| `payment_mode` | Optional[str] | NEFT, RTGS, IMPS, CHEQUE for Indian banking |
| `currency` | Optional[str] | ISO 4217 (currently all INR in dev) |
| (+ ~15 more) | various | See `schemas.py` for complete definition |

Empty strings in source data are normalized to None at the schema boundary
so downstream code can use `if payment.narrative:` truthiness checks.

### `OpenInvoice` (Pydantic schema mirroring `t_invoice_header`)

| Field | Type | Notes |
|---|---|---|
| `id` | int | Synthetic primary key |
| `customer_number` | Optional[str] | Customer master key |
| `customer_name` | Optional[str] | Display name; may differ from bank narrative |
| `invoice_number` | Optional[str] | Primary match key |
| `document_number` | Optional[str] | Alternative match key (often appears in remittance) |
| `po_number` | Optional[str] | Customer's purchase order |
| `invoice_reference` | Optional[str] | Customer-provided ref |
| `invoice_amount` | Decimal | Coerced from string at boundary |
| (+ ~15 more) | various | See `schemas.py` for complete definition |

Multiple matching keys exist deliberately. The matching agent (Project 2)
uses each as a confidence signal in a multi-signal scoring framework.

## Query patterns

The data access layer (`db.py`) exposes a small set of typed functions:

- `get_bank_payment_by_id(bank_txn_id)` — single bank payment lookup
- `get_recent_bank_payments(limit)` — exploration / batch processing entry
- `get_bank_payments_by_status(status, limit)` — filter by lifecycle state
- `get_invoices_for_customer(customer_number, limit)` — candidate invoices
- `get_invoice_by_number(customer_number, invoice_number)` — exact match
- `search_customers_by_name(name_fragment, limit)` — fuzzy customer discovery

Each function returns Pydantic objects, never raw dicts. Agent code never
sees SQL.

### Why no bind parameters?

The SQL gateway API takes a SQL string, not parameterized queries. The data
access layer substitutes values into SQL strings with two protection layers:

1. **Identifier validation** — customer numbers, invoice numbers, etc. are
   restricted to alphanumeric + underscore + hyphen via regex
2. **String literal escaping** — free-text fragments (e.g., name search)
   have single quotes doubled per SQL standard

Production deployment should add proper bind-parameter support if/when the
gateway API supports it.

## Project scope

For this prototype:

- **All invoices treated as candidates for matching** — no filter on
  `status` or `clearing_document_number`. Real production deployment would
  filter by appropriate open/closed semantics established with the data
  engineering team.
- **Single currency (INR)** — multi-currency handling is future-proofing.
- **Empty-string-to-None normalization** — applied at schema boundary; not
  pushed back to source database.

These simplifications are deliberate. They isolate the agent's logic from
data-modeling concerns that can be refined later without changing agent code.

## Failure modes

| Failure | Behavior |
|---|---|
| API endpoint unreachable | `requests.RequestException` → wrapped in `SQLClientError` after retries |
| API returns 5xx | Retried with exponential backoff (1s, 2s, 4s) |
| API returns 4xx | Surfaces as `SQLClientError` immediately (no retry) |
| API envelope `status: false` or `error` set | `SQLClientError` with the API's error message |
| Row fails Pydantic validation | `ValidationError` propagates; clearly indicates bad data shape |
| Query contains write keywords | `WriteSQLBlockedError` raised before the network call |

All failure modes raise typed exceptions. Agent code can decide how to
respond (retry the whole agent invocation, route to exception queue, etc.).

## What this contract does NOT cover

- Bank file parsing logic (DE team's domain)
- The agent's confidence scoring (covered in agent's confidence_rubric.md)
- Routing thresholds (auto-apply / HITL / exception bands)
- Audit log writes (separate write-path module, not yet built)
- Authentication (network-level access control today; production may add OAuth)

These are downstream concerns the agent owns. This document covers only the
input boundary.
