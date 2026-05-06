## Day 1 — Pivoting to SME-informed design

### Initial design (rejected)
First attempt: at the bank-extraction stage, fuzzy-match payer name against
customer master. If multiple candidates, emit them all with match_method=
'name_fuzzy_ambiguous' for downstream disambiguation.

### Why it was wrong
Consulted the cash app SME. The actual operational workflow is:
1. Bank line → look for VIN. If VIN exists, customer is known deterministically.
2. If no VIN, do NOT try to identify customer from bank line. Wait for remittance.
3. Remittance has UTR/customer_number/invoice_numbers — any of these is a
   stronger signal than fuzzy name matching against 3,907 dirty customer records.
4. If remittance has invoice numbers, lookup the invoice in t_invoice_header to
   get customer_number directly. Invoice numbers are unique and exact.

### Revised design
Bank extraction agent's job is reduced:
- Parse narrative signals (payment_mode, UTR, VIN, amount)
- If VIN matches: customer known, ready for matching
- If no VIN: emit signals; customer identification deferred to downstream
  matching agent (which will use the remittance, not name fuzzy-matching)

### Lesson
The agent should match the operational workflow, not invent a workflow.
Production cash app teams have refined cascade-matching strategies over years
of exception triage; trying to compress that into a single agent stage is
both wrong (loses information) and worse (lower accuracy than the SME's
multi-stage approach).

Always validate agent design against domain expert workflow.



## Day 1 — The "wrong stage" lesson

I designed three increasingly complex schemes for handling no-VIN bank
payments:
1. Single-customer fuzzy match (failed — picked wrong segment as payer name)
2. Better fuzzy match with stopwords + WRatio (worked but exposed master
   duplicates)
3. Multi-candidate cascade with deferred disambiguation (overengineered)

Each iteration tried to make fuzzy customer matching work AT THE BANK
EXTRACTION STAGE.

Then the SME said: "don't try at this stage."

The actual operational workflow uses three documents (bank + remittance +
ledger) where each provides different signals at different stages:
- Bank: VIN if present, otherwise just structured signals (UTR, amount, mode)
- Remittance: customer number, invoice numbers, UTR
- Ledger: invoice → customer mapping (deterministic via invoice_number lookup)

The agent should NOT compress this multi-stage workflow into one stage.
Each stage has its own information; trying to do too much at one stage
loses information from other stages.

### Generalizable lesson
When an agent is producing wrong/uncertain answers, the question to ask is
not "how do I make this agent smarter?" but "is this agent at the wrong
stage of the pipeline?" Sometimes the right design is for the agent to do
LESS, and pass richer context to the next stage.

### How this discipline applies elsewhere
- Don't have an extractor agent also try to validate against business rules.
  Validate at a separate stage with full context.
- Don't have a matching agent also try to make application decisions.
  Match in one stage, decide in another with full audit context.
- Don't have a chat agent also try to fetch real-time data. Use


## Day 1 — Real-world data quality findings from the bank table

While searching for an "unparseable narrative" eval case, found:

1. ~5 rows with `narrative = "Rev.of DD Chrgs"` and varying amounts.
   Translation: "Reversal of Demand Draft Charges" — bank fee refunds, not
   customer payments. Should be filtered upstream by the DE pipeline. The
   agent's correct disposition: route to exception.

2. ~5 rows with both `narrative = ""` AND `amount = NULL`. These are
   structurally invalid payments — no transaction info at all. Cannot be
   processed by any cash app system. Likely DE pipeline garbage rows that
   loaded by mistake.

These findings represent operational reality. In a production deployment,
the agent contract would specify upstream filters. In our learning project,
we handle them defensively (fee reversals → exception; null-amount rows
crash at schema validation, which is appropriate — they shouldn't be
processed).

### Lesson
Real banking tables contain non-payment rows: fee reversals, charge
adjustments, currency conversions, opening balances, system entries. A
production cash app system needs an explicit filter for "is this a customer
payment?" before invoking the matching pipeline. This is a Project 1
boundary concern, not Project 2 matching logic.


## Day 1 — The "well-formed but signal-less" failure mode

Initial system prompt had two narrative dispositions:
- Parsed cleanly → awaiting_remittance
- Could not parse → unparseable (exception)

Real bank data revealed a third category:
- Parsed without errors, but contains NO payment signals.

Examples: "Rev.of DD Chrgs" (bank fee reversal), "MONTHLY MAINTENANCE FEE",
"FX SETTLEMENT". These are well-formed English but describe bank-internal
entries, not customer payments.

The agent initially routed these to awaiting_remittance because the parser
didn't crash. That's wrong — a remittance will never arrive for a fee
reversal because there is no customer.

### Fix
Expanded CASE 4 in system prompt: use unparseable when narrative has no
payment signals (no payment_mode, no UTR, no VIN, no slash structure)
even when it's well-formed text. Listed common bank-internal patterns
(REVERSAL, ADJUSTMENT, FEE, CHARGE).

### Lesson
"The parser didn't crash" is not the same as "this is a real payment."
Validation needs both syntactic parsing AND semantic content checks.

In production, this distinction would also live upstream: the DE pipeline
should filter bank-internal entries before they reach the agent. Defense
in depth — the agent handles them gracefully even if the filter misses
some.

# Week 3 Findings

## Day 1 — Heuristic tuning for payer name extraction

Initial fuzzy customer matcher returned wrong candidates. Direct testing of
the narrative parser (`_guess_payer_name`) showed it picked "JP MORGAN BANK"
instead of "COSCO SHIPPING LINES". The heuristic scored both segments equally
because both are multi-word all-caps; stable-sort ordering picked the wrong
one.

Fix: added two rules — skip segments containing BANK/BNK/BK as whole words,
and apply a small earlier-position bonus when no VIN is present (NEFT
narrative convention puts payer before bank).

Lesson: heuristics that look reasonable in isolation fail on real data because
they lack domain context. The fix wasn't a smarter algorithm; it was encoding
two pieces of domain knowledge.


## Day 1 — Fuzzy matching at scale

After fixing the heuristic, the fuzzy match still failed. Diagnosis showed
the SQL pre-filter anchored on the longest query word ("SHIPPING"), a generic
suffix shared by hundreds of customers. The matcher returned candidates like
ACT SHIPPING, APG SHIPPING — all scoring ~80 — but COSCO wasn't in the
candidate set at all.

Root cause: `token_set_ratio` is too forgiving for this scale. Sharing one
common token (SHIPPING) scores 80 against any query containing SHIPPING,
whether COSCO is involved or not.

Fix: filter generic stopwords (INDIA, PVT, LTD, AND, OF) before selecting
search anchors; search across all distinctive words via OR; switch from
token_set_ratio to WRatio for stronger discrimination.

Lesson: thresholds and algorithms tuned for synthetic data rarely survive
contact with production-scale data. At 3,907 customers, single-token anchoring
on generic terms fails. Always validate against real volume.


## Day 1 — The "wrong stage" lesson (most important)

Pursued three increasingly complex schemes for handling no-VIN bank payments:
single-customer fuzzy match, ambiguity-tolerant scoring with WRatio, and
multi-candidate cascade with deferred disambiguation. Each tried to make
fuzzy customer matching work AT THE BANK EXTRACTION STAGE.

SME consultation revealed all three were wrong. The actual operational
workflow uses three documents (bank + remittance + ledger):
- Bank: VIN if present; otherwise just structured signals (UTR, amount, mode)
- Remittance: customer number, invoice numbers, UTR
- Ledger: invoice → customer (deterministic via invoice_number lookup)

Each document provides different signals at different stages. Trying to
compress this into one stage loses information from the others.

Lesson: when an agent is producing wrong/uncertain answers, the question
isn't "how do I make this agent smarter?" but "is this agent at the wrong
stage of the pipeline?" Sometimes the right design is for the agent to do
LESS and pass richer context forward via structured outputs.

Generalizable principle: agent stages should be aggressively single-purpose,
with information cascading forward.


## Day 1 — The "well-formed but signal-less" failure mode

Initial system prompt had two narrative dispositions: parsed cleanly →
awaiting_remittance, or could not parse → unparseable. Real bank data
revealed a third category — well-formed text containing no payment signals.

Examples: "Rev.of DD Chrgs" (Demand Draft fee reversal), "MONTHLY MAINTENANCE
FEE", "FX SETTLEMENT". These are coherent English text describing
bank-internal entries, not customer payments. The agent initially routed
them to awaiting_remittance because the parser didn't crash. Wrong outcome —
no remittance will ever arrive for a fee reversal.

Fix: expanded the system prompt to use match_method='unparseable' when
narrative has no payment signals (no payment_mode, no UTR, no VIN, no slash
structure) even when well-formed.

Lesson: "the parser didn't crash" is not the same as "this is a real payment."
Validation needs both syntactic parsing AND semantic content checks.


## Day 1 — Customer master data quality

Customer master (3,907 records) has known data quality issues:
- Multiple records for same logical customer (4 entries for COSCO SHIPPING
  LINES with name variations: parentheses, PVT LTD suffix, base name)
- Same customer name across 3 customer_numbers (KNK OVERSEAS appears as
  5000002857, 6000004941, 6000004951)
- Customer_number is a string identifier with varying length: from 3 digits
  ("761") to 10 digits ("6000005270")
- VIN populated for ~12% of customers (461 of 3,907) but ~48% of bank
  narratives reference VINs

These observations validate the SME-informed deferred matching design.
Production cash app systems handle dirty customer masters via the matching
agent's amount-and-AR consistency checks, not by assuming clean masters at
extraction time.
