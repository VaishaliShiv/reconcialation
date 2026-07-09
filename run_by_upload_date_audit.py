"""Reconcile by UPLOAD DATE + LLM summary + SAP write-back + EVIDENCE audit trail.

Same (vendorId, uploadDate) 1:1 pairing as run_by_upload_date_full.py, and additionally writes
ONE evidence record PER COMPARISON to the evidence container — the append-only audit trail
(run_id, refs, amounts, verdict, rule, action, comment, timestamp) so every decision is provable
later.

>>> ALL WRITES OFF FOR TESTING <<<
WRITE_SAP / WRITE_SUMMARY / WRITE_EVIDENCE are all False: payloads, summary doc, and evidence
records are only PRINTED. Flip a flag to True to persist. The evidence container is separate and
append-only, so WRITE_EVIDENCE is safe to turn on independently of the other two.

    python run_by_upload_date_audit.py mrb1Qmid 20260708
    python run_by_upload_date_audit.py mrb1Qmid
    python run_by_upload_date_audit.py

Separate file; does not import or modify run_cosmos_workflow.py or run_by_upload_date_full.py.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bank_reconciliation.config import settings                          # noqa: E402
from bank_reconciliation.evidence.store import EvidenceStore             # noqa: E402
from bank_reconciliation.models import Action, EvidenceRecord, MatchType  # noqa: E402
from bank_reconciliation.recon import report, sap_writeback, summary_store  # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap          # noqa: E402
from bank_reconciliation.triage import enrich_anomalies, run_summary     # noqa: E402

# ============================================================================
# WRITE SWITCHES — all OFF for testing. Nothing reaches Cosmos while False.
# ============================================================================
WRITE_SAP = False
WRITE_SUMMARY = False
WRITE_EVIDENCE = False       # per-comparison audit trail -> evidence container (append-only)


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


def _key_val(r: dict) -> str:
    ku = r.get("match_key_used") or ""
    return (ku.split(":", 1)[1] if ":" in ku else ku) or (r.get("partner_trn_reference_no") or "")


def _evidence_record(r: dict, vendor: str, run_id: str, file_date) -> EvidenceRecord | None:
    """One audit record for one comparison row (captures the decision + the two sides)."""
    try:
        mt = MatchType(r["classification"])
    except (ValueError, KeyError):
        return None
    try:
        act = Action(r.get("action") or "none")
    except ValueError:
        act = Action.NONE
    sap_rows = ([{"amount": r.get("amount_sap"), "date": report._cell(r.get("date_sap"))}]
                if r.get("amount_sap") not in (None, "") else [])
    return EvidenceRecord(
        run_id=run_id, bank_name=vendor, file_date=file_date,
        partner_txn_id=_key_val(r),
        file_row={"partner_trn_reference_no": r.get("partner_trn_reference_no"),
                  "payment_ref_no": r.get("payment_ref_no"),
                  "dewa_trn_reference_no": r.get("dewa_trn_reference_no"),
                  "amount": r.get("amount_source"), "date": report._cell(r.get("date_source")),
                  "type": r.get("type")},
        sap_rows=sap_rows,
        key_used=r.get("match_key_used") or "",
        file_amount=r.get("amount_source"), sap_amount=r.get("amount_sap"),
        diff=r.get("amount_diff") or 0.0,
        match_type=mt, rule_fired=r["classification"], action=act,
        comment=r.get("comment") or "", confidence=1.0,
        status=r.get("status") or r["classification"],
        ts=datetime.now(timezone.utc),
    )


def _write_evidence(rows: list[dict], vendor: str, run_id: str, file_date) -> None:
    """Build one evidence record per comparison; write to the evidence container (or preview)."""
    recs = [rec for r in rows if (rec := _evidence_record(r, vendor, run_id, file_date))]
    print(f"\n--- EVIDENCE ({'ON' if WRITE_EVIDENCE else 'OFF — preview only'}) ---")
    print(f"{len(recs)} evidence record(s) — one per comparison — for run {run_id}")
    if WRITE_EVIDENCE:
        try:
            store = EvidenceStore()
            for rec in recs:
                store.write(rec)
            n = store.flush()
            print(f"  [WROTE] {n} evidence doc(s) upserted to '{settings.cosmos_evidence_container}'")
        except Exception as exc:  # noqa: BLE001 - audit write must never break the run
            print(f"  [ERROR] evidence write failed ({exc}) — audit trail NOT written")
    elif recs:
        print("  sample evidence doc that WOULD be written (id = run_id:partner_txn_id):")
        print(json.dumps(recs[0].model_dump(mode="json"), indent=2, default=str, ensure_ascii=False))
        print(f"  (set WRITE_EVIDENCE=True to write all {len(recs)} to the evidence container)")


def _write_sap_back(sap_doc: dict | None, rows: list[dict], vendor: str, iso_date: str) -> None:
    payload = sap_writeback.build_write_payload(rows, vendor, iso_date)
    print(f"\n--- SAP WRITE-BACK ({'ON' if WRITE_SAP else 'OFF — preview only'}) ---")
    print(f"WRITE payload would carry {len(payload['transaction'])} stampable txn(s):")
    print(json.dumps(payload, indent=2, default=str, ensure_ascii=False))
    if not sap_doc:
        print("  (no paired SAP file — nothing to stamp)")
        return
    stamped = copy.deepcopy(sap_doc)
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

    enrich_anomalies(rows)
    summary = run_summary.summarize_structured(rows, snap, vendor, iso_date)
    print("\n--- STRUCTURED SUMMARY ---")
    print(f"HEADLINE : {summary.headline}")
    print(f"PROSE    : {summary.summary_text}")
    print(f"HEALTH={summary.health}  exposure_aed={summary.exposure_aed}  "
          f"unreconciled_pct={summary.unreconciled_pct}  top_actions={summary.top_actions}")

    _write_sap_back(sap_doc, rows, vendor, iso_date)
    _emit_summary(vendor, run_id, iso_date, summary.summary_text, filename)
    _write_evidence(rows, vendor, run_id, upload)         # audit trail, one record per comparison

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["classification"]] = tally.get(r["classification"], 0) + 1
    return tally


def main() -> int:
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    vendor_filter = positional[0] if positional else None
    upload_filter = positional[1].replace("-", "") if len(positional) > 1 else None

    print(f"RECONCILE+ENRICH+AUDIT BY UPLOAD DATE  vendor={vendor_filter or 'ALL'}  "
          f"uploadDate={upload_filter or 'ALL'}  triage={'on' if settings.triage_enabled else 'off'}  "
          f"WRITE_SAP={WRITE_SAP}  WRITE_SUMMARY={WRITE_SUMMARY}  WRITE_EVIDENCE={WRITE_EVIDENCE}\n")

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

    print(f"\n===== DONE. {len(files)} file(s) -> {grand}  (WRITE_SAP={WRITE_SAP}, "
          f"WRITE_SUMMARY={WRITE_SUMMARY}, WRITE_EVIDENCE={WRITE_EVIDENCE}) =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
