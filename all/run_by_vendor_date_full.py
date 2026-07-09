"""Reconcile by VENDOR + DATE, WITH the LLM summary + SAP write-back logic — WRITES OFF.

Same auto-selection as run_by_vendor_date.py (pass only vendor + date, the email file is found
by its transaction date), PLUS the two enrichment/write steps the real pipeline does:

  1. LLM run-summary   (run_summary.summarize — deterministic unless TRIAGE_ENABLED=true)
  2. SAP write-back    (reconflag + remarks stamped into the SAP file's transactions)
  3. summary doc       (the doc that would go to the summary container)

>>> WRITES ARE OFF FOR TESTING <<<
WRITE_SAP and WRITE_SUMMARY below are both False, so NOTHING is upserted to Cosmos — the SAP
write payload, the stamped rows, and the summary doc are only PRINTED. Flip a flag to True when
you're ready to actually write (the upsert code is already wired behind each flag).

    python run_by_vendor_date_full.py VENDOR_ID DATE[,DATE2,...]
    python run_by_vendor_date_full.py nbd1Qmid 20260629

This is a separate test file; it does not import or modify run_cosmos_workflow.py.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bank_reconciliation.config import settings                          # noqa: E402
from bank_reconciliation.recon import report, sap_writeback, summary_store  # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap          # noqa: E402
from bank_reconciliation.triage import enrich_anomalies, run_summary     # noqa: E402

# ============================================================================
# WRITE SWITCHES — both OFF for testing. Nothing reaches Cosmos while False.
# Flip to True (one or both) only when you actually want to persist.
# ============================================================================
WRITE_SAP = False        # upsert reconflag/remarks into the SAP source container
WRITE_SUMMARY = False     # upsert the summary doc into the results/summary container


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
    dvs: set[str] = set()
    for t in txns:
        if t.txn_date:
            dvs.add(t.txn_date.isoformat().replace("-", ""))
        if t.settlement_date:
            dvs.add(t.settlement_date.isoformat().replace("-", ""))
    return dvs


def _write_sap_back(sap_docs: list[dict], rows: list[dict], vendor: str) -> None:
    """SAP write-back. WRITES OFF -> print the payload + which rows WOULD be stamped.
    When WRITE_SAP is True, upsert each stamped SAP doc into the SAP source container."""
    primary_date = _sap_datevalue(sap_docs[0]) if sap_docs else ""
    payload = sap_writeback.build_write_payload(rows, vendor, primary_date)
    print(f"\n--- SAP WRITE-BACK ({'ON' if WRITE_SAP else 'OFF — preview only'}) ---")
    print(f"WRITE payload would carry {len(payload['transaction'])} stampable txn(s):")
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))

    for sd in sap_docs:
        stamped = copy.deepcopy(sd)                       # never mutate the source doc
        filled_doc, n = sap_writeback.fill_sap_read(stamped, rows)
        tag = f"{vendor}_{_sap_datevalue(sd)}"
        if WRITE_SAP and n and filled_doc.get("id"):
            clean = {k: v for k, v in filled_doc.items() if not k.startswith("_")}
            _cosmos_container(settings.cosmos_sap_container).upsert_item(clean)
            print(f"  [WROTE] stamped {n} row(s) into SAP file {tag}")
        else:
            print(f"  [OFF] would stamp {n} row(s) in SAP file {tag}")


def _emit_summary(vendor: str, run_id: str, date: str, summary_text: str, filename: str) -> None:
    """Build the summary doc. WRITES OFF -> print it. When WRITE_SUMMARY is True, upsert it."""
    sdoc = sap_writeback.build_summary(vendor, run_id, date=date,
                                       summary_text=summary_text, filename=filename)
    print(f"\n--- SUMMARY DOC ({'ON' if WRITE_SUMMARY else 'OFF — preview only'}) ---")
    if WRITE_SUMMARY:
        print(f"[WROTE] summary upserted: id={summary_store.upsert(sdoc)}")
    else:
        print("[OFF] summary doc that WOULD be upserted:")
        print(json.dumps(sdoc, indent=2, default=str, ensure_ascii=False))


def main() -> int:
    args = sys.argv[1:]
    positional = [a for a in args if not a.startswith("-")]
    if len(positional) < 2:
        print(__doc__)
        print("ERROR: need VENDOR_ID and DATE, e.g.  python run_by_vendor_date_full.py nbd1Qmid 20260629")
        return 2
    vendor = positional[0]
    dates = {d.strip().replace("-", "") for d in positional[1].split(",") if d.strip()}

    print(f"RECONCILE+ENRICH BY VENDOR+DATE  vendor={vendor}  dates={sorted(dates)}  "
          f"triage={'on' if settings.triage_enabled else 'off'}  "
          f"WRITE_SAP={WRITE_SAP}  WRITE_SUMMARY={WRITE_SUMMARY}\n")

    email_docs = _read_all(settings.cosmos_file_container)
    sap_raw = _read_all(settings.cosmos_sap_container)

    sap_docs = [d for d in sap_raw if _sap_vendor(d) == vendor and _sap_datevalue(d) in dates]
    print(f"email files: {len(email_docs)}   "
          f"SAP files for {vendor} on {sorted(dates)}: {len(sap_docs)} of {len(sap_raw)} raw")
    if sap_raw and not sap_docs:
        s = sap_raw[0]
        print("⚠️  no SAP file matched. First raw SAP doc:")
        print(f"    keys={sorted(k for k in s if not k.startswith('_'))}  "
              f"vendor={_sap_vendor(s)!r} date={_sap_datevalue(s)!r}")
    sap_txns = cmap.map_sap_read(sap_docs)

    picked = []
    for doc in email_docs:
        if cmap._s(doc.get("vendorId")) != vendor:
            continue
        _fn, _v, upload, txns = cmap.map_email_doc(doc, vendor)
        active = [t for t in txns if (t.status or "").lower() != "completed"]
        if _txn_datevalues(active) & dates:
            picked.append((_fn, upload, active, len(txns) - len(active)))
    print(f"email files matching vendor+date: {len(picked)}\n")

    if not picked:
        print("No email file has transactions on that date for this vendor — nothing to reconcile.")
        return 0

    tally: dict[str, int] = {}
    for filename, upload, active, skipped in picked:
        date = upload.isoformat() if upload else sorted(dates)[0]
        print(f"===== FILE {filename}  vendor={vendor}  dates={sorted(dates)}  "
              f"active={len(active)} skipped(Completed)={skipped}  "
              f"sap_files={len(sap_docs)} sap_txns={len(sap_txns)} =====")
        run_id = report.run_id_for(vendor)
        rows, snap, meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)
        report.print_summary(rows, snap, meta, vendor)

        enrich_anomalies(rows)                        # LLM anomaly notes (only if TRIAGE_ENABLED)
        summary_text = run_summary.summarize(rows, snap, vendor, date)   # LLM/deterministic summary
        print("\n--- RUN SUMMARY (LLM if enabled, else deterministic) ---")
        print(summary_text)

        _write_sap_back(sap_docs, rows, vendor)       # WRITES OFF -> preview
        _emit_summary(vendor, run_id, date, summary_text, filename)      # WRITES OFF -> preview

        for r in rows:
            tally[r["classification"]] = tally.get(r["classification"], 0) + 1

    print(f"\n===== DONE. classifications -> {tally}  (WRITE_SAP={WRITE_SAP}, "
          f"WRITE_SUMMARY={WRITE_SUMMARY}) =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
