"""Full Cosmos reconciliation workflow — run from the DEWA network.

Reads the bank file + SAP from Cosmos, runs the deterministic compare, (optionally, when
TRIAGE_ENABLED=true) adds AI explanation + AI run-summary text, (optionally) writes
reconflag/remarks back into the SAP docs, and (optionally) upserts the run summary doc.

    python run_cosmos_workflow.py [VENDOR_ID | ALL] [flags]

NEW nested format: each email doc is one FILE (top-level vendorId/uploadDate/id + a
transactions[] array). It is paired to the SAP READ doc (transaction[] array) by
(vendorId, uploadDate=datevalue). The summary id IS the file name (the email doc's id).
With no arg (or ALL) every email file is processed; a VENDOR_ID limits to that vendor.
Email transactions with status 'Completed' are skipped; 'In Progress'/blank are processed.

TARGETED TEST — one email file vs the SAP file(s) YOU choose. The email file is named by its
own id (--email-id); the SAP file(s) are named by vendorid + date, so you pick them with the
VENDOR_ID arg + --dates (YYYYMMDD), never by SAP id:
    # one email file  ->  ONE sap file (vendor nbd1Qmid, date 20260629)
    python run_cosmos_workflow.py nbd1Qmid --email-id file-001-20260629 --dates 20260629 --all-sap --dry-run
    # one email file  ->  MULTIPLE sap files (two dates, same vendor)
    python run_cosmos_workflow.py nbd1Qmid --email-id file-001-20260629 --dates 20260629,20260630 --all-sap --dry-run
    # let the code pick the SAP file(s) automatically from the email file's own dates
    python run_cosmos_workflow.py nbd1Qmid --email-id file-001-20260629 --dry-run

Flags:
    --email-id ID     process ONLY the email doc whose id == ID (default: every email file)
    --dates D1,D2     restrict SAP to these datevalues (YYYYMMDD) for the vendor. The SAP file
                      name IS vendorid+date, so this is how you name the SAP file(s). With
                      --all-sap the email file is compared to EXACTLY these SAP files; without
                      it they still must overlap the email file's own dates. Default: all SAP.
    --write-sap       upsert reconflag + remarks into the SAP docs (bank-sap-source)
    --write-summary   upsert the summary doc to COSMOS_RESULTS_CONTAINER  (written LAST)
    --dry-run         force a NO-WRITE run even if WRITE_SAP/WRITE_SUMMARY are on in .env
    --all-sap         don't date-scope SAP; use every SAP file loaded for the vendor
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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from bank_reconciliation.config import settings                      # noqa: E402
from bank_reconciliation.recon import report, sap_writeback, summary_store  # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap       # noqa: E402
from bank_reconciliation.triage import enrich_anomalies, run_summary  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "fixtures"


def _opt(args: list[str], key: str) -> str | None:
    """Read `--key value` or `--key=value` from argv (None if absent)."""
    for i, a in enumerate(args):
        if a == key and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(key + "="):
            return a.split("=", 1)[1]
    return None


def _positionals(args: list[str], consumes: set[str]) -> list[str]:
    """Positional args, skipping flags and the value that follows a `--key value` option."""
    out, skip = [], False
    for a in args:
        if skip:                       # this token is the value of the previous --key
            skip = False
            continue
        if a in consumes:              # `--key value` form: consume its value next
            skip = True
            continue
        if a.startswith("-"):          # a flag or `--key=value` form
            continue
        out.append(a)
    return out


def _cosmos_container(name: str):
    from azure.cosmos import CosmosClient  # lazy import
    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    return client.get_database_client(settings.cosmos_database).get_container_client(name)


def _fixture_docs(container_name: str) -> list[dict]:
    """SOURCE_MODE=fixture: read a container's docs from local fixtures/ (no Cosmos)."""
    fmap = {settings.cosmos_file_container: "email_files.json",
            settings.cosmos_sap_container: "sap_read.json"}
    name = fmap.get(container_name)
    path = FIXTURE_DIR / name if name else None
    return json.loads(path.read_text()) if path and path.exists() else []


def _query(container_name: str, sql: str, params: list[dict]) -> list[dict]:
    if settings.source_mode == "fixture":            # offline test: local JSON, no Cosmos
        return _fixture_docs(container_name)
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


# ============================================================================
# OLD FORMAT (flat email rows grouped by Upload_Date; flat SAP docs) — REPLACED
# by the nested per-file flow below. Kept commented for reference / rollback.
# ============================================================================
# def _write_sap_reconflag(rows: list[dict], sap_docs: list[dict]) -> int:
#     """Upsert reconflag + remarks into each SAP doc, matched on partner/payment/dewa id."""
#     lookup = sap_writeback._key_to_flag_remark(rows)
#     cont = _cosmos_container(settings.cosmos_sap_container)
#     n = 0
#     for doc in sap_docs:
#         key = (cmap._norm_key(doc.get("partnertransactionid"))
#                or cmap._norm_key(doc.get("paymentreferencenumber"))
#                or cmap._norm_key(doc.get("dewatransactionid")))
#         hit = lookup.get(key)
#         if not hit or not doc.get("id"):
#             continue
#         clean = {k: v for k, v in doc.items() if not k.startswith("_")}
#         clean["reconflag"], clean["remarks"] = hit
#         cont.upsert_item(clean)
#         n += 1
#     return n
#
#
# def _process_file(vendor, date, file_group, sap_txns_all, sap_docs, flags):
#     """Reconcile ONE file (one Upload_Date) and, if opted-in, write SAP flags + summary."""
#     write_sap, write_summary, all_sap, force = flags
#     active = [t for t in file_group if (t.status or "").lower() != "completed"]
#     it_date = sorted({t.txn_date.isoformat() for t in active if t.txn_date})
#     sap_txns = (sap_txns_all if all_sap else
#                 [t for t in sap_txns_all if t.txn_date and t.txn_date.isoformat() in it_date])
#     run_id = report.run_id_for(vendor)
#     rows, snap, meta = report.reconcile_and_build(file_group, sap_txns, vendor, run_id)
#     report.print_summary(rows, snap, meta, vendor)
#     invalid = sum(1 for r in rows if r["classification"] == "invalid_record")
#     if rows and invalid / len(rows) > 0.2:
#         return "error"
#     enrich_anomalies(rows)
#     summary_text = run_summary.summarize(rows, snap, vendor, date)
#     if write_summary and not force and date and _summary_exists(vendor, date):
#         return "skipped"
#     if write_sap:
#         n = _write_sap_reconflag(rows, sap_docs)
#     doc = sap_writeback.build_summary(vendor, run_id, date=date, summary_text=summary_text)
#     if write_summary:
#         summary_store.upsert(doc)
#     return "processed"
# ============================================================================


# ============================================================================
# NEW FORMAT — one email doc = one FILE (nested transactions[]); pair to the
# SAP READ doc (nested transaction[]) by (vendorId, uploadDate=datevalue).
# ============================================================================
def _sap_datevalue(doc: dict) -> str | None:
    """SAP READ doc -> its datevalue (YYYYMMDD) at datetime.datetimelist.datevalue."""
    return ((doc.get("datetime") or {}).get("datetimelist") or {}).get("datevalue")


def _file_datevalues(txns: list) -> set[str]:
    """All YYYYMMDD dates present in a file's transactions (txn + settlement dates).

    An email file may span multiple dates; SAP splits those into one file per date, so we
    pair against every SAP file whose datevalue matches one of these.
    """
    dvs: set[str] = set()
    for t in txns:
        if t.txn_date:
            dvs.add(t.txn_date.isoformat().replace("-", ""))
        if t.settlement_date:
            dvs.add(t.settlement_date.isoformat().replace("-", ""))
    return dvs


def _summary_exists_id(doc_id: str, vendor: str) -> bool:
    if settings.source_mode == "fixture":            # no idempotency guard offline
        return False
    try:
        _cosmos_container(settings.cosmos_results_container).read_item(
            item=doc_id, partition_key=vendor)
        return True
    except Exception:  # noqa: BLE001
        return False


def _write_sap_doc(sap_doc: dict, rows: list[dict]) -> int:
    """Fill reconflag+remarks into a SAP READ doc's transaction[] (in place). Upsert to
    Cosmos in cosmos mode; in fixture mode the mutation is persisted to sap_read.json by main()."""
    filled, n = sap_writeback.fill_sap_read(sap_doc, rows)
    if n and filled.get("id") and settings.source_mode != "fixture":
        clean = {k: v for k, v in filled.items() if not k.startswith("_")}
        _cosmos_container(settings.cosmos_sap_container).upsert_item(clean)
    return n


def _process_email_file(doc: dict, vendor_filter: str | None, sap_index: dict,
                        flags: tuple[bool, bool, bool, bool]) -> tuple[str, dict | None]:
    """Reconcile ONE email FILE vs its paired SAP READ doc; opt-in write-back + summary.

    Returns (outcome, summary_doc). In fixture mode the SAP mutation + summary are persisted
    to local files by main(); in cosmos mode they are upserted here.
    """
    write_sap, write_summary, all_sap, force = flags
    is_fixture = settings.source_mode == "fixture"
    filename, vendor, upload, txns = cmap.map_email_doc(doc, vendor_filter)
    active = [t for t in txns if (t.status or "").lower() != "completed"]   # skip Completed
    skipped = len(txns) - len(active)
    date = upload.isoformat() if upload else ""

    # One email file can span several settlement/txn dates; SAP delivers ONE file per date.
    # Pair this email file to EVERY SAP doc whose date matches a date present in the file.
    file_dvs = _file_datevalues(active)
    if all_sap:                                       # every SAP doc for this vendor
        sap_docs = [d for (v, _dv), d in sap_index.items() if v == vendor]
    else:                                             # SAP docs covering this file's date(s)
        sap_docs = [d for (v, dv), d in sap_index.items() if v == vendor and dv in file_dvs]
    sap_txns = cmap.map_sap_read(sap_docs)

    print(f"\n===== FILE {filename}  vendor={vendor}  uploadDate={date}  dates={sorted(file_dvs)}  "
          f"txns={len(txns)} active={len(active)} skipped(Completed)={skipped}  "
          f"sap_docs={len(sap_docs)} sap_txns={len(sap_txns)} =====")
    run_id = report.run_id_for(vendor)
    rows, snap, meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)
    report.print_summary(rows, snap, meta, vendor)

    invalid = sum(1 for r in rows if r["classification"] == "invalid_record")
    if rows and invalid / len(rows) > 0.2:
        print(f"⚠️  {invalid}/{len(rows)} rows invalid_record — likely a FIELD-NAME mismatch. "
              f"Skipping this file (no writes).")
        return "error", None

    enrich_anomalies(rows)                            # AI only if TRIAGE_ENABLED; else deterministic
    summary_text = run_summary.summarize(rows, snap, vendor, date)

    if write_summary and not force and filename and _summary_exists_id(filename, vendor):
        print(f"summary '{filename}' already exists — skipping (use --force to reprocess).")
        return "skipped", None

    if write_sap:
        total = sum(_write_sap_doc(sd, rows) for sd in sap_docs)
        dest = "fixtures/sap_read.json" if is_fixture else "SAP docs"
        print(f"SAP write-back: reconflag+remarks written into {total} SAP txn(s) ({dest}).")
    else:
        preview = sap_writeback.build_write_payload(rows, vendor, date)
        print(f"[DRY-RUN] SAP write-back would fill {len(preview['transaction'])} txn(s) "
              f"(pass --write-sap to apply).")

    sdoc = sap_writeback.build_summary(vendor, run_id, date=date,
                                       summary_text=summary_text, filename=filename)
    if write_summary and is_fixture:
        print(f"summary (fixture) id={sdoc['id']} -> fixtures/summary_container.json")
    elif write_summary:
        print(f"summary upserted to '{settings.cosmos_results_container}': "
              f"id={summary_store.upsert(sdoc)}")
    else:
        print("[DRY-RUN] summary doc (pass --write-summary to upsert):")
        print(json.dumps(sdoc, indent=2, default=str, ensure_ascii=False))
    return "processed", sdoc


# ---- OLD FORMAT main() (flat rows grouped by Upload_Date) — commented for reference ----
# def main() -> int:
#     args = sys.argv[1:]
#     positional = [a for a in args if not a.startswith("-")]
#     vendor = positional[0] if positional else "MBANK"
#     arg_date = positional[1] if len(positional) > 1 else ""
#     dry_run = "--dry-run" in args
#     write_sap = (settings.write_sap or "--write-sap" in args) and not dry_run
#     write_summary = (settings.write_summary or "--write-summary" in args) and not dry_run
#     flags = (write_sap, write_summary, "--all-sap" in args, "--force" in args)
#     file_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
#     file_txns = cmap.map_bank_file(file_docs, vendor)
#     groups = defaultdict(list)
#     for t in file_txns:
#         groups[t.upload_date.isoformat() if t.upload_date else ""].append(t)
#     dates = sorted(groups)
#     if arg_date:
#         dates = [d for d in dates if d == arg_date] or [arg_date]
#     sap_docs = _query(settings.cosmos_sap_container,
#                       "SELECT * FROM c WHERE c.vendorid=@v", [{"name": "@v", "value": vendor}])
#     if not sap_docs:
#         sap_docs = _query(settings.cosmos_sap_container, "SELECT * FROM c", [])
#     sap_txns_all = cmap.map_sap_txns(sap_docs)
#     tally = {}
#     for date in dates:
#         outcome = _process_file(vendor, date, groups.get(date, []), sap_txns_all, sap_docs, flags)
#         tally[outcome] = tally.get(outcome, 0) + 1
#     return 1 if tally.get("error") else 0
# ------------------------------------------------------------------------------


def main() -> int:
    """NEW format: each email doc is a FILE; pair it to its SAP READ doc and reconcile.

        python run_cosmos_workflow.py [VENDOR_ID | ALL] [flags]

    With no arg (or ALL) every email file is processed; a VENDOR_ID processes only that
    vendor's files. Writes are opt-in (--write-sap/--write-summary or WRITE_* in .env;
    --dry-run forces no writes).
    """
    args = sys.argv[1:]
    email_id = _opt(args, "--email-id")            # name the ONE email file to test
    dates_opt = _opt(args, "--dates")              # name the SAP file(s) by their date(s)
    sap_dates = {d.strip() for d in dates_opt.split(",") if d.strip()} if dates_opt else None
    positional = _positionals(args, consumes={"--email-id", "--dates"})
    vendor_filter = positional[0] if positional else None
    dry_run = "--dry-run" in args
    write_sap = (settings.write_sap or "--write-sap" in args) and not dry_run
    write_summary = (settings.write_summary or "--write-summary" in args) and not dry_run
    flags = (write_sap, write_summary, "--all-sap" in args, "--force" in args)

    print(f"VENDOR_FILTER={vendor_filter or 'ALL'}  email_id={email_id or 'ALL'}  "
          f"sap_dates={sorted(sap_dates) if sap_dates else 'ALL'}  "
          f"triage={'on' if settings.triage_enabled else 'off'}  "
          f"write_sap={write_sap}  write_summary={write_summary}"
          f"{'  [--dry-run: writes forced OFF]' if dry_run else ''}\n")

    # Email side: pull ONLY the named file when --email-id is given, else every file.
    if email_id:
        email_docs = _query(settings.cosmos_file_container,
                            "SELECT * FROM c WHERE c.id=@id", [{"name": "@id", "value": email_id}])
        email_docs = [d for d in email_docs if cmap._s(d.get("id")) == email_id]  # fixture-safe
    else:
        email_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    sap_docs = _query(settings.cosmos_sap_container, "SELECT * FROM c", [])

    # SAP side: the SAP file name is vendorid+date, so scope to the requested vendor + date(s).
    if vendor_filter and vendor_filter.upper() != "ALL":
        sap_docs = [d for d in sap_docs if cmap._s(d.get("vendorid")) == vendor_filter]
    if sap_dates:
        sap_docs = [d for d in sap_docs if _sap_datevalue(d) in sap_dates]
    print(f"email files '{settings.cosmos_file_container}': {len(email_docs)} doc(s)")
    print(f"SAP  docs   '{settings.cosmos_sap_container}': {len(sap_docs)} doc(s)"
          f" (vendor+dates scoped)" if (vendor_filter or sap_dates) else "")

    # index each SAP READ doc by (vendorid, datevalue) so a file pairs to its SAP doc
    sap_index: dict = {}
    for d in sap_docs:
        sap_index[(cmap._s(d.get("vendorid")), _sap_datevalue(d))] = d

    files = [d for d in email_docs
             if not vendor_filter or vendor_filter.upper() == "ALL"
             or cmap._s(d.get("vendorId")) == vendor_filter]
    print(f"\nprocessing {len(files)} email file(s)")
    tally: dict[str, int] = {}
    summaries: list[dict] = []
    for doc in files:
        outcome, sdoc = _process_email_file(doc, vendor_filter, sap_index, flags)
        tally[outcome] = tally.get(outcome, 0) + 1
        if sdoc and write_summary:
            summaries.append(sdoc)

    # fixture mode: persist the in-place SAP mutations + summaries to local files (no Cosmos)
    if settings.source_mode == "fixture":
        if write_sap:
            (FIXTURE_DIR / "sap_read.json").write_text(json.dumps(sap_docs, indent=2, default=str) + "\n")
            print("\nwrote reconflag/remarks back into fixtures/sap_read.json (in place)")
        if write_summary and summaries:
            (FIXTURE_DIR / "summary_container.json").write_text(json.dumps(summaries, indent=2, default=str) + "\n")
            print("wrote summaries to fixtures/summary_container.json")

    print(f"\n===== DONE. files -> {tally} =====")
    return 1 if tally.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
