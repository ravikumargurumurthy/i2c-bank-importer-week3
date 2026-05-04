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
