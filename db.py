# db.py
"""
Read-only data access layer between the SQL gateway API and the agent.

Each function returns Pydantic objects (BankPaymentLine, OpenInvoice).
Agent code never sees SQL or raw dicts.

Note on string parameterization: the SQL gateway API takes a SQL string,
not bind parameters. So we substitute values into the SQL string ourselves.
We use a strict whitelist + escaping to prevent SQL injection.
"""

import re
from typing import Optional

from schemas import BankPaymentLine, OpenInvoice, Customer
from sql_client import query_sql, query_one


# ---- Safe value escaping ----

_SAFE_IDENT_PATTERN = re.compile(r"^[A-Za-z0-9_\-]+$")


def _escape_string_literal(value: str) -> str:
    """Escape a string for safe inclusion in SQL.

    Standard SQL string escape: replace ' with ''. Defense in depth alongside
    _validate_identifier for inputs that come from less trusted sources.
    """
    if not isinstance(value, str):
        raise TypeError(f"Expected string, got {type(value)}")
    return value.replace("'", "''")


def _validate_identifier(value: str, name: str = "value") -> str:
    """Reject values that don't look like business identifiers.

    Customer numbers, invoice numbers, txn IDs are alphanumeric with a few
    delimiters. Anything else is suspicious and should be refused.
    """
    if not _SAFE_IDENT_PATTERN.match(value):
        raise ValueError(
            f"{name} contains unexpected characters: {value!r}. "
            f"Expected alphanumeric, underscore, or hyphen only."
        )
    return value


# ============================================================
# Bank payment queries
# ============================================================

# Common SELECT clause kept as a constant — keeps queries DRY and ensures
# Pydantic schemas always see all the columns they expect.
_BANK_COLUMNS = """
    bank_txn_id, reference, statement_id, account_number, bank_name,
    statement_date, value_date, entry_date,
    currency, amount, transaction_type, payment_mode,
    cheque_number, narrative, status, source_file, created_by, vin,
    created_date, updated_date, load_date
"""


def get_bank_payment_by_id(bank_txn_id: str) -> Optional[BankPaymentLine]:
    """Look up one specific bank payment by its bank_txn_id."""
    safe_id = _escape_string_literal(bank_txn_id)
    query = f"""
        SELECT {_BANK_COLUMNS}
        FROM cashapp.t_raw_bank_statements
        WHERE bank_txn_id = '{safe_id}'
        LIMIT 1
    """
    row = query_one(query)
    return BankPaymentLine.model_validate(row) if row else None


def get_recent_bank_payments(limit: int = 10) -> list[BankPaymentLine]:
    """Most recent bank payments. For development and exploration."""
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    query = f"""
        SELECT {_BANK_COLUMNS}
        FROM cashapp.t_raw_bank_statements
        ORDER BY statement_date DESC NULLS LAST, bank_txn_id
        LIMIT {limit}
    """
    rows = query_sql(query)
    return [BankPaymentLine.model_validate(r) for r in rows]


def get_bank_payments_by_status(status: str, limit: int = 100) -> list[BankPaymentLine]:
    """Bank payments filtered by status. Useful for finding 'exception' or 'matched' rows."""
    safe_status = _escape_string_literal(status)
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    query = f"""
        SELECT {_BANK_COLUMNS}
        FROM cashapp.t_raw_bank_statements
        WHERE status = '{safe_status}'
        ORDER BY statement_date DESC NULLS LAST
        LIMIT {limit}
    """
    rows = query_sql(query)
    return [BankPaymentLine.model_validate(r) for r in rows]


# ============================================================
# Invoice queries
# ============================================================

_INVOICE_COLUMNS = """
    id, entity_code, customer_number, customer_name,
    po_number, invoice_description,
    invoice_number, document_number, invoice_reference,
    invoice_date, posting_date, net_due_date, document_date,
    reason_code, payment_terms, document_type, tax_code, gl_indicator,
    invoice_currency, invoice_amount,
    clearing_document_number, clearing_date,
    status, created_date, updated_date
"""


def get_invoices_for_customer(
    customer_number: str,
    limit: int = 100,
) -> list[OpenInvoice]:
    """All invoices for a single customer.

    For this prototype, no filter on status or clearing_document_number;
    all rows treated as candidates for matching.
    """
    _validate_identifier(customer_number, "customer_number")
    if limit < 1 or limit > 1000:
        raise ValueError("limit must be between 1 and 1000")
    query = f"""
        SELECT {_INVOICE_COLUMNS}
        FROM cashapp.t_invoice_header
        WHERE customer_number = '{customer_number}'
        ORDER BY invoice_date DESC NULLS LAST
        LIMIT {limit}
    """
    rows = query_sql(query)
    return [OpenInvoice.model_validate(r) for r in rows]


def get_invoice_by_number(
    customer_number: str,
    invoice_number: str,
) -> Optional[OpenInvoice]:
    """Find a specific invoice for a customer by invoice number."""
    _validate_identifier(customer_number, "customer_number")
    _validate_identifier(invoice_number, "invoice_number")
    query = f"""
        SELECT {_INVOICE_COLUMNS}
        FROM cashapp.t_invoice_header
        WHERE customer_number = '{customer_number}'
          AND invoice_number = '{invoice_number}'
        LIMIT 1
    """
    row = query_one(query)
    return OpenInvoice.model_validate(row) if row else None


def search_customers_by_name(name_fragment: str, limit: int = 20) -> list[OpenInvoice]:
    """Find invoices where customer_name LIKE '%name_fragment%'.

    Useful for: agent has 'SHRINATH SHIPPING' from a bank narrative;
    needs to find candidate customer numbers.

    Returns: list of invoices (which carry customer_number) — caller
    deduplicates by customer_number to get unique customers.
    """
    safe = _escape_string_literal(name_fragment.upper())
    if len(name_fragment) < 3:
        raise ValueError("name_fragment must be at least 3 characters")
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    query = f"""
        SELECT {_INVOICE_COLUMNS}
        FROM cashapp.t_invoice_header
        WHERE UPPER(customer_name) LIKE '%{safe}%'
        ORDER BY customer_name
        LIMIT {limit}
    """
    rows = query_sql(query)
    return [OpenInvoice.model_validate(r) for r in rows]


# ============================================================
# Customer master queries
# ============================================================

_CUSTOMER_COLUMNS = """
    customer_number, customer_name, vin, entity_code,
    bill_to_name, bill_to_address, bill_to_phone, bill_to_contact,
    ship_to_name, ship_to_address, ship_to_phone, ship_to_contact,
    tax_id_1, tax_id_2, tax_id_3, payment_terms,
    created_date, updated_date, created_by
"""


def get_customer_by_vin(vin: str) -> Optional[Customer]:
    """
    Look up a customer by their virtual account identification number.

    Returns Customer if VIN matches; None if no match (which happens for
    ~5% of bank VINs in our dev data — real-world data quality).
    """
    _validate_identifier(vin, "vin")
    query = f"""
        SELECT {_CUSTOMER_COLUMNS}
        FROM cashapp.t_customer_master
        WHERE vin = '{vin}'
        LIMIT 1
    """
    row = query_one(query)
    return Customer.model_validate(row) if row else None


def get_customer_by_number(customer_number: str) -> Optional[Customer]:
    """Look up a customer by their primary customer number."""
    _validate_identifier(customer_number, "customer_number")
    query = f"""
        SELECT {_CUSTOMER_COLUMNS}
        FROM cashapp.t_customer_master
        WHERE customer_number = '{customer_number}'
        LIMIT 1
    """
    row = query_one(query)
    return Customer.model_validate(row) if row else None


def search_customers_by_name_master(name_fragment: str, limit: int = 20) -> list[Customer]:
    """
    Search the customer master by name (vs search_customers_by_name which
    searches via the invoice table). Use this when you don't have a VIN
    and need to fuzzy-match against the customer base.
    """
    safe = _escape_string_literal(name_fragment.upper())
    if len(name_fragment) < 3:
        raise ValueError("name_fragment must be at least 3 characters")
    if limit < 1 or limit > 200:
        raise ValueError("limit must be between 1 and 200")
    query = f"""
        SELECT {_CUSTOMER_COLUMNS}
        FROM cashapp.t_customer_master
        WHERE UPPER(customer_name) LIKE '%{safe}%'
        ORDER BY customer_name
        LIMIT {limit}
    """
    rows = query_sql(query)
    return [Customer.model_validate(r) for r in rows]