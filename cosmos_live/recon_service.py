"""Programmatic entry point for the reconciliation — for orchestrators / agentic flows.

Instead of running a CLI, another system calls ONE function:

    from recon_service import reconcile
    result = reconcile("mrb1Qmid", "20260708")        # vendor id + upload date

and gets back a JSON-serializable dict (matches, discrepancies, money-weighted summary, health,
evidence written). Same (vendorId, uploadDate) pairing and engine as run_by_upload_date_audit.py.

Writes are OFF unless enabled in .env (WRITE_SAP/WRITE_SUMMARY/WRITE_EVIDENCE) or overridden per
call. This module never prints — it returns data — so it can back an API, an MCP tool, or a
direct import. It does not import or modify run_cosmos_workflow.py.
"""
from __future__ import annotations

import copy
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


def _record_view(r: dict) -> dict:
    """One comparison row as clean, JSON-safe primitives."""
    return {
        "ref": _key_val(r),
        "partner_ref": r.get("partner_trn_reference_no"),
        "payment_ref": r.get("payment_ref_no"),
        "dewa_ref": r.get("dewa_trn_reference_no"),
        "amount_email": r.get("amount_source"),
        "amount_sap": r.get("amount_sap"),
        "amount_diff": r.get("amount_diff"),
        "date_email": report._cell(r.get("date_source")),
        "date_sap": report._cell(r.get("date_sap")),
        "classification": r.get("classification"),
        "result": "MATCH" if r.get("classification") == "matched" else "ANOMALY",
        "action": r.get("action"),
        "comment": r.get("comment"),
    }


def _write_evidence(rows: list[dict], vendor: str, run_id: str, file_date) -> int:
    recs = []
    for r in rows:
        try:
            mt = MatchType(r["classification"])
        except (ValueError, KeyError):
            continue
        try:
            act = Action(r.get("action") or "none")
        except ValueError:
            act = Action.NONE
        recs.append(EvidenceRecord(
            run_id=run_id, bank_name=vendor, file_date=file_date, partner_txn_id=_key_val(r),
            key_used=r.get("match_key_used") or "",
            file_amount=r.get("amount_source"), sap_amount=r.get("amount_sap"),
            diff=r.get("amount_diff") or 0.0, match_type=mt, rule_fired=r["classification"],
            action=act, comment=r.get("comment") or "", confidence=1.0,
            status=r.get("status") or r["classification"], ts=datetime.now(timezone.utc)))
    store = EvidenceStore()
    for rec in recs:
        store.write(rec)
    return store.flush()


def _process(email_doc: dict, sap_doc: dict | None, vendor: str,
             write_sap: bool, write_summary: bool, write_evidence: bool,
             include_records: bool) -> dict:
    filename, _v, upload, txns = cmap.map_email_doc(email_doc, vendor)
    active = [t for t in txns if (t.status or "").lower() != "completed"]
    iso_date = upload.isoformat() if upload else ""
    sap_txns = cmap.map_sap_read([sap_doc]) if sap_doc else []

    run_id = report.run_id_for(vendor)
    rows, snap, _meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)
    enrich_anomalies(rows)
    summary = run_summary.summarize_structured(rows, snap, vendor, iso_date)

    writes = {"sap": False, "summary": False, "evidence_written": 0}
    if write_sap and sap_doc:
        stamped = copy.deepcopy(sap_doc)
        filled, n = sap_writeback.fill_sap_read(stamped, rows)
        if n and filled.get("id"):
            _cosmos_container(settings.cosmos_sap_container).upsert_item(
                {k: v for k, v in filled.items() if not k.startswith("_")})
            writes["sap"] = True
    if write_summary:
        sdoc = sap_writeback.build_summary(vendor, run_id, date=iso_date,
                                           summary_text=summary.summary_text, filename=filename)
        summary_store.upsert(sdoc)
        writes["summary"] = True
    if write_evidence:
        writes["evidence_written"] = _write_evidence(rows, vendor, run_id, upload)

    return {
        "email_file_id": filename, "sap_file_id": sap_doc.get("id") if sap_doc else None,
        "run_id": run_id, "sap_file_found": sap_doc is not None,
        "total": summary.total, "matched": summary.matched, "anomalies": summary.anomalies,
        "counts": summary.counts, "health": summary.health,
        "exposure_aed": summary.exposure_aed, "unreconciled_pct": summary.unreconciled_pct,
        "file_total": summary.file_total, "sap_total": summary.sap_total,
        "balance_diff": summary.balance_diff, "recon_status": summary.recon_status,
        "headline": summary.headline, "summary": summary.summary_text,
        "top_actions": summary.top_actions,
        "discrepancies": [_record_view(r) for r in rows if r.get("classification") != "matched"],
        "records": [_record_view(r) for r in rows] if include_records else None,
        "writes": writes,
    }


def reconcile(vendor_id: str, upload_date: str, *,
              write_sap: bool | None = None, write_summary: bool | None = None,
              write_evidence: bool | None = None, include_records: bool = False) -> dict:
    """Run the whole reconciliation for one (vendor_id, upload_date) and RETURN a JSON dict.

    upload_date accepts '20260708' or '2026-07-08'. Write flags default to the .env values
    (settings.write_*); pass a bool to override per call. Set include_records=True to also
    return every row (not just discrepancies).
    """
    dv = str(upload_date).replace("-", "")
    ws = settings.write_sap if write_sap is None else write_sap
    wsum = settings.write_summary if write_summary is None else write_summary
    wev = settings.write_evidence if write_evidence is None else write_evidence

    email_docs = _read_all(settings.cosmos_file_container)
    sap_docs = _read_all(settings.cosmos_sap_container)
    sap_index = {(_vendor(d), _upload_dv(d)): d for d in sap_docs}

    files = [d for d in email_docs if _vendor(d) == vendor_id and _upload_dv(d) == dv]
    results = [_process(d, sap_index.get((vendor_id, dv)), vendor_id, ws, wsum, wev, include_records)
               for d in files]

    agg: dict[str, int] = {}
    for fr in results:
        for k, v in fr["counts"].items():
            agg[k] = agg.get(k, 0) + v

    return {
        "status": "ok" if results else "no_email_file",
        "vendor_id": vendor_id, "upload_date": dv,
        "email_files_processed": len(results),
        "writes_enabled": {"sap": ws, "summary": wsum, "evidence": wev},
        "totals": agg,
        "files": results,
    }


if __name__ == "__main__":       # also usable as: python recon_service.py mrb1Qmid 20260708
    import json
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if len(args) < 2:
        print("usage: python recon_service.py VENDOR_ID UPLOAD_DATE")
        sys.exit(2)
    print(json.dumps(reconcile(args[0], args[1]), indent=2, default=str, ensure_ascii=False))
