"""Run the reconciliation against LOCAL fixtures — NO Cosmos. Test everything offline.

Reads fixtures/email_files.json + fixtures/sap_read.json and runs the EXACT same logic the
Cosmos runner uses (map_email_doc -> reconcile -> write-back -> summary), so testing here
tests the real pipeline. It writes back IN PLACE, like the real SAP write-back:

    fixtures/sap_read.json         - reconflag/remarks filled into the SAP transactions
    fixtures/summary_container.json - one summary doc per email file (id = file name)

    python run_fixtures.py            # process every fixture file
    python run_fixtures.py VENDOR_ID  # only that vendor's files

This never touches Cosmos and never imports run_cosmos_workflow — pure offline test.
"""
from __future__ import annotations

import json
import pathlib
import sys

from bank_reconciliation.schema import canonical_mapper as cmap       # noqa: E402
from bank_reconciliation.recon import report, sap_writeback           # noqa: E402
from bank_reconciliation.triage import enrich_anomalies, run_summary  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent
FIX = ROOT / "fixtures"


def _sap_datevalue(doc: dict) -> str | None:
    return ((doc.get("datetime") or {}).get("datetimelist") or {}).get("datevalue")


def _process(doc: dict, vendor_filter: str | None, sap_index: dict) -> tuple[str, dict]:
    """Reconcile ONE email file vs its paired SAP doc; stamp the SAP doc; return (outcome, summary)."""
    filename, vendor, upload, txns = cmap.map_email_doc(doc, vendor_filter)
    active = [t for t in txns if (t.status or "").lower() != "completed"]     # skip Completed
    skipped = len(txns) - len(active)
    date = upload.isoformat() if upload else ""
    # one email file may span several dates; SAP splits into one file per date -> pair to all
    file_dvs = {d for t in active for d in (
        [t.txn_date.isoformat().replace("-", "")] if t.txn_date else []) + (
        [t.settlement_date.isoformat().replace("-", "")] if t.settlement_date else [])}
    paired = [d for (v, dv), d in sap_index.items() if v == vendor and dv in file_dvs]
    sap_txns = cmap.map_sap_read(paired)

    print(f"\n===== FILE {filename}  vendor={vendor}  uploadDate={date}  dates={sorted(file_dvs)}  "
          f"txns={len(txns)} active={len(active)} skipped(Completed)={skipped}  "
          f"sap_docs={len(paired)} sap_txns={len(sap_txns)} =====")
    run_id = report.run_id_for(vendor)
    rows, snap, meta = report.reconcile_and_build(active, sap_txns, vendor, run_id)
    report.print_summary(rows, snap, meta, vendor)

    enrich_anomalies(rows)                          # AI only if TRIAGE_ENABLED; else deterministic
    summary_text = run_summary.summarize(rows, snap, vendor, date)

    filled = sum(sap_writeback.fill_sap_read(sd, rows)[1] for sd in paired)   # stamp SAP docs in place
    print(f"write-back: reconflag+remarks stamped into {filled} SAP txn(s) (written back to sap_read.json)")

    summary = sap_writeback.build_summary(vendor, run_id, date=date,
                                          summary_text=summary_text, filename=filename)
    return "processed", summary


def main() -> int:
    vendor_filter = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    email_docs = json.loads((FIX / "email_files.json").read_text())
    sap_docs = json.loads((FIX / "sap_read.json").read_text())
    print(f"FIXTURE RUN (no Cosmos)  vendor={vendor_filter or 'ALL'}")
    print(f"  email files: {len(email_docs)}   SAP docs: {len(sap_docs)}")

    sap_index = {(cmap._s(d.get("vendorid")), _sap_datevalue(d)): d for d in sap_docs}
    files = [d for d in email_docs
             if not vendor_filter or vendor_filter.upper() == "ALL"
             or cmap._s(d.get("vendorId")) == vendor_filter]

    summaries, tally = [], {}
    for doc in files:
        outcome, summary = _process(doc, vendor_filter, sap_index)
        summaries.append(summary)
        tally[outcome] = tally.get(outcome, 0) + 1

    # write reconflag/remarks BACK into the input SAP file (in place), like the real write-back
    (FIX / "sap_read.json").write_text(json.dumps(sap_docs, indent=2, default=str) + "\n")
    (FIX / "summary_container.json").write_text(json.dumps(summaries, indent=2, default=str) + "\n")
    print(f"\n===== DONE. files -> {tally} =====")
    print("wrote reconflag/remarks back into fixtures/sap_read.json (in place)")
    print("wrote summaries to fixtures/summary_container.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
