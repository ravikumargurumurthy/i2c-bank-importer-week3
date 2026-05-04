# tools.py
"""
Deterministic tools for the bank payment extraction agent.

The agent calls these to ground its reasoning in real data:
- extract_payment_signals_from_narrative: regex parsing of slash-delimited bank narratives
- lookup_customer_by_vin: deterministic customer ID via virtual account number
- lookup_customer_by_name: fuzzy match against ~3,907 customers (fallback)
- lookup_invoices_for_customer: open AR for matching candidate

The agent's job is to decide WHICH tool path to use given the input,
and how confident the result is. The tools themselves are dumb — they
just return what the data says.
"""

import re
from decimal import Decimal
from typing import Optional

from rapidfuzz import fuzz, process

from db import (
    get_customer_by_vin,
    search_customers_by_name_master,
    get_invoices_for_customer,
)


# ---- Constants ----

# VIN format: ZLAD + 14 alphanumeric. Strict on purpose — better to miss
# a malformed VIN than match a false positive.
VIN_PATTERN = re.compile(r"\bZLAD[A-Z0-9]{14}\b")

# Common Indian banking payment modes seen in narratives
PAYMENT_MODE_PATTERN = re.compile(
    r"^(NEFT|RTGS|IMPS|UPI|CHEQUE|CHQ|CASH|TRANSFER|TRF|BACS|SWIFT|WIRE)\b",
    re.IGNORECASE,
)

# Generic corporate suffixes to strip when fuzzy-matching customer names.
# Same idea as Week 1, expanded for Indian corporate forms.
SUFFIX_PATTERN = re.compile(
    r"\b(PVT\s*LTD|PRIVATE\s*LIMITED|LTD|LIMITED|INC|INCORPORATED|"
    r"CORP|CORPORATION|CO|COMPANY|LLC|LLP|GMBH|AG|PTE\s*LTD|"
    r"INDIA|INDIAN|SOLUTIONS|SERVICES|GROUP|HOLDINGS)\.?\s*$",
    re.IGNORECASE,
)


# ============================================================
# Tool 1: extract signals from narrative
# ============================================================

def extract_payment_signals_from_narrative(narrative: str) -> dict:
    """
    Parse a slash-delimited bank narrative into structured fields.

    Example input:
        'NEFT/HDFCH00705513688/ZLAD2001A600000409/VA/V LOGISTICS AND SERVICES/HDFC BANK/'

    Example output:
        {
            'payment_mode': 'NEFT',
            'bank_utr': 'HDFCH00705513688',
            'vin': 'ZLAD2001A600000409',
            'is_virtual_account': True,  # because 'VA' segment present
            'payer_name_candidate': 'V LOGISTICS AND SERVICES',
            'segments': ['NEFT', 'HDFCH00705513688', 'ZLAD2001A600000409', 'VA',
                         'V LOGISTICS AND SERVICES', 'HDFC BANK', '']
        }

    All fields are best-effort. If the narrative is malformed or doesn't
    match expected patterns, fields will be None — the agent should handle
    this and lower confidence accordingly.
    """
    if not narrative:
        return {
            "payment_mode": None,
            "bank_utr": None,
            "vin": None,
            "is_virtual_account": False,
            "payer_name_candidate": None,
            "segments": [],
            "raw": narrative,
        }

    # Split by slash; trim whitespace from each segment
    segments = [s.strip() for s in narrative.split("/")]

    # Payment mode: first segment if it matches known modes
    payment_mode = None
    if segments:
        m = PAYMENT_MODE_PATTERN.match(segments[0])
        if m:
            payment_mode = m.group(1).upper()

    # Bank UTR: typically the second segment (e.g., HDFCH00705513688, CHASH00007900862)
    # Pattern: starts with bank prefix letters, followed by digits.
    bank_utr = None
    if len(segments) >= 2 and re.match(r"^[A-Z]{4,}\d{8,}$", segments[1]):
        bank_utr = segments[1]

    # VIN: search the entire narrative for ZLAD pattern
    vin = None
    vin_match = VIN_PATTERN.search(narrative)
    if vin_match:
        vin = vin_match.group(0)

    # Virtual account flag: segment 'VA' appears when VIN is in use
    is_virtual_account = "VA" in segments

    # Payer name candidate: heuristic — look for a segment that's all-caps
    # multi-word text after the VIN/VA marker
    payer_name_candidate = _guess_payer_name(segments, vin_position=_find_segment_index(segments, vin))

    return {
        "payment_mode": payment_mode,
        "bank_utr": bank_utr,
        "vin": vin,
        "is_virtual_account": is_virtual_account,
        "payer_name_candidate": payer_name_candidate,
        "segments": segments,
        "raw": narrative,
    }


def _find_segment_index(segments: list[str], target: Optional[str]) -> int:
    """Find the index of a segment containing the target string (-1 if not found)."""
    if not target:
        return -1
    for i, s in enumerate(segments):
        if target in s:
            return i
    return -1


def _guess_payer_name(segments: list[str], vin_position: int = -1) -> Optional[str]:
    """
    Heuristic payer name extraction.

    Strategy:
    - Skip empty / single-char / numeric / structural segments
    - Skip segments containing BANK / BK / BNK markers (almost always the payer's bank, not payer name)
    - Score remaining candidates by position (after VIN is better) and word count (multi-word better)
    - Return the highest-scoring candidate
    """
    candidates = []
    for i, seg in enumerate(segments):
        # Skip empty, short, or pure-numeric segments
        if not seg or len(seg) < 4 or seg.isdigit():
            continue
        # Skip known non-payer markers
        if seg.upper() in ("VA", "NEFT", "RTGS", "IMPS", "UPI"):
            continue
        # Skip segments that look like bank UTRs
        if re.match(r"^[A-Z]{4,}\d{8,}$", seg):
            continue
        # Skip segments that look like VINs
        if VIN_PATTERN.match(seg):
            continue
        # Skip long all-digit segments (account numbers, account IDs)
        if re.match(r"^\d{8,}$", seg):
            continue
        # NEW: Skip segments that look like bank names (BANK, BNK, BK as whole words)
        if re.search(r"\b(BANK|BNK|BK)\b", seg.upper()):
            continue

        # If we're after the VIN/VA position, this is a strong candidate
        position_score = 1.0 if (vin_position >= 0 and i > vin_position) else 0.5

        # Multi-word strings score higher
        word_count = len(seg.split())
        if word_count >= 2:
            position_score += 0.5

        # NEW: Earlier position is slightly preferred when no VIN (payer name typically appears
        # before bank info in NEFT narratives)
        if vin_position < 0:
            position_score += (1.0 / (i + 1)) * 0.3  # small earlier-position boost

        candidates.append((position_score, seg))

    if not candidates:
        return None

    candidates.sort(reverse=True)
    return candidates[0][1]


# ============================================================
# Tool 2: lookup customer by VIN
# ============================================================

def lookup_customer_by_vin_tool(vin: str) -> dict:
    """
    Resolve a VIN to a customer.

    Returns:
        {
            'customer_number': str | None,
            'customer_name': str | None,
            'entity_code': str | None,
            'matched': bool,
            'match_method': 'vin_exact'
        }
    """
    customer = get_customer_by_vin(vin)
    if customer:
        return {
            "customer_number": customer.customer_number,
            "customer_name": customer.customer_name,
            "entity_code": customer.entity_code,
            "matched": True,
            "match_method": "vin_exact",
        }
    return {
        "customer_number": None,
        "customer_name": None,
        "entity_code": None,
        "matched": False,
        "match_method": "vin_exact",
        "error": f"VIN {vin} not found in customer master (~5% of real bank VINs don't match)",
    }


# ============================================================
# Tool 3: lookup customer by name (fuzzy)
# ============================================================

def _normalize_name(name: str) -> str:
    """Strip generic suffixes and normalize whitespace for fuzzy matching."""
    if not name:
        return ""
    s = name.upper().strip()
    s = SUFFIX_PATTERN.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


# ============================================================
# Tool 4: lookup invoices for customer
# ============================================================

def lookup_invoices_for_customer_tool(customer_number: str, limit: int = 50) -> dict:
    """
    Get open invoices for a customer.

    For Week 3, all invoices are treated as candidates (no filter on
    status / clearing). The matching agent in Project 2 (Weeks 7-9)
    will narrow this down by amount, date, and document type.
    """
    try:
        invoices = get_invoices_for_customer(customer_number, limit=limit)
    except Exception as e:
        return {
            "invoices": [],
            "count": 0,
            "error": f"Invoice lookup failed: {e}",
        }

    return {
        "invoices": [
            {
                "invoice_number": inv.invoice_number,
                "document_number": inv.document_number,
                "invoice_amount": str(inv.invoice_amount),
                "invoice_currency": inv.invoice_currency,
                "invoice_date": str(inv.invoice_date) if inv.invoice_date else None,
                "document_type": inv.document_type,
            }
            for inv in invoices
        ],
        "count": len(invoices),
    }
