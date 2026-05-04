# sql_client.py
"""
Thin client for the cashapp SQL gateway API.

Wraps requests to a single endpoint with:
- envelope handling: extracts `data` from {status, data, message, error}
- read-only enforcement: blocks any SQL containing write keywords
- error surfacing: transforms API errors into Python exceptions
- retry: exponential backoff on transient failures (network, 5xx)

The agent code never calls requests directly — all DB access goes through
query_sql() so we have one place to add caching, retries, observability,
or auth headers if/when needed.
"""

import os
import re
import time
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("SQL_API_ENDPOINT", "https://i2c-api-dev.fractal.ai/v1/db")
DEFAULT_TIMEOUT = int(os.getenv("SQL_API_TIMEOUT", "60"))

# ---- Read-only enforcement ----
# Aggressive on purpose: blocks any SQL containing a write keyword as a
# whole word. False positives are acceptable — we'd rather refuse a
# legitimate query than slip through a destructive one.
_WRITE_KEYWORD_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE|"
    r"REPLACE|MERGE|UPSERT|COPY|VACUUM|REINDEX)\b",
    re.IGNORECASE,
)


class SQLClientError(Exception):
    """Raised when the SQL gateway returns an error or unexpected response."""


class WriteSQLBlockedError(SQLClientError):
    """Raised when a query containing write keywords is attempted."""


def _check_read_only(query: str) -> None:
    """Reject any query containing write keywords.

    This is a defensive layer ON TOP of any database-level read-only role.
    Both should agree; either alone would be sufficient; together they're robust.
    """
    if _WRITE_KEYWORD_PATTERN.search(query):
        raise WriteSQLBlockedError(
            f"Query rejected: contains write keyword. "
            f"This client only supports SELECT queries. "
            f"Query (first 200 chars): {query[:200]}"
        )


def query_sql(
    query: str,
    timeout: Optional[int] = None,
    retries: int = 2,
) -> List[Dict[str, Any]]:
    """Execute a SELECT query and return list of row dicts.

    Args:
        query: A SELECT SQL string.
        timeout: Request timeout in seconds. Defaults to SQL_API_TIMEOUT.
        retries: Retries on transient failures (network, 5xx). 0 disables.

    Returns:
        A list of dicts, one per row, with column names as keys.
        Empty list if no rows match.

    Raises:
        WriteSQLBlockedError: query contains write keywords.
        SQLClientError: API call failed or returned an error envelope.
    """
    _check_read_only(query)
    timeout = timeout or DEFAULT_TIMEOUT
    url = f"{ENDPOINT}/sql"

    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                url,
                json={"query": query},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s backoff
                continue
            raise SQLClientError(
                f"Network error after {retries + 1} attempts: {exc}"
            ) from exc

        # 5xx → retry
        if response.status_code >= 500 and attempt < retries:
            time.sleep(2 ** attempt)
            continue

        if response.status_code != 200:
            raise SQLClientError(
                f"HTTP {response.status_code}: {response.text[:500]}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise SQLClientError(
                f"Response is not valid JSON: {response.text[:200]}"
            ) from exc

        if not isinstance(payload, dict):
            raise SQLClientError(f"Unexpected response shape: {payload}")

        # API envelope: {status, data, message, error}
        if payload.get("status") is False or payload.get("error"):
            raise SQLClientError(
                f"API error: {payload.get('error') or payload.get('message')}"
            )

        data = payload.get("data", [])
        if not isinstance(data, list):
            raise SQLClientError(f"`data` is not a list: {type(data)}")

        return data

    raise SQLClientError(f"All {retries + 1} attempts exhausted: {last_exc}")


def query_one(query: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Convenience: returns the first row or None."""
    rows = query_sql(query, **kwargs)
    return rows[0] if rows else None


def query_count(query: str, **kwargs) -> int:
    """Convenience: SELECT COUNT(*) AS cnt FROM ... → returns the int."""
    row = query_one(query, **kwargs)
    if not row:
        return 0
    # The API may return the count under various aliases; grab the only value
    return int(next(iter(row.values())))
