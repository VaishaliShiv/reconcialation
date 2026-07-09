"""Reconcile by VENDOR + DATE only — the email file is found automatically. READ-ONLY.

You don't pass an email file name. You pass the vendor id and the date (the same date that
names the SAP file, vendorid_date). The script then:
  1. picks the SAP file(s) for that vendor + date  (vendorid / uploadDate / id-digits, any shape)
  2. picks the email file(s) whose ACTIVE transactions fall on that date (trnDate/settlementDate)
  3. reconciles each picked email file against the picked SAP file(s) and prints the result

Nothing is written to Cosmos — this is a dry-run test aid, fully separate from
run_cosmos_workflow.py (it neither imports nor modifies it).

    python run_by_vendor_date.py VENDOR_ID DATE[,DATE2,...]
    python run_by_vendor_date.py nbd1Qmid 20260629
    python run_by_vendor_date.py hbb1Qmid 20260520,20260521

DATE is YYYYMMDD (also accepts 2026-06-29 — dashes are stripped). If several email files touch
the date, all of them are reconciled. An email row dated OUTSIDE the given date(s) will show as
missing_in_sap (expected — you only pulled that date's SAP file).
"""
from __future__ import annotations

import sys
import pathlib

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


def _sap_vendor(doc: dict) -> str | None:
    return cmap._s(doc.get("vendorid") or doc.get("vendorId"))


def _sap_datevalue(doc: dict) -> str | None:
    """SAP doc -> datevalue (YYYYMMDD): datetimelist -> uploadDate -> id digits."""
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
    """YYYYMMDD dates a set of transactions fall on (trnDate + settlementDate)."""
    dvs: set[str] = set()
    for t in txns:
        if t.txn_date:
            dvs.add(t.txn_date.isoformat().replace("-", ""))
        if t.settlement_date:
            dvs.add(t.settlement_date.isoformat().replace("-", ""))
    return dvs


def main() -> int:
    args = sys.argv[1:]
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) < 2:
        print(__doc__)
        print("ERROR: need VENDOR_ID and DATE, e.g.  python run_by_vendor_date.py nbd1Qmid 20260629")
        return 2
    vendor = positional[0]
    dates = {d.strip().replace("-", "") for d in positional[1].split(",") if d.strip()}

    print(f"RECONCILE BY VENDOR+DATE (read-only, no Cosmos writes)  vendor={vendor}  "
          f"dates={sorted(dates)}\n")

    email_docs = _read_all(settings.cosmos_file_container)
    sap_raw = _read_all(settings.cosmos_sap_container)

    # SAP side: the vendor's SAP file(s) for exactly these date(s).
    sap_docs = [d for d in sap_raw if _sap_vendor(d) == vendor and _sap_datevalue(d) in dates]
    print(f"email files: {len(email_docs)}   "
          f"SAP files for {vendor} on {sorted(dates)}: {len(sap_docs)} of {len(sap_raw)} raw")
    if sap_raw and not sap_docs:
        s = sap_raw[0]
        print("⚠️  no SAP file matched. First raw SAP doc:")
        print(f"    keys={sorted(k for k in s if not k.startswith('_'))}  "
              f"vendor={_sap_vendor(s)!r} date={_sap_datevalue(s)!r}")
    sap_txns = cmap.map_sap_read(sap_docs)

    # Email side: auto-pick the file(s) whose active transactions fall on the date(s).
    picked = []
    for doc in email_docs:
        if cmap._s(doc.get("vendorId")) != vendor:
            continue
        _fn, _v, _up, txns = cmap.map_email_doc(doc, vendor)
        active = [t for t in txns if (t.status or "").lower() != "completed"]
        if _txn_datevalues(active) & dates:
            picked.append((doc, _fn, active, len(txns) - len(active)))
    print(f"email files matching vendor+date: {len(picked)}\n")

    if not picked:
        print("No email file has transactions on that date for this vendor — nothing to reconcile.")
        return 0

    tally: dict[str, int] = {}
    for _doc, filename, active, skipped in picked:
        print(f"===== FILE {filename}  vendor={vendor}  dates={sorted(dates)}  "
              f"active={len(active)} skipped(Completed)={skipped}  "
              f"sap_files={len(sap_docs)} sap_txns={len(sap_txns)} =====")
        run_id = report.run_id_for(vendor)
        rows, snap, meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)
        report.print_summary(rows, snap, meta, vendor)
        for r in rows:
            tally[r["classification"]] = tally.get(r["classification"], 0) + 1

    print(f"\n===== DONE. classifications -> {tally} =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
