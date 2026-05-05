# agent.py
"""
Bank payment extraction agent (SME-informed design).

Takes a BankPaymentLine and produces a structured PaymentExtraction:
- Identified customer ONLY when a strong identifier (VIN) is present
- Otherwise, structured signals (UTR, payment_mode, amount) for downstream
  matching agent to use with the remittance document

Key design principle (per cash app SME):
The bank-extraction agent does NOT attempt customer identification from payer
name. Real cash app workflow:
  1. Bank line: extract VIN if present
  2. If no VIN, defer — wait for remittance document
  3. Remittance has UTR / customer_number / invoice_numbers — those are the
     authoritative customer-identification signals
  4. If remittance gives invoice numbers, look up invoice in t_invoice_header
     to get customer_number directly

Routing bands:
  - auto_apply: VIN matched, customer known with high confidence
  - awaiting_remittance: No VIN; signals captured for downstream matching
    (this is the NORMAL case for ~52% of payments, not an exception)
  - hitl_review: VIN extracted but didn't match master (~5% real-world rate)
  - exception: Narrative completely unparseable
"""

import json
import os
from decimal import Decimal
from enum import Enum
from operator import add
from typing import Annotated, Optional

from dotenv import load_dotenv
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_openai import AzureChatOpenAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from schemas import BankPaymentLine
from tools import (
    extract_payment_signals_from_narrative,
    lookup_customer_by_vin_tool,
    lookup_invoices_for_customer_tool,
)

load_dotenv()


# ============================================================
# Output schema
# ============================================================

class PaymentExtraction(BaseModel):
    """The agent's structured output for a bank payment.

    Only populates payer_customer_number / payer_customer_name when a
    strong identifier (VIN) was extracted and matched. Otherwise the
    customer is left null and identification is deferred to downstream
    matching using the remittance document.
    """

    bank_txn_id: str
    amount: Decimal
    currency: Optional[str] = None

    # Structured signals extracted from narrative
    payment_mode: Optional[str] = None
    bank_utr: Optional[str] = None

    # Customer identification — populated only for VIN-matched cases
    payer_customer_number: Optional[str] = None
    payer_customer_name: Optional[str] = None

    match_method: Optional[str] = Field(
        None,
        description=(
            "One of: 'vin_exact', 'vin_unmatched', 'awaiting_remittance', "
            "'unparseable'"
        ),
    )

    confidence: float = Field(..., ge=0.0, le=1.0)
    extraction_notes: Optional[str] = None


class RoutingDecision(str, Enum):
    AUTO_APPLY = "auto_apply"
    AWAITING_REMITTANCE = "awaiting_remittance"
    HITL_REVIEW = "hitl_review"
    EXCEPTION = "exception"


# ============================================================
# Agent state
# ============================================================

class AgentState(BaseModel):
    bank_payment: BankPaymentLine
    messages: Annotated[list[BaseMessage], add] = Field(default_factory=list)
    extraction: Optional[PaymentExtraction] = None
    validation_error: Optional[str] = None
    validation_retries: int = 0
    routing_decision: Optional[RoutingDecision] = None
    action_result: Optional[dict] = None


# ============================================================
# LLM client + tool registry
# ============================================================

TOOL_REGISTRY = {
    "extract_payment_signals_from_narrative": extract_payment_signals_from_narrative,
    "lookup_customer_by_vin": lookup_customer_by_vin_tool,
    "lookup_invoices_for_customer": lookup_invoices_for_customer_tool,
}

TOOLS_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "extract_payment_signals_from_narrative",
            "description": (
                "Parse a bank narrative into structured fields: payment_mode, "
                "bank_utr, vin, payer_name_candidate. Always call this FIRST "
                "to ground extraction. Returns deterministic results."
            ),
            "parameters": {
                "type": "object",
                "properties": {"narrative": {"type": "string"}},
                "required": ["narrative"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_customer_by_vin",
            "description": (
                "Resolve a VIN (virtual account number, format ZLAD + 14 "
                "alphanumeric) to a customer. STRONG identifier — when matched, "
                "this IS the payer with very high confidence. ~5%% of bank VINs "
                "don't match the master; in those cases, set match_method to "
                "'vin_unmatched' and route to HITL review."
            ),
            "parameters": {
                "type": "object",
                "properties": {"vin": {"type": "string"}},
                "required": ["vin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_invoices_for_customer",
            "description": (
                "Get open invoices for a customer. Use AFTER successfully "
                "identifying the customer via VIN to verify they have outstanding "
                "AR consistent with the payment amount. Optional — useful for "
                "raising confidence further when AR matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_number": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["customer_number"],
            },
        },
    },
]

_llm_base = AzureChatOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
    azure_deployment=os.getenv("AZURE_OPENAI_DEPLOYMENT"),
)
llm_with_tools = _llm_base.bind_tools(TOOLS_SCHEMA)


# ============================================================
# System prompt
# ============================================================

SYSTEM_PROMPT = """You are a bank payment extraction assistant for an Indian \
shipping/logistics company's cash application system.

CRITICAL DESIGN PRINCIPLE — read carefully:

You do NOT attempt customer identification from payer name. The bank narrative \
contains a payer name segment (e.g., "COSCO SHIPPING LINES (INDIA)"), but the \
customer master has ~3,907 records with frequent duplicates and name variations. \
Fuzzy-matching at this stage produces wrong answers.

Customer identification is the job of the downstream matching agent, which uses \
the REMITTANCE DOCUMENT (a separate document arriving via email/PDF/Excel) to \
identify customers via:
- Customer number stated explicitly in remittance
- Invoice numbers (which lookup to a single customer in t_invoice_header)
- UTR matching between bank line and remittance

Your job at this stage is to extract STRUCTURED SIGNALS for the matching agent.

Process:
1. Call `extract_payment_signals_from_narrative` first to parse the narrative.
2. If a VIN was extracted (ZLAD pattern), call `lookup_customer_by_vin`. VIN is \
the ONLY way you identify a customer at this stage.
3. (Optional) If VIN matched, call `lookup_invoices_for_customer` to verify \
the customer has open AR — this raises confidence.
4. Produce a final PaymentExtraction JSON.

Output schema (PaymentExtraction):
{
  "bank_txn_id": str (echo from input),
  "amount": str (decimal as string),
  "currency": str | null,
  "payment_mode": str | null (NEFT/RTGS/IMPS/etc.),
  "bank_utr": str | null,
  "payer_customer_number": str | null,
  "payer_customer_name": str | null,
  "match_method": "vin_exact" | "vin_unmatched" | "awaiting_remittance" | "unparseable",
  "confidence": float between 0.0 and 1.0,
  "extraction_notes": str | null
}

Decision rules — EXACTLY follow these:

CASE 1: VIN extracted AND lookup_customer_by_vin returned matched=true
- match_method: "vin_exact"
- payer_customer_number: from lookup result
- payer_customer_name: from lookup result
- confidence: 0.95-0.99
- extraction_notes: brief — "VIN matched customer master deterministically"

CASE 2: VIN extracted BUT lookup_customer_by_vin returned matched=false
- match_method: "vin_unmatched"
- payer_customer_number: null
- payer_customer_name: null
- confidence: 0.50-0.65
- extraction_notes: include the VIN that didn't match; note that this is a \
known data quality scenario (~5% of bank VINs don't match master)

CASE 3: No VIN in narrative AND narrative contains payment signals
- Required signals: at least ONE of {payment_mode (NEFT/RTGS/IMPS/UPI), bank_utr, slash-delimited structure suggesting a real payment}
- match_method: "awaiting_remittance"
- payer_customer_number: null
- payer_customer_name: null
- confidence: 0.30-0.49
- extraction_notes: include the parsed signals; note that this is the NORMAL flow

CASE 4: Narrative is unparseable OR contains no payment signals
Use this when ANY of:
- Narrative is empty/missing
- Narrative is malformed (random characters, no structure)
- Narrative is well-formed text but contains NO payment signals — no payment_mode (NEFT/RTGS/IMPS/UPI), no UTR pattern, no VIN, no recognizable structure suggesting a customer payment
- Narrative describes a bank-internal entry rather than a customer payment. Examples:
    * "Rev.of DD Chrgs" (bank fee reversal)
    * "MONTHLY MAINTENANCE FEE" (bank fee)
    * "FX SETTLEMENT" (currency conversion)
    * "OPENING BALANCE" (statement opener)
    * Anything with "REVERSAL", "ADJUSTMENT", "FEE", "CHARGE" without a payer reference

For CASE 4:
- match_method: "unparseable"
- payer_customer_number: null
- payer_customer_name: null
- confidence: 0.0-0.29
- extraction_notes: explain why narrative was treated as unparseable

ABSOLUTE RULES:
- DO NOT set match_method to "vin_exact" unless lookup_customer_by_vin returned matched=true.
- DO NOT attempt to fuzzy-match customer names. The fuzzy-matching tool has been \
deliberately removed from your toolset.
- ALWAYS extract payment_mode, bank_utr, amount, currency — even when match_method \
is "awaiting_remittance". The downstream matching agent needs these signals.
- "awaiting_remittance" is NOT a failure. It's the expected outcome for payments \
without VINs. Do not raise concerns or unnecessary caveats in extraction_notes for this case.
"""


# ============================================================
# Nodes
# ============================================================

def call_llm_node(state: AgentState) -> dict:
    new_context: list[BaseMessage] = []

    if not state.messages:
        # First call: seed with system + user message
        bank = state.bank_payment
        user_input = (
            f"Extract payment signals from this bank line:\n\n"
            f"bank_txn_id: {bank.bank_txn_id}\n"
            f"amount: {bank.amount} {bank.currency or 'INR'}\n"
            f"narrative: {bank.narrative or '(empty)'}\n"
            f"bank_name: {bank.bank_name or '(unknown)'}\n"
            f"value_date: {bank.value_date or '(unknown)'}\n"
        )
        new_context.extend([
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_input),
        ])

    if state.validation_error:
        new_context.append(HumanMessage(
            content=(
                f"Your previous output failed validation:\n{state.validation_error}\n\n"
                f"Fix the issues and return corrected JSON matching the "
                f"PaymentExtraction schema."
            )
        ))

    full_messages = list(state.messages) + new_context
    response = llm_with_tools.invoke(full_messages)

    return {
        "messages": new_context + [response],
        "validation_error": None,
    }


def execute_tools_node(state: AgentState) -> dict:
    last_msg = state.messages[-1]
    if not getattr(last_msg, "tool_calls", None):
        return {}

    tool_messages = []
    for tc in last_msg.tool_calls:
        name = tc["name"]
        args = tc["args"]
        if name not in TOOL_REGISTRY:
            result = json.dumps({"error": f"Unknown tool: {name}"})
        else:
            try:
                fn = TOOL_REGISTRY[name]
                output = fn(**args)
                result = json.dumps(output, default=str)
            except Exception as e:
                result = json.dumps({"error": f"Tool {name} failed: {str(e)}"})

        tool_messages.append(
            ToolMessage(content=result, tool_call_id=tc["id"], name=name)
        )

    return {"messages": tool_messages}


def validate_output_node(state: AgentState) -> dict:
    last_msg = state.messages[-1]
    content = last_msg.content if isinstance(last_msg, AIMessage) else None

    if not content:
        return {"validation_error": "Final assistant message had no content."}

    try:
        extraction = PaymentExtraction.model_validate_json(content)
    except Exception as e:
        return {
            "validation_error": f"Schema validation failed: {e}",
            "validation_retries": state.validation_retries + 1,
        }

    # Sanity: bank_txn_id should match input
    if extraction.bank_txn_id != state.bank_payment.bank_txn_id:
        return {
            "validation_error": (
                f"bank_txn_id mismatch: extraction has '{extraction.bank_txn_id}' "
                f"but input was '{state.bank_payment.bank_txn_id}'"
            ),
            "validation_retries": state.validation_retries + 1,
        }

    # Sanity: vin_exact requires a customer_number
    if extraction.match_method == "vin_exact" and not extraction.payer_customer_number:
        return {
            "validation_error": (
                "match_method is 'vin_exact' but payer_customer_number is null. "
                "If VIN matched, populate payer_customer_number from the lookup result. "
                "If VIN did not match, use match_method='vin_unmatched' instead."
            ),
            "validation_retries": state.validation_retries + 1,
        }

    return {"extraction": extraction}


def route_by_confidence_node(state: AgentState) -> dict:
    """Map match_method + confidence to routing decision."""
    if not state.extraction:
        return {"routing_decision": RoutingDecision.EXCEPTION}

    e = state.extraction

    # Method-driven routing (not pure confidence-based)
    if e.match_method == "vin_exact" and e.confidence >= 0.95:
        return {"routing_decision": RoutingDecision.AUTO_APPLY}

    if e.match_method == "awaiting_remittance":
        return {"routing_decision": RoutingDecision.AWAITING_REMITTANCE}

    if e.match_method == "vin_unmatched":
        # VIN extracted but not in master — needs investigation
        return {"routing_decision": RoutingDecision.HITL_REVIEW}

    # Fall through: unparseable narrative or unexpected method
    return {"routing_decision": RoutingDecision.EXCEPTION}


def auto_apply_node(state: AgentState) -> dict:
    """Mock auto-apply: in production this would write to the GL subledger."""
    e = state.extraction
    return {
        "action_result": {
            "action": "auto_apply",
            "would_post_to_GL": {
                "bank_txn_id": e.bank_txn_id,
                "customer_number": e.payer_customer_number,
                "customer_name": e.payer_customer_name,
                "amount": str(e.amount),
                "currency": e.currency,
                "match_method": e.match_method,
            },
            "audit_note": (
                f"Auto-applied via {e.match_method}; confidence {e.confidence}"
            ),
        }
    }


def awaiting_remittance_node(state: AgentState) -> dict:
    """Defer customer identification to downstream matching agent.

    Used when no VIN was extracted from the bank narrative. The bank-extraction
    agent has done its job: parsed structured signals (UTR, payment_mode,
    amount). The matching agent will pair this with the remittance document
    to identify the customer.

    This is the NORMAL case for ~52% of payments — not an exception.
    """
    e = state.extraction
    return {
        "action_result": {
            "action": "awaiting_remittance",
            "deferred_record": {
                "bank_txn_id": e.bank_txn_id,
                "amount": str(e.amount),
                "currency": e.currency,
                "payment_mode": e.payment_mode,
                "bank_utr": e.bank_utr,
                "narrative": state.bank_payment.narrative,
                "agent_notes": e.extraction_notes,
            },
            "audit_note": (
                f"Customer identification deferred to remittance/matching stage. "
                f"UTR {e.bank_utr or 'none'} captured for bank<->remittance pairing."
            ),
        }
    }


def hitl_review_node(state: AgentState) -> dict:
    """For VIN-extracted but unmatched cases — needs human investigation."""
    e = state.extraction
    return {
        "action_result": {
            "action": "hitl_review",
            "review_queue_entry": {
                "bank_txn_id": e.bank_txn_id,
                "amount": str(e.amount),
                "match_method": e.match_method,
                "confidence": e.confidence,
                "agent_notes": e.extraction_notes,
                "reason": "VIN extracted from narrative but not in customer master",
            },
            "audit_note": f"HITL review; {e.match_method}; confidence {e.confidence}",
        }
    }


def exception_node(state: AgentState) -> dict:
    """Genuine failure — narrative unparseable or unexpected agent state."""
    e = state.extraction
    return {
        "action_result": {
            "action": "exception",
            "exception_record": {
                "bank_txn_id": e.bank_txn_id if e else state.bank_payment.bank_txn_id,
                "amount": str(e.amount) if e else str(state.bank_payment.amount),
                "narrative": state.bank_payment.narrative,
                "agent_notes": e.extraction_notes if e else "Agent did not produce extraction",
                "match_method": e.match_method if e else "unparseable",
                "confidence": e.confidence if e else 0.0,
            },
            "audit_note": "Exception escalation; needs ops investigation",
        }
    }


# ============================================================
# Conditional edge functions
# ============================================================

def after_llm(state: AgentState) -> str:
    """After LLM responds, decide next: tools or validate."""
    last_msg = state.messages[-1]
    if getattr(last_msg, "tool_calls", None):
        return "execute_tools"
    return "validate_output"


def after_validation(state: AgentState) -> str:
    """After validation: success → routing, retry → loop, give up → END."""
    if state.extraction is not None:
        return "route_by_confidence"
    if state.validation_retries >= 2:
        return END
    return "call_llm"


def route_to_terminal(state: AgentState) -> str:
    """Dispatch from route_by_confidence_node to the appropriate terminal node."""
    decision = state.routing_decision
    if decision == RoutingDecision.AUTO_APPLY:
        return "auto_apply"
    elif decision == RoutingDecision.AWAITING_REMITTANCE:
        return "awaiting_remittance"
    elif decision == RoutingDecision.HITL_REVIEW:
        return "hitl_review"
    return "exception"


# ============================================================
# Build graph
# ============================================================

builder = StateGraph(AgentState)

# Existing core nodes
builder.add_node("call_llm", call_llm_node)
builder.add_node("execute_tools", execute_tools_node)
builder.add_node("validate_output", validate_output_node)
builder.add_node("route_by_confidence", route_by_confidence_node)

# Terminal nodes (one per routing band)
builder.add_node("auto_apply", auto_apply_node)
builder.add_node("awaiting_remittance", awaiting_remittance_node)
builder.add_node("hitl_review", hitl_review_node)
builder.add_node("exception", exception_node)

# Edges
builder.add_edge(START, "call_llm")

builder.add_conditional_edges(
    "call_llm",
    after_llm,
    {"execute_tools": "execute_tools", "validate_output": "validate_output"},
)
builder.add_edge("execute_tools", "call_llm")

builder.add_conditional_edges(
    "validate_output",
    after_validation,
    {
        "route_by_confidence": "route_by_confidence",
        "call_llm": "call_llm",
        END: END,
    },
)

builder.add_conditional_edges(
    "route_by_confidence",
    route_to_terminal,
    {
        "auto_apply": "auto_apply",
        "awaiting_remittance": "awaiting_remittance",
        "hitl_review": "hitl_review",
        "exception": "exception",
    },
)

# Each terminal goes to END
builder.add_edge("auto_apply", END)
builder.add_edge("awaiting_remittance", END)
builder.add_edge("hitl_review", END)
builder.add_edge("exception", END)

graph = builder.compile()


# ============================================================
# Public API
# ============================================================

def extract_bank_payment(bank_payment: BankPaymentLine) -> dict:
    """Run the full agent on a bank payment. Returns extraction + routing + action."""
    initial = AgentState(bank_payment=bank_payment)
    final_state = graph.invoke(initial, config={"recursion_limit": 25})

    if not final_state.get("extraction"):
        raise RuntimeError(
            f"Agent failed for {bank_payment.bank_txn_id}. "
            f"Validation error: {final_state.get('validation_error')}"
        )

    return {
        "extraction": final_state["extraction"],
        "routing_decision": final_state.get("routing_decision"),
        "action_result": final_state.get("action_result"),
    }


# ============================================================
# Demo
# ============================================================

if __name__ == "__main__":
    from db import get_bank_payment_by_id

    test_cases = [
        ("VIN match (HAPAG)", "27e31459-a03c-4616-a539-fdc515d6ad9d"),
        ("No VIN (COSCO — should defer)", "bd6c613c-dbee-49b6-9d2c-610ed6c985a1"),
    ]

    for label, txn_id in test_cases:
        print("=" * 70)
        print(f"  {label}")
        print("=" * 70)

        payment = get_bank_payment_by_id(txn_id)
        if not payment:
            print(f"  Payment {txn_id} not found in DB.")
            continue

        print(f"  Narrative: {(payment.narrative or '')[:80]}")
        print(f"  Amount: {payment.amount} {payment.currency}")
        print()

        result = extract_bank_payment(payment)
        e = result["extraction"]
        print(f"  match_method:    {e.match_method}")
        print(f"  routing:         {result['routing_decision']}")
        print(f"  confidence:      {e.confidence}")
        print(f"  customer_number: {e.payer_customer_number or '(deferred)'}")
        print(f"  customer_name:   {e.payer_customer_name or '(deferred)'}")
        print(f"  bank_utr:        {e.bank_utr or '(none)'}")
        print(f"  payment_mode:    {e.payment_mode or '(none)'}")
        print()