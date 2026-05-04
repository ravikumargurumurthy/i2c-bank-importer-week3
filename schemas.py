# schemas.py
"""
Pydantic schemas for cashapp tables, modeled on real production columns.

Handles three data quirks observed in the dev database:
1. Bank `amount` arrives as float; we coerce to Decimal for safe arithmetic.
2. Invoice `invoice_amount` arrives as string; Decimal(string) is exact.
3. Many text fields are empty strings ("") instead of NULL; we normalize
   to None at the schema boundary so downstream code can use truthiness checks.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def _empty_to_none(value):
    """Coerce empty strings to None. Used by validators for Optional[str] fields."""
    if value == "" or value is None:
        return None
    return value


class BankPaymentLine(BaseModel):
    """
    One row from cashapp.t_raw_bank_statements.

    Primary key: `bank_txn_id` (UUID-like string from your DE pipeline).
    Most important fields for the agent:
    - `narrative` (free-text payment description; primary input for parsing)
    - `amount` (transaction amount; INR for this client)
    - `payment_mode` (NEFT/RTGS/IMPS/CHEQUE/etc. for Indian banking)
    - `value_date` (when funds available)
    """

    # Identifiers
    bank_txn_id: str
    reference: Optional[str] = None
    statement_id: Optional[str] = None

    # Account
    account_number: Optional[str] = None
    bank_name: Optional[str] = None

    # Dates
    statement_date: Optional[date] = None
    value_date: Optional[date] = None
    entry_date: Optional[date] = None

    # Money
    currency: Optional[str] = Field(None, max_length=3)
    amount: Decimal

    # Transaction details
    transaction_type: Optional[str] = None
    payment_mode: Optional[str] = None
    cheque_number: Optional[str] = None
    narrative: Optional[str] = None

    # Pipeline metadata
    status: Optional[str] = None
    source_file: Optional[str] = None
    created_by: Optional[str] = None
    vin: Optional[str] = None
    created_date: Optional[datetime] = None
    updated_date: Optional[datetime] = None
    load_date: Optional[datetime] = None

    # ---- Validators: empty-string-to-None for Optional[str] fields ----
    @field_validator(
        "reference", "statement_id", "account_number", "bank_name",
        "currency", "transaction_type", "payment_mode", "cheque_number",
        "narrative", "status", "source_file", "created_by", "vin",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v):
        return _empty_to_none(v)

    # ---- Amount: float (or string) → Decimal (safe for money) ----
    @field_validator("amount", mode="before")
    @classmethod
    def _amount_to_decimal(cls, v):
        if v is None:
            raise ValueError("amount is required")
        # str() conversion handles floats safely (avoids float-binary precision)
        return Decimal(str(v))

    class Config:
        from_attributes = True


class OpenInvoice(BaseModel):
    """
    One row from cashapp.t_invoice_header.

    For this learning project, all rows are treated as candidates for matching
    (no filter on status or clearing_document_number). Production deployment
    would filter by appropriate open/closed semantics.

    Most important fields for matching:
    - `invoice_number` (primary match key)
    - `document_number` (often referenced in remittance instead of invoice_number)
    - `customer_number`, `customer_name` (entity match)
    - `invoice_amount` (amount-driven matching)
    - `po_number`, `invoice_reference` (alternative match keys)
    """

    # Synthetic primary key
    id: int

    # Entity / customer
    entity_code: Optional[str] = None
    customer_number: Optional[str] = None
    customer_name: Optional[str] = None

    # Multiple matching keys — these are why I2C matching is hard
    invoice_number: Optional[str] = None
    document_number: Optional[str] = None
    po_number: Optional[str] = None
    invoice_reference: Optional[str] = None

    # Document metadata
    invoice_description: Optional[str] = None
    document_type: Optional[str] = None
    reason_code: Optional[str] = None
    payment_terms: Optional[str] = None
    tax_code: Optional[str] = None
    gl_indicator: Optional[str] = None

    # Dates
    invoice_date: Optional[date] = None
    posting_date: Optional[date] = None
    document_date: Optional[date] = None
    net_due_date: Optional[date] = None

    # Money
    invoice_currency: Optional[str] = Field(None, max_length=3)
    invoice_amount: Decimal

    # Clearing info — not used for filtering per project scope
    clearing_document_number: Optional[str] = None
    clearing_date: Optional[date] = None

    # Status / lifecycle
    status: Optional[str] = None

    # Audit
    created_date: Optional[datetime] = None
    updated_date: Optional[datetime] = None

    # ---- Validators ----
    @field_validator(
        "entity_code", "customer_number", "customer_name",
        "invoice_number", "document_number", "po_number", "invoice_reference",
        "invoice_description", "document_type", "reason_code", "payment_terms",
        "tax_code", "gl_indicator", "invoice_currency",
        "clearing_document_number", "status",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, v):
        return _empty_to_none(v)

    @field_validator("invoice_amount", mode="before")
    @classmethod
    def _amount_to_decimal(cls, v):
        if v is None:
            raise ValueError("invoice_amount is required")
        return Decimal(str(v))

    class Config:
        from_attributes = True
