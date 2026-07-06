"""Deterministic SAP write-back (mode=WRITE) + run summary.

Money-critical rules:
- `reconflag` is binary: MATCHED | ANOMALY, derived ONLY from the engine classification.
- `remarks` is a SHORT deterministic string per classification — NEVER AI text.
- SAP write is gated by SAP_WRITE_ENABLED; default is dry-run (build payload, don't send).

The `result` container receives ONLY the run summary (not per-row detail).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from ..schema.canonical_mapper import _norm_key

# Result-row id column -> SAP WRITE key
_ID_MAP = {
    "dewatransactionid": "dewa_trn_reference_no",
    "partnertransactionid": "partner_trn_reference_no",
    "paymentreferencenumber": "payment_ref_no",
}

# Short remarks (the SAP comment must be small). One line per classification.
_REMARKS = {
    "matched": "Reconciled with partner ledger",
    "amount_mismatch": "Amount mismatch",
    "date_mismatch": "Date mismatch",
    "missing_in_sap": "Not posted in SAP",
    "missing_in_file": "Not in bank file",
    "duplicate": "Duplicate posting",
    "invalid_record": "Invalid record",
}


def reconflag_for(classification: str | None) -> str:
    return "MATCHED" if classification == "matched" else "ANOMALY"


def remarks_for(classification: str | None) -> str:
    return _REMARKS.get(classification or "", "Anomaly")


def _to_datevalue(iso_date: str) -> str:
    """'2026-06-29' -> '20260629'. Passthrough if already compact or empty."""
    return (iso_date or "").replace("-", "")


def _txn(row: dict) -> dict:
    return {
        sap_key: row.get(col) or None for sap_key, col in _ID_MAP.items()
    } | {
        "reconflag": reconflag_for(row.get("classification")),
        "remarks": remarks_for(row.get("classification")),
    }


def build_write_payload(rows: list[dict], vendorid: str, iso_date: str) -> dict:
    """Build the SAP WRITE plaintext request. invalid_record rows are skipped
    (returned to bank, not reconciled)."""
    txns = [_txn(r) for r in rows if r.get("classification") != "invalid_record"]
    return {
        "vendorid": vendorid,
        "mode": "WRITE",
        "datetime": {"datetimelist": {"datevalue": _to_datevalue(iso_date)}},
        "transaction": txns,
    }


def _key_to_flag_remark(rows: list[dict]) -> dict[str, tuple[str, str]]:
    """resolved join key -> (reconflag, remarks), from the recon result rows."""
    out: dict[str, tuple[str, str]] = {}
    for r in rows:
        ku = r.get("match_key_used") or ""
        val = ku.split(":", 1)[1] if ":" in ku else ku
        key = _norm_key(val)
        if key:
            cls = r.get("classification")
            out[key] = (reconflag_for(cls), remarks_for(cls))
    return out


def fill_sap_read(response: dict, rows: list[dict]) -> tuple[dict, int]:
    """Fill reconflag + remarks into each transaction of a SAP READ response, matching
    on partner/payment/dewa id. Returns (response, filled_count). Rows SAP doesn't hold
    (e.g. missing_in_sap) simply don't appear here — only real SAP rows get stamped."""
    lookup = _key_to_flag_remark(rows)
    filled = 0
    for t in response.get("transaction", []):
        key = (_norm_key(t.get("partnertransactionid"))
               or _norm_key(t.get("paymentreferencenumber"))
               or _norm_key(t.get("dewatransactionid")))
        hit = lookup.get(key)
        if hit:
            t["reconflag"], t["remarks"] = hit
            filled += 1
    return response, filled


def write_sap_read_file(path, response: dict) -> None:
    """Persist the reconflag/remarks-filled SAP READ response back to its JSON file."""
    with open(path, "w") as f:
        json.dump(response, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---- OLD FORMAT summary id = vendor:date (kept commented for reference) ----
# def build_summary(vendorid: str, run_id: str, *, date: str, summary_text: str) -> dict:
#     return {
#         "id": f"{vendorid}:{date}" if date else run_id,
#         "vendor_id": vendorid,
#         "date": date,
#         "status": "complete",
#         "run_id": run_id,
#         "summary": summary_text,
#         "generated_at": datetime.now(timezone.utc).isoformat(),
#     }


def build_summary(vendorid: str, run_id: str, *, date: str, summary_text: str,
                  filename: str | None = None) -> dict:
    """The `summary`-container doc. NEW format: keyed by the file name (unique per file).

    Each email file has its own unique name (`id`), so the summary id IS that file name.
    A file can span settlement dates, so date is descriptive only (stored, not the key).
    `summary_text` is the DETAILED run summary for the summary container.
    """
    return {
        "id": filename or (f"{vendorid}:{date}" if date else run_id),
        "vendor_id": vendorid,
        "filename": filename,         # unique email file name (Cosmos doc id)
        "date": date,                 # email upload day (day-level) — descriptive
        "status": "complete",         # the comparison our code performed is finished
        "run_id": run_id,
        "summary": summary_text,      # DETAILED grounded run summary text
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
