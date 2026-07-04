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
from collections import defaultdict

from run_cosmos_workflow import _query                       # noqa: E402
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


def _scope_sap(file_group: list, sap_all: list, all_sap: bool) -> list:
    """Mirror run_cosmos_workflow: SAP scoped to this file's txn dates unless --all-sap."""
    if all_sap:
        return sap_all
    active = [t for t in file_group if (t.status or "").lower() != "completed"]
    it_date = {t.txn_date.isoformat() for t in active if t.txn_date}
    return [t for t in sap_all if t.txn_date and t.txn_date.isoformat() in it_date]


def main() -> int:
    args = sys.argv[1:]
    all_sap = "--all-sap" in args
    vendor = next((a for a in args if not a.startswith("-")), "MBANK")
    print(f"READ-ONLY unmatched-ref probe — VENDOR={vendor}  SOURCE_MODE={settings.source_mode}"
          f"  all_sap={all_sap}\n(this writes NOTHING)\n")

    file_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    sap_docs = _query(settings.cosmos_sap_container,
                      "SELECT * FROM c WHERE c.vendorid=@v", [{"name": "@v", "value": vendor}])
    print(f"email-source '{settings.cosmos_file_container}': {len(file_docs)} docs")
    print(f"SAP          '{settings.cosmos_sap_container}': {len(sap_docs)} docs (vendor={vendor})")
    if not file_docs and not sap_docs:
        print("  (nothing to compare)")
        return 1

    file_txns = cmap.map_bank_file(file_docs, vendor)
    sap_all = cmap.map_sap_txns(sap_docs)

    groups: dict[str, list] = defaultdict(list)
    for t in file_txns:
        groups[t.upload_date.isoformat() if t.upload_date else "(no Upload_Date)"].append(t)

    total_no_sap = total_no_file = 0
    for date in sorted(groups):
        file_group = groups[date]
        sap_txns = _scope_sap(file_group, sap_all, all_sap)
        rows = reconcile(file_group, sap_txns)
        print(f"\n===== FILE {vendor}:{date}  file_rows={len(file_group)}  sap_compared={len(sap_txns)} =====")
        total_no_sap += _print_side(rows, MatchType.MISSING_IN_SAP,
                                     "file_amount", "date_source", "in EMAIL but NOT in SAP (missing_in_sap)")
        total_no_file += _print_side(rows, MatchType.MISSING_IN_FILE,
                                     "sap_amount", "date_sap", "in SAP but NOT in EMAIL (missing_in_file)")

    print("\n" + "=" * 60)
    print(f"TOTAL unmatched — missing_in_sap: {total_no_sap}   missing_in_file: {total_no_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
