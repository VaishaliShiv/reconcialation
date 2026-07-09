"""Read-only CSV comparison report — email-source vs SAP, every scenario. NEVER writes Cosmos.

This is a TESTING aid, fully separate from run_cosmos_workflow.py (it does not import or modify
it). It reuses the SAME reconciliation engine (map_email_doc -> pair by date -> reconcile) so the
classifications match the real pipeline exactly, then dumps ONE CSV row per comparison — so you
can eyeball match/anomaly across all files in Excel. Nothing is upserted anywhere.

    python recon_csv_report.py [VENDOR_ID | ALL] [--email-id ID] [--dates D1,D2] [--all-sap] [--out FILE.csv]

Filters mirror run_cosmos_workflow so you can scope to ONE email-file/SAP-file pair for a targeted
test, or omit them to sweep every email file vs its date-paired SAP file(s):

    # one pair
    python recon_csv_report.py hbb1Qmid --email-id 7f5c3b26-... --dates 20260520 --all-sap
    # everything
    python recon_csv_report.py

Covers matched / amount_mismatch / date_mismatch / missing_in_sap / missing_in_file /
duplicate / invalid_record. `result` collapses each to MATCH/ANOMALY for quick filtering.
"""
from __future__ import annotations

import csv
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bank_reconciliation.config import settings                      # noqa: E402
from bank_reconciliation.recon import report                         # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap      # noqa: E402

COLUMNS = ["email_file", "vendor", "sap_file", "partner_ref", "payment_ref", "dewa_ref",
           "type", "amount_email", "amount_sap", "amount_diff", "date_email", "date_sap",
           "classification", "result", "reason"]


# ---- read helpers (own copies — this script never depends on run_cosmos_workflow) ----
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
    """SAP doc -> datevalue (YYYYMMDD): datetimelist -> uploadDate -> id digits (same as runner)."""
    dv = ((doc.get("datetime") or {}).get("datetimelist") or {}).get("datevalue")
    if cmap._s(dv):
        return cmap._s(dv)
    up = cmap._s(doc.get("uploadDate"))
    if up:
        digits = up.replace("-", "")
        return digits if len(digits) == 8 and digits.isdigit() else None
    tail = "".join(ch for ch in (cmap._s(doc.get("id")) or "") if ch.isdigit())[-8:]
    return tail if len(tail) == 8 else None


def _file_datevalues(txns: list) -> set[str]:
    dvs: set[str] = set()
    for t in txns:
        if t.txn_date:
            dvs.add(t.txn_date.isoformat().replace("-", ""))
        if t.settlement_date:
            dvs.add(t.settlement_date.isoformat().replace("-", ""))
    return dvs


# ---- CLI parsing (own copies) ----
def _opt(args: list[str], key: str) -> str | None:
    for i, a in enumerate(args):
        if a == key and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(key + "="):
            return a.split("=", 1)[1]
    return None


def _positionals(args: list[str], consumes: set[str]) -> list[str]:
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in consumes:
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


def _fmt(v) -> str:
    return "" if v is None else str(v)


def _sap_file(vendor: str, date_sap: str) -> str:
    """Derive the SAP file name vendor_<datevalue> from a row's SAP date (blank if none)."""
    digits = (date_sap or "").replace("-", "")
    return f"{vendor}_{digits}" if len(digits) == 8 and digits.isdigit() else ""


def _rows_for_file(doc: dict, vendor_filter: str | None, sap_index: dict,
                   all_sap: bool) -> tuple[str, list[dict], dict]:
    """Reconcile ONE email file vs its paired SAP doc(s); return (filename, csv_rows, counts)."""
    filename, vendor, _upload, txns = cmap.map_email_doc(doc, vendor_filter)
    active = [t for t in txns if (t.status or "").lower() != "completed"]
    file_dvs = _file_datevalues(active)
    if all_sap:
        sap_docs = [d for (v, _dv), d in sap_index.items() if v == vendor]
    else:
        sap_docs = [d for (v, dv), d in sap_index.items() if v == vendor and dv in file_dvs]
    sap_txns = cmap.map_sap_read(sap_docs)

    run_id = report.run_id_for(vendor)
    rows, _snap, _meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)

    csv_rows, counts = [], {}
    for r in rows:
        klass = r["classification"]
        counts[klass] = counts.get(klass, 0) + 1
        date_sap = report._cell(r.get("date_sap"))
        csv_rows.append({
            "email_file": filename, "vendor": vendor,
            "sap_file": _sap_file(vendor, date_sap),
            "partner_ref": _fmt(r.get("partner_trn_reference_no")),
            "payment_ref": _fmt(r.get("payment_ref_no")),
            "dewa_ref": _fmt(r.get("dewa_trn_reference_no")),
            "type": _fmt(r.get("type")),
            "amount_email": _fmt(r.get("amount_source")),
            "amount_sap": _fmt(r.get("amount_sap")),
            "amount_diff": _fmt(r.get("amount_diff")),
            "date_email": report._cell(r.get("date_source")),
            "date_sap": date_sap,
            "classification": klass,
            "result": "MATCH" if klass == "matched" else "ANOMALY",
            "reason": _fmt(r.get("comment")),
        })
    counts["_skipped_completed"] = len(txns) - len(active)
    counts["_sap_files"] = len(sap_docs)
    return filename, csv_rows, counts


def main() -> int:
    args = sys.argv[1:]
    email_id = _opt(args, "--email-id")
    dates_opt = _opt(args, "--dates")
    out_opt = _opt(args, "--out")
    sap_dates = {d.strip() for d in dates_opt.split(",") if d.strip()} if dates_opt else None
    all_sap = "--all-sap" in args
    positional = _positionals(args, consumes={"--email-id", "--dates", "--out"})
    vendor_filter = positional[0] if positional else None

    print(f"CSV REPORT (read-only, no Cosmos writes)  vendor={vendor_filter or 'ALL'}  "
          f"email_id={email_id or 'ALL'}  sap_dates={sorted(sap_dates) if sap_dates else 'ALL'}  "
          f"all_sap={all_sap}\n")

    email_docs = _read_all(settings.cosmos_file_container)
    sap_raw = _read_all(settings.cosmos_sap_container)

    sap_docs = sap_raw
    if vendor_filter and vendor_filter.upper() != "ALL":
        sap_docs = [d for d in sap_docs if _sap_vendor(d) == vendor_filter]
    if sap_dates:
        sap_docs = [d for d in sap_docs if _sap_datevalue(d) in sap_dates]
    print(f"email files: {len(email_docs)}   SAP docs: {len(sap_docs)} of {len(sap_raw)} raw")

    if sap_raw and not sap_docs:
        s = sap_raw[0]
        print("⚠️  SAP docs exist but none matched the scope. First raw SAP doc:")
        print(f"    keys={sorted(k for k in s if not k.startswith('_'))}  "
              f"vendor={_sap_vendor(s)!r} date={_sap_datevalue(s)!r}")

    sap_index: dict = {}
    for d in sap_docs:
        sap_index[(_sap_vendor(d), _sap_datevalue(d))] = d

    files = [d for d in email_docs
             if (not email_id or cmap._s(d.get("id")) == email_id)
             and (not vendor_filter or vendor_filter.upper() == "ALL"
                  or cmap._s(d.get("vendorId")) == vendor_filter)]

    all_rows: list[dict] = []
    grand: dict[str, int] = {}
    for doc in files:
        filename, rows, counts = _rows_for_file(doc, vendor_filter, sap_index, all_sap)
        all_rows.extend(rows)
        tally = {k: v for k, v in counts.items() if not k.startswith("_")}
        for k, v in tally.items():
            grand[k] = grand.get(k, 0) + v
        print(f"  {filename}: {sum(tally.values())} rows  {tally}  "
              f"(sap_files={counts['_sap_files']}, skipped_completed={counts['_skipped_completed']})")

    out_path = pathlib.Path(out_opt) if out_opt else \
        pathlib.Path(f"recon_report_{datetime.now():%Y%m%d_%H%M%S}.csv")
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(all_rows)

    matches = sum(1 for r in all_rows if r["result"] == "MATCH")
    print(f"\n===== {len(all_rows)} comparisons: {matches} MATCH / {len(all_rows) - matches} ANOMALY =====")
    print(f"by classification: {grand}")
    print(f"CSV written -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
