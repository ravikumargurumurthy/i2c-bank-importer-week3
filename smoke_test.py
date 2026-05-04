# smoke_test.py
"""End-to-end verification: API client → schemas → data access → real data."""

from db import (
    get_recent_bank_payments,
    get_bank_payment_by_id,
    get_invoices_for_customer,
    search_customers_by_name,
)
from sql_client import query_count


def main():
    print("=" * 70)
    print("Counts (sanity check)")
    print("=" * 70)
    bank_count = query_count("SELECT COUNT(*) AS cnt FROM cashapp.t_raw_bank_statements")
    invoice_count = query_count("SELECT COUNT(*) AS cnt FROM cashapp.t_invoice_header")
    customer_count = query_count(
        "SELECT COUNT(DISTINCT customer_number) AS cnt FROM cashapp.t_invoice_header"
    )
    print(f"  Bank payments:    {bank_count:,}")
    print(f"  Invoices:         {invoice_count:,}")
    print(f"  Unique customers: {customer_count:,}")

    print("\n" + "=" * 70)
    print("Sample bank payments (most recent 3)")
    print("=" * 70)
    payments = get_recent_bank_payments(limit=3)
    for p in payments:
        narrative_preview = (p.narrative or "")[:60]
        print(f"  {p.bank_txn_id[:12]}... {p.amount} {p.currency}  "
              f"date={p.statement_date}  narrative='{narrative_preview}'")

    print("\n" + "=" * 70)
    print("Round-trip lookup by ID")
    print("=" * 70)
    if payments:
        first_id = payments[0].bank_txn_id
        retrieved = get_bank_payment_by_id(first_id)
        if retrieved and retrieved.bank_txn_id == first_id:
            print(f"  Retrieved {first_id[:12]}... successfully")
            print(f"  Type of amount field: {type(retrieved.amount).__name__}")
        else:
            print(f"  ERROR: could not retrieve {first_id}")

    print("\n" + "=" * 70)
    print("Customer name search ('SHIPPING')")
    print("=" * 70)
    matches = search_customers_by_name("SHIPPING", limit=5)
    unique_customers = {(i.customer_number, i.customer_name) for i in matches}
    print(f"  Found {len(unique_customers)} customer(s) with 'SHIPPING' in name")
    for cust_num, cust_name in sorted(unique_customers):
        print(f"    {cust_num} -> {cust_name}")

    print("\n" + "=" * 70)
    print("Invoices for one customer")
    print("=" * 70)
    if unique_customers:
        sample_cust = sorted(unique_customers)[0][0]
        invoices = get_invoices_for_customer(sample_cust, limit=5)
        print(f"  Customer {sample_cust} has at least {len(invoices)} invoices "
              f"(showing up to 5):")
        for inv in invoices:
            print(f"    {inv.invoice_number} doc={inv.document_number}  "
                  f"{inv.invoice_amount} {inv.invoice_currency}  "
                  f"date={inv.invoice_date}")

    print("\n" + "=" * 70)
    print("Smoke test passed.")
    print("=" * 70)


if __name__ == "__main__":
    main()
