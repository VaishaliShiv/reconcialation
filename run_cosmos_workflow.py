"""Full Cosmos reconciliation workflow — run from the DEWA network.

Reads the bank file + SAP from Cosmos, runs the deterministic compare, (optionally, when
TRIAGE_ENABLED=true) adds AI explanation + AI run-summary text, (optionally) writes
reconflag/remarks back into the SAP docs, and (optionally) upserts the run summary doc.

    python run_cosmos_workflow.py VENDOR [DATE] [flags]

DATE (optional): email/upload day 'YYYY-MM-DD'. Used as the summary id (VENDOR:DATE) and
the idempotency guard. If omitted, it is derived from the file rows' Upload_Date.

Flags:
    --write-sap       upsert reconflag + remarks into the SAP docs (bank-sap-source)
    --write-summary   upsert the summary doc to COSMOS_RESULTS_CONTAINER  (written LAST)
    --dry-run         force a NO-WRITE run even if WRITE_SAP/WRITE_SUMMARY are on in .env
    --all-sap         don't date-scope SAP; use every SAP row for the vendor
    --force           ignore the "summary already exists" idempotency guard

Writes turn on EITHER via these flags OR by setting WRITE_SAP=true / WRITE_SUMMARY=true in
.env (so the plain `python run_cosmos_workflow.py VENDOR` writes by itself). If BOTH are
false/unset and no flag is passed, the run is a READ-ONLY dry run (writes NOTHING). Use
--dry-run any time you want to preview safely regardless of the .env defaults.

SAFE FIRST TEST: run with NO flags. It prints the live column names of both containers
and the full reconciliation, and writes nothing — so you can confirm the field names and
classifications look right BEFORE enabling any write.

NOTE: --write-sap assumes the SAP write is idempotent (writing the same reconflag twice
is harmless). Confirm with the SAP team before using it in anger.
"""
from __future__ import annotations

import json
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bank_reconciliation.config import settings                      # noqa: E402
from bank_reconciliation.recon import report, sap_writeback, summary_store  # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap       # noqa: E402
from bank_reconciliation.triage import enrich_anomalies, run_summary  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent


def _cosmos_container(name: str):
    from azure.cosmos import CosmosClient  # lazy import
    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    return client.get_database_client(settings.cosmos_database).get_container_client(name)


def _query(container_name: str, sql: str, params: list[dict]) -> list[dict]:
    return list(_cosmos_container(container_name).query_items(
        query=sql, parameters=params, enable_cross_partition_query=True, max_item_count=1000))


def _cols(docs: list[dict]) -> list[str]:
    return sorted(k for k in docs[0] if not k.startswith("_")) if docs else []


def _summary_exists(vendor: str, date: str) -> bool:
    try:
        _cosmos_container(settings.cosmos_results_container).read_item(
            item=f"{vendor}:{date}", partition_key=vendor)
        return True
    except Exception:  # noqa: BLE001
        return False


def _write_sap_reconflag(rows: list[dict], sap_docs: list[dict]) -> int:
    """Upsert reconflag + remarks into each SAP doc, matched on partner/payment/dewa id."""
    lookup = sap_writeback._key_to_flag_remark(rows)
    cont = _cosmos_container(settings.cosmos_sap_container)
    n = 0
    for doc in sap_docs:
        key = (cmap._norm_key(doc.get("partnertransactionid"))
               or cmap._norm_key(doc.get("paymentreferencenumber"))
               or cmap._norm_key(doc.get("dewatransactionid")))
        hit = lookup.get(key)
        if not hit or not doc.get("id"):
            continue
        clean = {k: v for k, v in doc.items() if not k.startswith("_")}
        clean["reconflag"], clean["remarks"] = hit
        cont.upsert_item(clean)
        n += 1
    return n


def _process_file(vendor: str, date: str, file_group: list, sap_txns_all: list,
                  sap_docs: list, flags: tuple[bool, bool, bool, bool]) -> str:
    """Reconcile ONE file (one Upload_Date) and, if opted-in, write its SAP flags + summary.

    Returns an outcome: 'processed' | 'skipped' (summary already exists) | 'error' (field drift).
    """
    write_sap, write_summary, all_sap, force = flags
    active = [t for t in file_group if (t.status or "").lower() != "completed"]
    it_date = sorted({t.txn_date.isoformat() for t in active if t.txn_date})
    sap_txns = (sap_txns_all if all_sap else
                [t for t in sap_txns_all if t.txn_date and t.txn_date.isoformat() in it_date])

    print(f"\n===== FILE {vendor}:{date or '(no upload_date)'}  "
          f"rows={len(file_group)}  sap_kept={len(sap_txns)}  IT_DATE={it_date} =====")
    run_id = report.run_id_for(vendor)
    rows, snap, meta = report.reconcile_and_build(file_group, sap_txns, vendor, run_id)
    report.print_summary(rows, snap, meta, vendor)

    invalid = sum(1 for r in rows if r["classification"] == "invalid_record")
    if rows and invalid / len(rows) > 0.2:
        print(f"⚠️  {invalid}/{len(rows)} rows invalid_record — likely a FIELD-NAME mismatch. "
              f"Skipping this file (no writes).")
        return "error"

    # triage (AI) — only if TRIAGE_ENABLED; deterministic fallback otherwise
    enrich_anomalies(rows)
    summary_text = run_summary.summarize(rows, snap, vendor, date)

    # per-file idempotency guard: skip if THIS file's summary already exists
    if write_summary and not force and date and _summary_exists(vendor, date):
        print(f"summary '{vendor}:{date}' already exists — skipping (use --force to reprocess).")
        return "skipped"

    # write reconflag/remarks back to SAP for this file's rows only (opt-in)
    if write_sap:
        n = _write_sap_reconflag(rows, sap_docs)
        print(f"SAP write-back: reconflag+remarks upserted into {n} SAP docs.")
    else:
        preview = sap_writeback.build_write_payload(rows, vendor, date)
        print(f"[DRY-RUN] SAP write-back would fill {len(preview['transaction'])} txns "
              f"(pass --write-sap to apply).")

    # summary doc — one per file, WRITTEN LAST so its existence means this file finished
    doc = sap_writeback.build_summary(vendor, run_id, date=date, summary_text=summary_text)
    if write_summary:
        print(f"summary upserted to '{settings.cosmos_results_container}': "
              f"id={summary_store.upsert(doc)}")
    else:
        print("[DRY-RUN] summary doc (pass --write-summary to upsert):")
        print(json.dumps(doc, indent=2, default=str, ensure_ascii=False))
    return "processed"


def main() -> int:
    args = sys.argv[1:]
    positional = [a for a in args if not a.startswith("-")]
    vendor = positional[0] if positional else "MBANK"
    arg_date = positional[1] if len(positional) > 1 else ""
    # Writes turn on via .env (WRITE_SAP / WRITE_SUMMARY) OR the CLI flag; --dry-run forces off.
    dry_run = "--dry-run" in args
    write_sap = (settings.write_sap or "--write-sap" in args) and not dry_run
    write_summary = (settings.write_summary or "--write-summary" in args) and not dry_run
    flags = (write_sap, write_summary, "--all-sap" in args, "--force" in args)

    print(f"VENDOR={vendor}  DATE={arg_date or '(all files in container)'}  "
          f"triage={'on' if settings.triage_enabled else 'off'}  "
          f"write_sap={write_sap}  write_summary={write_summary}"
          f"{'  [--dry-run: writes forced OFF]' if dry_run else ''}\n")

    # read the whole email container once, map to canonical, then SPLIT INTO FILES
    file_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    print(f"file container '{settings.cosmos_file_container}': {len(file_docs)} docs")
    print(f"  columns: {_cols(file_docs)}")
    file_txns = cmap.map_bank_file(file_docs, vendor)

    # one "file" == one Upload_Date (one bank sends one file per day -> vendor:date is unique)
    groups: dict[str, list] = defaultdict(list)
    for t in file_txns:
        groups[t.upload_date.isoformat() if t.upload_date else ""].append(t)
    dates = sorted(groups)
    if arg_date:                        # a DATE argument = process only that one file
        dates = [d for d in dates if d == arg_date] or [arg_date]

    # read the vendor's SAP rows ONCE; each file scopes to its own txn dates
    sap_docs = _query(settings.cosmos_sap_container,
                      "SELECT * FROM c WHERE c.vendorid=@v", [{"name": "@v", "value": vendor}])
    if not sap_docs:
        print(f"  (no SAP rows for vendorid={vendor}; reading all SAP docs — verify the vendor field)")
        sap_docs = _query(settings.cosmos_sap_container, "SELECT * FROM c", [])
    print(f"SAP  container '{settings.cosmos_sap_container}': {len(sap_docs)} docs")
    print(f"  columns: {_cols(sap_docs)}")
    sap_txns_all = cmap.map_sap_txns(sap_docs)

    # process each file one-by-one; each gets its own reconcile + its own summary doc
    print(f"\nfound {len(dates)} file(s) for {vendor}: {dates}")
    tally: dict[str, int] = {}
    for date in dates:
        outcome = _process_file(vendor, date, groups.get(date, []),
                                sap_txns_all, sap_docs, flags)
        tally[outcome] = tally.get(outcome, 0) + 1
    print(f"\n===== DONE. files -> {tally} =====")
    return 1 if tally.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
