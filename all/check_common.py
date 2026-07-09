"""Shared helpers for the read-only check_*.py diagnostics (NEW nested format).

Email docs are one-file-per-doc with a `transactions[]` array; SAP READ docs carry a
`transaction[]` array. These flatten either into a list of raw transaction rows so the
diagnostics can inspect column names / values just like the old flat feed.
"""
from __future__ import annotations


def email_txn_rows(docs: list[dict]) -> list[dict]:
    """Flatten nested email FILE docs -> raw transaction rows (file context injected)."""
    rows: list[dict] = []
    for d in docs:
        ctx = {"uploadDate": d.get("uploadDate"), "filename": d.get("id"),
               "vendorId": d.get("vendorId")}
        for t in (d.get("transactions") or []):
            rows.append({**t, **ctx})
    return rows


def sap_txn_rows(docs: list[dict]) -> list[dict]:
    """Flatten SAP READ docs -> raw transaction rows (accepts already-flat rows too)."""
    rows: list[dict] = []
    for d in docs:
        txns = d.get("transaction")
        rows.extend(txns if isinstance(txns, list) else [d])
    return rows
