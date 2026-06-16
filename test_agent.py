# test_agent.py
"""
Pytest harness with multi-run eval methodology.

Each case can run N times (controlled by EVAL_RUNS env var, default 1).
A case PASSES if at least PASS_THRESHOLD of N runs succeeded.

Usage:
  pytest test_agent.py -v                          # 1 run per case
  EVAL_RUNS=5 PASS_THRESHOLD=4 pytest test_agent.py -v
                                                   # 5 runs, need 4 passes
"""

import os
import pytest
from db import get_bank_payment_by_id
from agent import extract_bank_payment
from eval_data import EVAL_SET


# ---- Configuration ----
EVAL_RUNS = int(os.getenv("EVAL_RUNS", "1"))
PASS_THRESHOLD = int(os.getenv("PASS_THRESHOLD", str(max(1, EVAL_RUNS))))


def _check_one_run(case, extraction, routing_decision):
    """
    Validate one extraction against expected. Returns (passed, failure_reason).
    Pure function — no pytest assertions, just truth value + diagnostic.
    """
    expected = case["expected"]

    # Match method
    if "match_method" in expected:
        if extraction.match_method != expected["match_method"]:
            return False, (
                f"match_method: got {extraction.match_method!r}, "
                f"expected {expected['match_method']!r}"
            )
    elif "match_method_in" in expected:
        if extraction.match_method not in expected["match_method_in"]:
            return False, (
                f"match_method {extraction.match_method!r} "
                f"not in {expected['match_method_in']}"
            )

    # Confidence band
    if not (
        expected["min_confidence"]
        <= extraction.confidence
        <= expected["max_confidence"]
    ):
        return False, (
            f"confidence {extraction.confidence} outside band "
            f"[{expected['min_confidence']}, {expected['max_confidence']}]"
        )

    # Routing decision
    if "routing_decision" in expected:
        if routing_decision != expected["routing_decision"]:
            return False, (
                f"routing: got {routing_decision!r}, "
                f"expected {expected['routing_decision']!r}"
            )
    elif "routing_decision_in" in expected:
        if routing_decision not in expected["routing_decision_in"]:
            return False, (
                f"routing {routing_decision!r} "
                f"not in {expected['routing_decision_in']}"
            )

    # Customer name substring (only checked if expected has it)
    if "payer_customer_name_contains" in expected:
        actual_name = (extraction.payer_customer_name or "").upper()
        expected_substring = expected["payer_customer_name_contains"].upper()
        if expected_substring not in actual_name:
            return False, (
                f"customer name {extraction.payer_customer_name!r} "
                f"missing substring {expected['payer_customer_name_contains']!r}"
            )

    # Customer number is null
    if expected.get("payer_customer_number_is_null"):
        if extraction.payer_customer_number is not None:
            return False, (
                f"expected payer_customer_number=None, "
                f"got {extraction.payer_customer_number!r}"
            )

    # Bank UTR was set
    if expected.get("bank_utr_is_set"):
        if not extraction.bank_utr:
            return False, "bank_utr was not extracted from narrative"

    # Payment mode was set
    if expected.get("payment_mode_is_set"):
        if not extraction.payment_mode:
            return False, "payment_mode was not extracted from narrative"

    return True, None


@pytest.mark.parametrize("case", EVAL_SET, ids=[c["id"] for c in EVAL_SET])
def test_extraction(case):
    """Run one eval case EVAL_RUNS times, pass if PASS_THRESHOLD or more succeed."""
    bank_payment = get_bank_payment_by_id(case["bank_txn_id"])
    assert bank_payment is not None, (
        f"Bank payment {case['bank_txn_id']} not found in DB. "
        f"Maybe it was deleted from dev — pick a different example."
    )

    runs = []
    for i in range(EVAL_RUNS):
        try:
            result = extract_bank_payment(bank_payment)
            extraction = result["extraction"]
            routing_decision = result["routing_decision"]
            passed, failure_reason = _check_one_run(case, extraction, routing_decision)
        except Exception as e:
            passed = False
            failure_reason = f"raised {type(e).__name__}: {e}"
            extraction = None
            routing_decision = None

        runs.append({
            "run": i + 1,
            "passed": passed,
            "failure_reason": failure_reason,
            "match_method": extraction.match_method if extraction else None,
            "confidence": extraction.confidence if extraction else None,
            "routing": routing_decision,
        })

    # Print per-run summary even on success — useful diagnostic for boundary cases
    pass_count = sum(1 for r in runs if r["passed"])
    print(f"\n{case['id']}: {pass_count}/{EVAL_RUNS} runs passed")
    for r in runs:
        status = "✓" if r["passed"] else "✗"
        if r["passed"]:
            print(f"  {status} run {r['run']}: method={r['match_method']} "
                  f"conf={r['confidence']:.2f} routing={r['routing']}")
        else:
            print(f"  {status} run {r['run']}: {r['failure_reason']}")

    assert pass_count >= PASS_THRESHOLD, (
        f"Only {pass_count}/{EVAL_RUNS} runs passed (threshold: {PASS_THRESHOLD}). "
        f"See per-run details above."
    )