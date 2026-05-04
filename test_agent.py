# test_agent.py
"""
Pytest harness for the bank payment extraction agent.

Each case in EVAL_SET drives one end-to-end run. Confidence is checked
against bands (LLM non-determinism); routing decision is exact match;
customer name uses substring match (handles minor name variations).

Supports the SME-informed design where bank-extraction defers customer
identification to downstream matching when no VIN is present.
"""

import pytest
from db import get_bank_payment_by_id
from agent import extract_bank_payment
from eval_data import EVAL_SET


@pytest.mark.parametrize("case", EVAL_SET, ids=[c["id"] for c in EVAL_SET])
def test_extraction(case):
    """Run one eval case end-to-end against the live SQL gateway."""
    bank_payment = get_bank_payment_by_id(case["bank_txn_id"])
    assert bank_payment is not None, (
        f"Bank payment {case['bank_txn_id']} not found in DB. "
        f"Maybe it was deleted from dev — pick a different example."
    )

    result = extract_bank_payment(bank_payment)
    extraction = result["extraction"]
    expected = case["expected"]

    # ---- Match method (exact OR set membership) ----
    if "match_method" in expected:
        assert extraction.match_method == expected["match_method"], (
            f"match_method mismatch: got {extraction.match_method}, "
            f"expected {expected['match_method']}"
        )
    elif "match_method_in" in expected:
        assert extraction.match_method in expected["match_method_in"], (
            f"match_method {extraction.match_method!r} not in allowed set "
            f"{expected['match_method_in']}"
        )

    # ---- Confidence band ----
    assert (
        expected["min_confidence"]
        <= extraction.confidence
        <= expected["max_confidence"]
    ), (
        f"confidence {extraction.confidence} outside band "
        f"[{expected['min_confidence']}, {expected['max_confidence']}]"
    )

    # ---- Routing decision (exact OR set membership) ----
    actual_routing = result["routing_decision"]
    if "routing_decision" in expected:
        assert actual_routing == expected["routing_decision"], (
            f"routing_decision mismatch: got {actual_routing}, "
            f"expected {expected['routing_decision']}"
        )
    elif "routing_decision_in" in expected:
        assert actual_routing in expected["routing_decision_in"], (
            f"routing_decision {actual_routing!r} not in allowed set "
            f"{expected['routing_decision_in']}"
        )

    # ---- Customer name (substring match — only for VIN-matched cases) ----
    if "payer_customer_name_contains" in expected:
        actual_name = (extraction.payer_customer_name or "").upper()
        expected_substring = expected["payer_customer_name_contains"].upper()
        assert expected_substring in actual_name, (
            f"customer name mismatch: got '{extraction.payer_customer_name}', "
            f"expected to contain '{expected['payer_customer_name_contains']}'"
        )

    # ---- Customer number is null (for deferred cases) ----
    if expected.get("payer_customer_number_is_null"):
        assert extraction.payer_customer_number is None, (
            f"Expected payer_customer_number to be None (deferred to remittance), "
            f"but got '{extraction.payer_customer_number}'"
        )

    # ---- Bank UTR was extracted (signals captured even when customer deferred) ----
    if expected.get("bank_utr_is_set"):
        assert extraction.bank_utr, (
            f"Expected bank_utr to be extracted from narrative, "
            f"but got '{extraction.bank_utr}'"
        )

    # ---- Payment mode was extracted ----
    if expected.get("payment_mode_is_set"):
        assert extraction.payment_mode, (
            f"Expected payment_mode to be extracted from narrative, "
            f"but got '{extraction.payment_mode}'"
        )