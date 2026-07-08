"""Reconcile by UPLOAD DATE — email file <-> SAP file paired on (vendorId, uploadDate). READ-ONLY.

This implements the daily workflow:
  - each day an email file is processed and stored in Cosmos with an `uploadDate`;
  - the app then calls the SAP API for every distinct Transaction Date in that file and stores
    ONE SAP doc for the day, with the SAME `uploadDate` (and `id` = vendorId-uploadDate);
  - so an email file and its SAP file share (vendorId, uploadDate) 1:1.

The comparison therefore PAIRS on `uploadDate` (not transaction date), then reconciles every
record. A single uploaded file may carry several Transaction Dates; the matcher handles them
per-record (a match still requires the email `trnDate` to equal the SAP `transactiondate`).

Nothing is written to Cosmos — this prints matches + discrepancies for testing. It does not
import or modify run_cosmos_workflow.py.

    python run_by_upload_date.py                       # every email file, paired by uploadDate
    python run_by_upload_date.py mrb1Qmid              # one vendor's files
    python run_by_upload_date.py mrb1Qmid 20260708     # one pair (vendor + uploadDate; 2026-07-08 ok too)
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bank_reconciliation.config import settings                      # noqa: E402
from bank_reconciliation.recon import report                         # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap      # noqa: E402


def _cosmos_container(name: str):
    from azure.cosmos import CosmosClient  # lazy import
    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    return client.get_database_client(settings.cosmos_database).get_container_client(name)


def _read_all(name: str) -> list[dict]:
    return list(_cosmos_container(name).query_items(
        query="SELECT * FROM c", enable_cross_partition_query=True, max_item_count=1000))


def _vendor(doc: dict) -> str | None:
    """vendorId on either side, tolerant of casing (vendorId / vendorid)."""
    return cmap._s(doc.get("vendorId") or doc.get("vendorid"))


def _upload_dv(doc: dict) -> str | None:
    """A doc's uploadDate as YYYYMMDD — the pairing key. Falls back to datetimelist / id digits."""
    up = cmap._s(doc.get("uploadDate"))
    if up:
        digits = up.replace("-", "")
        if len(digits) == 8 and digits.isdigit():
            return digits
    dv = ((doc.get("datetime") or {}).get("datetimelist") or {}).get("datevalue")
    if cmap._s(dv):
        return cmap._s(dv)
    tail = "".join(ch for ch in (cmap._s(doc.get("id")) or "") if ch.isdigit())[-8:]
    return tail if len(tail) == 8 else None


def _by_tran_date(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Group result rows by their transaction date -> {matched, anomalies} per date."""
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        d = report._cell(r.get("date_source")) or report._cell(r.get("date_sap")) or "(no date)"
        bucket = out.setdefault(d, {"matched": 0, "anomalies": 0})
        bucket["matched" if r.get("classification") == "matched" else "anomalies"] += 1
    return out


def _reconcile_pair(email_doc: dict, sap_doc: dict | None, vendor: str) -> dict[str, int]:
    filename, _v, upload, txns = cmap.map_email_doc(email_doc, vendor)
    active = [t for t in txns if (t.status or "").lower() != "completed"]
    skipped = len(txns) - len(active)
    upload_dv = _upload_dv(email_doc)
    sap_txns = cmap.map_sap_read([sap_doc]) if sap_doc else []
    sap_id = sap_doc.get("id") if sap_doc else "(none)"

    print(f"\n===== EMAIL {filename}  vendor={vendor}  uploadDate={upload_dv}  "
          f"txns={len(txns)} active={len(active)} skipped(Completed)={skipped} =====")
    print(f"      paired SAP file: {sap_id}   sap_txns={len(sap_txns)}")
    if not sap_doc:
        print("⚠️  no SAP file with the same (vendorId, uploadDate) — every record is missing_in_sap.")

    run_id = report.run_id_for(vendor)
    rows, snap, meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)
    report.print_summary(rows, snap, meta, vendor)

    # per-Transaction-Date view (one uploaded file can span several tran dates)
    grouped = _by_tran_date(rows)
    print("  by transaction date:")
    for d in sorted(grouped):
        g = grouped[d]
        print(f"    {d}:  {g['matched']} matched, {g['anomalies']} discrepancy(ies)")

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["classification"]] = tally.get(r["classification"], 0) + 1
    return tally


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    vendor_filter = positional[0] if positional else None
    upload_filter = positional[1].replace("-", "") if len(positional) > 1 else None

    print(f"RECONCILE BY UPLOAD DATE (read-only)  vendor={vendor_filter or 'ALL'}  "
          f"uploadDate={upload_filter or 'ALL'}\n")

    email_docs = _read_all(settings.cosmos_file_container)
    sap_docs = _read_all(settings.cosmos_sap_container)
    print(f"email files: {len(email_docs)}   SAP files: {len(sap_docs)}")

    # index SAP files by (vendor, uploadDate) — the 1:1 pairing key
    sap_index: dict[tuple, dict] = {}
    for d in sap_docs:
        sap_index[(_vendor(d), _upload_dv(d))] = d

    files = [d for d in email_docs
             if (not vendor_filter or vendor_filter.upper() == "ALL" or _vendor(d) == vendor_filter)
             and (not upload_filter or _upload_dv(d) == upload_filter)]
    print(f"email files to process: {len(files)}")

    grand: dict[str, int] = {}
    for doc in files:
        vendor = _vendor(doc)
        key = (vendor, _upload_dv(doc))
        tally = _reconcile_pair(doc, sap_index.get(key), vendor)
        for k, v in tally.items():
            grand[k] = grand.get(k, 0) + v

    print(f"\n===== DONE. {len(files)} file(s) -> {grand} =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
