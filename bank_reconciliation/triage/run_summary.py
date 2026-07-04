"""Run-level natural-language summary for the `summary` container's `summary` field.

AI-phrased when triage is enabled, grounded against the deterministic counts/totals;
falls back to a deterministic template on any failure. Numbers are never hallucinated.
"""
from __future__ import annotations

import logging

from ..config import settings
from . import client, guardrails

log = logging.getLogger(__name__)

SYSTEM = (
    "You summarize one bank-reconciliation run in 2-3 plain sentences for a finance manager. "
    "Use ONLY the figures given — never invent a number. All amounts are in AED (UAE Dirham): "
    "write 'AED', never a '$' or dollar sign. Be concise and factual."
)


def _facts(rows: list[dict], snap, vendorid: str, date: str) -> dict:
    by: dict[str, int] = {}
    for r in rows:
        by[r["classification"]] = by.get(r["classification"], 0) + 1
    matched = by.get("matched", 0)
    return {
        "vendorid": vendorid, "date": date, "total": len(rows), "matched": matched,
        "anomalies": len(rows) - matched, "by": by,
        "file_total": snap.file_total, "sap_total": snap.sap_total,
        "balance_diff": snap.balance_diff, "recon_status": snap.recon_status.value,
    }


def _deterministic(f: dict) -> str:
    parts = [f"{f['total']} transactions reconciled for {f['vendorid']} (email {f['date']}): "
             f"{f['matched']} matched, {f['anomalies']} anomalies."]
    anoms = {k: v for k, v in f["by"].items() if k != "matched"}
    if anoms:
        parts.append("Breakdown: " + ", ".join(f"{v} {k}" for k, v in anoms.items()) + ".")
    parts.append(f"File total AED {f['file_total']}, SAP total AED {f['sap_total']}, "
                 f"difference AED {f['balance_diff']} ({f['recon_status']}).")
    return " ".join(parts)


def _user(f: dict) -> str:
    return (f"vendor={f['vendorid']} email_date={f['date']} total={f['total']} "
            f"matched={f['matched']} anomalies={f['anomalies']} breakdown={f['by']} "
            f"file_total={f['file_total']} sap_total={f['sap_total']} "
            f"balance_diff={f['balance_diff']} status={f['recon_status']}")


def summarize(rows: list[dict], snap, vendorid: str, date: str, *, complete_fn=None) -> str:
    """Return the run summary text. Deterministic template unless triage is enabled and the
    AI text is grounded; any failure falls back to the deterministic template."""
    f = _facts(rows, snap, vendorid, date)
    base = _deterministic(f)
    if not settings.triage_enabled:
        return base
    row_like = {"amount_source": f["file_total"], "amount_sap": f["sap_total"],
                "amount_diff": f["balance_diff"]}
    try:
        text = ((complete_fn or client.complete_text)(SYSTEM, _user(f)) or "").strip()
        if text and guardrails.numbers_are_grounded(text, row_like):
            return text
        log.warning("run summary discarded (ungrounded/empty) -> deterministic")
    except Exception as exc:  # noqa: BLE001 - never break the run
        log.warning("run summary AI failed (%s) -> deterministic", exc)
    return base
