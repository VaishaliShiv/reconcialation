"""READ-ONLY reconcile probe — prints only the UNMATCHED reference numbers. Writes NOTHING.

Runs the exact deterministic matcher (dynamic_matcher.reconcile) against live Cosmos and
lists, per file (one file = one Upload_Date), the refs that did NOT match on each side:

  - missing_in_sap   : ref is in the EMAIL SOURCE but has no SAP row  ("Not posted in SAP")
  - missing_in_file  : ref is in SAP but not in the email source      ("Not in bank file")

    python check_unmatched.py MBANK            # SAP scoped per-file by txn date (like the real run)
    python check_unmatched.py MBANK --all-sap  # compare against ALL of the vendor's SAP rows

This is a diagnostic/test helper — it never writes reconflag, remarks, or summaries.
"""
from __future__ import annotations

import sys

from run_cosmos_workflow import _query, _sap_datevalue       # noqa: E402
from bank_reconciliation.config import settings              # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap  # noqa: E402
from bank_reconciliation.recon.dynamic_matcher import reconcile  # noqa: E402
from bank_reconciliation.models import MatchType             # noqa: E402


def _ref(r: dict) -> str:
    """Human-readable ref for an unmatched row (strip the 'kind:' prefix on the join key)."""
    ku = r.get("match_key_used") or ""
    return ku.split(":", 1)[1] if ":" in ku else (ku or "(no ref)")


def _print_side(rows: list[dict], mt: MatchType, amt_key: str, date_key: str, label: str) -> int:
    hits = [r for r in rows if r["match_type"] == mt]
    print(f"\n  {label}: {len(hits)}")
    for r in hits:
        amt = r.get(amt_key)
        dt = r.get(date_key)
        dt = dt.isoformat() if hasattr(dt, "isoformat") else (dt or "")
        print(f"    ref={_ref(r):<24} amount={amt!s:<12} date={dt}")
    return len(hits)


def main() -> int:
    args = sys.argv[1:]
    all_sap = "--all-sap" in args
    vendor_filter = next((a for a in args if not a.startswith("-")), None)
    print(f"READ-ONLY unmatched-ref probe — VENDOR={vendor_filter or 'ALL'}  "
          f"SOURCE_MODE={settings.source_mode}  all_sap={all_sap}\n(this writes NOTHING)\n")

    email_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    sap_docs = _query(settings.cosmos_sap_container, "SELECT * FROM c", [])
    print(f"email-source '{settings.cosmos_file_container}': {len(email_docs)} file(s)")
    print(f"SAP          '{settings.cosmos_sap_container}': {len(sap_docs)} doc(s)")
    if not email_docs and not sap_docs:
        print("  (nothing to compare)")
        return 1

    # index each SAP READ doc by (vendorid, datevalue) so a file pairs to its SAP doc
    sap_index: dict = {}
    for d in sap_docs:
        sap_index[(cmap._s(d.get("vendorid")), _sap_datevalue(d))] = d

    total_no_sap = total_no_file = 0
    for doc in email_docs:
        filename, vendor, upload, txns = cmap.map_email_doc(doc, vendor_filter)
        if vendor_filter and vendor_filter.upper() != "ALL" and vendor != vendor_filter:
            continue
        active = [t for t in txns if (t.status or "").lower() != "completed"]   # skip Completed
        datevalue = (upload.isoformat().replace("-", "")) if upload else ""
        if all_sap:
            paired = [d for (v, _dv), d in sap_index.items() if v == vendor]
        else:
            paired = [sap_index[(vendor, datevalue)]] if (vendor, datevalue) in sap_index else []
        sap_txns = cmap.map_sap_read(paired)
        rows = reconcile(active, sap_txns)
        print(f"\n===== FILE {filename}  vendor={vendor}  uploadDate={upload}  "
              f"active={len(active)}  sap_compared={len(sap_txns)} =====")
        total_no_sap += _print_side(rows, MatchType.MISSING_IN_SAP,
                                     "file_amount", "date_source", "in EMAIL but NOT in SAP (missing_in_sap)")
        total_no_file += _print_side(rows, MatchType.MISSING_IN_FILE,
                                     "sap_amount", "date_sap", "in SAP but NOT in EMAIL (missing_in_file)")

    print("\n" + "=" * 60)
    print(f"TOTAL unmatched — missing_in_sap: {total_no_sap}   missing_in_file: {total_no_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
