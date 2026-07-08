"""Money-weighted STRUCTURED run summary, by vendor + date. READ-ONLY, prints only.

Demonstrates the enhanced summary agent (bank_reconciliation.triage.run_summary):
  - all figures (exposure, largest anomaly, %, health) are computed DETERMINISTICALLY;
  - the prose is AI-phrased only when TRIAGE_ENABLED and every figure is grounded, else a
    deterministic template — so the numbers can never be hallucinated.

Pass only vendor + date (same auto-selection as run_by_vendor_date.py). Writes nothing to
Cosmos; does not import or modify run_cosmos_workflow.py.

    python summary_report.py VENDOR_ID DATE[,DATE2,...]
    python summary_report.py hbb1Qmid 20260520
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bank_reconciliation.config import settings                      # noqa: E402
from bank_reconciliation.recon import report                         # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap      # noqa: E402
from bank_reconciliation.triage import run_summary                   # noqa: E402


def _cosmos_container(name: str):
    from azure.cosmos import CosmosClient  # lazy import
    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    return client.get_database_client(settings.cosmos_database).get_container_client(name)


def _read_all(name: str) -> list[dict]:
    return list(_cosmos_container(name).query_items(
        query="SELECT * FROM c", enable_cross_partition_query=True, max_item_count=1000))


def _sap_vendor(doc: dict) -> str | None:
    return cmap._s(doc.get("vendorid") or doc.get("vendorId"))


def _sap_datevalue(doc: dict) -> str | None:
    dv = ((doc.get("datetime") or {}).get("datetimelist") or {}).get("datevalue")
    if cmap._s(dv):
        return cmap._s(dv)
    up = cmap._s(doc.get("uploadDate"))
    if up:
        digits = up.replace("-", "")
        return digits if len(digits) == 8 and digits.isdigit() else None
    tail = "".join(ch for ch in (cmap._s(doc.get("id")) or "") if ch.isdigit())[-8:]
    return tail if len(tail) == 8 else None


def _txn_datevalues(txns: list) -> set[str]:
    dvs: set[str] = set()
    for t in txns:
        if t.txn_date:
            dvs.add(t.txn_date.isoformat().replace("-", ""))
        if t.settlement_date:
            dvs.add(t.settlement_date.isoformat().replace("-", ""))
    return dvs


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(positional) < 2:
        print(__doc__)
        print("ERROR: need VENDOR_ID and DATE, e.g.  python summary_report.py hbb1Qmid 20260520")
        return 2
    vendor = positional[0]
    dates = {d.strip().replace("-", "") for d in positional[1].split(",") if d.strip()}

    print(f"STRUCTURED SUMMARY (read-only)  vendor={vendor}  dates={sorted(dates)}  "
          f"triage={'on' if settings.triage_enabled else 'off (deterministic)'}\n")

    email_docs = _read_all(settings.cosmos_file_container)
    sap_raw = _read_all(settings.cosmos_sap_container)
    sap_docs = [d for d in sap_raw if _sap_vendor(d) == vendor and _sap_datevalue(d) in dates]
    sap_txns = cmap.map_sap_read(sap_docs)

    picked = []
    for doc in email_docs:
        if cmap._s(doc.get("vendorId")) != vendor:
            continue
        _fn, _v, upload, txns = cmap.map_email_doc(doc, vendor)
        active = [t for t in txns if (t.status or "").lower() != "completed"]
        if _txn_datevalues(active) & dates:
            picked.append((_fn, upload, active))
    if not picked:
        print("No email file has transactions on that date for this vendor.")
        return 0

    for filename, upload, active in picked:
        date = upload.isoformat() if upload else sorted(dates)[0]
        run_id = report.run_id_for(vendor)
        rows, snap, _meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)
        summary = run_summary.summarize_structured(rows, snap, vendor, date)

        print(f"===== FILE {filename} =====")
        print(f"HEADLINE : {summary.headline}")
        print(f"PROSE    : {summary.summary_text}\n")
        print("STRUCTURED OBJECT (drives a dashboard / notification):")
        print(json.dumps(summary.model_dump(), indent=2, default=str, ensure_ascii=False))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
