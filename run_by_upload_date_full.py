"""Reconcile by UPLOAD DATE + LLM summary + SAP write-back — WRITES OFF (print-only).

Same (vendorId, uploadDate) 1:1 pairing as run_by_upload_date.py, PLUS the enrichment/write
steps of the real pipeline:
  1. LLM anomaly notes   (enrich_anomalies — only if TRIAGE_ENABLED)
  2. money-weighted structured summary  (run_summary.summarize_structured)
  3. SAP write-back      (reconflag + remarks stamped into the paired SAP file)
  4. summary doc         (the doc that would go to the summary container)

>>> WRITES ARE OFF FOR TESTING <<<
WRITE_SAP / WRITE_SUMMARY are both False: the WRITE payload, stamped rows, and summary doc are
only PRINTED. Flip a flag to True when ready — the upsert code is wired behind each flag.

    python run_by_upload_date_full.py mrb1Qmid 20260708
    python run_by_upload_date_full.py mrb1Qmid
    python run_by_upload_date_full.py

Separate file; does not import or modify run_cosmos_workflow.py.
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
# ============================================================================
WRITE_SAP = False
WRITE_SUMMARY = False


def _cosmos_container(name: str):
    from azure.cosmos import CosmosClient  # lazy import
    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    return client.get_database_client(settings.cosmos_database).get_container_client(name)


def _read_all(name: str) -> list[dict]:
    return list(_cosmos_container(name).query_items(
        query="SELECT * FROM c", enable_cross_partition_query=True, max_item_count=1000))


def _vendor(doc: dict) -> str | None:
    return cmap._s(doc.get("vendorId") or doc.get("vendorid"))


def _upload_dv(doc: dict) -> str | None:
    """Doc's uploadDate as YYYYMMDD — the pairing key (falls back to datetimelist / id digits)."""
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


def _write_sap_back(sap_doc: dict | None, rows: list[dict], vendor: str, iso_date: str) -> None:
    """SAP write-back. WRITES OFF -> print payload + rows that WOULD be stamped."""
    payload = sap_writeback.build_write_payload(rows, vendor, iso_date)
    print(f"\n--- SAP WRITE-BACK ({'ON' if WRITE_SAP else 'OFF — preview only'}) ---")
    print(f"WRITE payload would carry {len(payload['transaction'])} stampable txn(s):")
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    if not sap_doc:
        print("  (no paired SAP file — nothing to stamp)")
        return
    stamped = copy.deepcopy(sap_doc)                      # never mutate the source doc
    filled_doc, n = sap_writeback.fill_sap_read(stamped, rows)
    tag = sap_doc.get("id")
    if WRITE_SAP and n and filled_doc.get("id"):
        clean = {k: v for k, v in filled_doc.items() if not k.startswith("_")}
        _cosmos_container(settings.cosmos_sap_container).upsert_item(clean)
        print(f"  [WROTE] stamped {n} row(s) into SAP file {tag}")
    else:
        print(f"  [OFF] would stamp {n} row(s) in SAP file {tag}")


def _emit_summary(vendor: str, run_id: str, iso_date: str, summary_text: str, filename: str) -> None:
    sdoc = sap_writeback.build_summary(vendor, run_id, date=iso_date,
                                       summary_text=summary_text, filename=filename)
    print(f"\n--- SUMMARY DOC ({'ON' if WRITE_SUMMARY else 'OFF — preview only'}) ---")
    if WRITE_SUMMARY:
        print(f"[WROTE] summary upserted: id={summary_store.upsert(sdoc)}")
    else:
        print("[OFF] summary doc that WOULD be upserted:")
        print(json.dumps(sdoc, indent=2, default=str, ensure_ascii=False))


def _process(email_doc: dict, sap_doc: dict | None, vendor: str) -> dict[str, int]:
    filename, _v, upload, txns = cmap.map_email_doc(email_doc, vendor)
    active = [t for t in txns if (t.status or "").lower() != "completed"]
    skipped = len(txns) - len(active)
    iso_date = upload.isoformat() if upload else ""
    sap_txns = cmap.map_sap_read([sap_doc]) if sap_doc else []

    print(f"\n===== EMAIL {filename}  vendor={vendor}  uploadDate={_upload_dv(email_doc)}  "
          f"txns={len(txns)} active={len(active)} skipped(Completed)={skipped} =====")
    print(f"      paired SAP file: {sap_doc.get('id') if sap_doc else '(none)'}   sap_txns={len(sap_txns)}")
    if not sap_doc:
        print("⚠️  no SAP file with the same (vendorId, uploadDate) — every record is missing_in_sap.")

    run_id = report.run_id_for(vendor)
    rows, snap, meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)
    report.print_summary(rows, snap, meta, vendor)

    enrich_anomalies(rows)                                # LLM notes only if TRIAGE_ENABLED
    summary = run_summary.summarize_structured(rows, snap, vendor, iso_date)
    print("\n--- STRUCTURED SUMMARY ---")
    print(f"HEADLINE : {summary.headline}")
    print(f"PROSE    : {summary.summary_text}")
    print(f"HEALTH={summary.health}  exposure_aed={summary.exposure_aed}  "
          f"unreconciled_pct={summary.unreconciled_pct}  top_actions={summary.top_actions}")

    _write_sap_back(sap_doc, rows, vendor, iso_date)
    _emit_summary(vendor, run_id, iso_date, summary.summary_text, filename)

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["classification"]] = tally.get(r["classification"], 0) + 1
    return tally


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    vendor_filter = positional[0] if positional else None
    upload_filter = positional[1].replace("-", "") if len(positional) > 1 else None

    print(f"RECONCILE+ENRICH BY UPLOAD DATE  vendor={vendor_filter or 'ALL'}  "
          f"uploadDate={upload_filter or 'ALL'}  triage={'on' if settings.triage_enabled else 'off'}  "
          f"WRITE_SAP={WRITE_SAP}  WRITE_SUMMARY={WRITE_SUMMARY}\n")

    email_docs = _read_all(settings.cosmos_file_container)
    sap_docs = _read_all(settings.cosmos_sap_container)
    print(f"email files: {len(email_docs)}   SAP files: {len(sap_docs)}")

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
        tally = _process(doc, sap_index.get((vendor, _upload_dv(doc))), vendor)
        for k, v in tally.items():
            grand[k] = grand.get(k, 0) + v

    print(f"\n===== DONE. {len(files)} file(s) -> {grand}  "
          f"(WRITE_SAP={WRITE_SAP}, WRITE_SUMMARY={WRITE_SUMMARY}) =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
