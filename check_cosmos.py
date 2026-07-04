"""READ-ONLY pre-flight check for the Cosmos reconciliation workflow.

Run this FIRST on the company laptop. It connects to Cosmos, prints the live column
names of the bank-file and SAP containers, checks them against what the mapper expects,
and lists the per-file split (one file = one Upload_Date). It WRITES NOTHING.

    python check_cosmos.py MBANK

Exit code 0 = looks good; 1 = a critical mapper field is missing or many rows are invalid.
Once this looks right, run the real workflow: `python run_cosmos_workflow.py MBANK`.
"""
from __future__ import annotations

import sys
from collections import defaultdict

# reuse the exact Cosmos helpers + mapper the real runner uses (no duplication)
from run_cosmos_workflow import _cols, _query           # noqa: E402
from bank_reconciliation.config import settings          # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap  # noqa: E402

# Fields the mapper reads. (critical = needed to pass the 4-field validation)
FILE_FIELDS = {
    "Partner_Trn_Reference_No": "ref key", "Payment_Ref_No": "ref key",
    "DEWA_Trn_Reference_No": "ref key", "Trn_Amount": "critical",
    "Trn_Date": "critical", "Type": "critical",
    "Settlement_Date": "info", "Upload_Date": "file id", "Status": "info", "Details": "info",
}
SAP_FIELDS = {
    "vendorid": "info", "transactiondate": "critical", "partnertransactionid": "ref key",
    "paymentreferencenumber": "ref key", "dewatransactionid": "ref key",
    "amount": "critical", "status": "info", "reconflag": "write target", "remarks": "write target",
}


def _check_fields(actual: list[str], expected: dict[str, str], label: str) -> bool:
    """Print a present/missing table. Returns False if a 'critical' field is missing."""
    cols = set(actual)
    print(f"\n{label} — mapper field check:")
    print(f"  {'field':<28}{'kind':<14}present?")
    ok = True
    for field, kind in expected.items():
        present = field in cols
        mark = "✓" if present else "✗"
        if not present and kind == "critical":
            ok = False
        print(f"  {field:<28}{kind:<14}{mark}")
    ref_keys = [f for f, k in expected.items() if k == "ref key"]
    if ref_keys and not any(f in cols for f in ref_keys):
        print(f"  ⚠ NONE of the ref-key fields {ref_keys} are present — rows can't join.")
        ok = False
    extra = sorted(cols - set(expected) - {c for c in cols if c.startswith("_")})
    if extra:
        print(f"  (columns in Cosmos the mapper ignores: {extra})")
    return ok


def _list_files(file_docs: list[dict], vendor: str) -> int:
    """Group mapped file rows by Upload_Date and print one line per file. Returns invalid count."""
    txns = cmap.map_bank_file(file_docs, vendor)
    groups: dict[str, list] = defaultdict(list)
    for t in txns:
        groups[t.upload_date.isoformat() if t.upload_date else "(no Upload_Date)"].append(t)

    print(f"\nper-file split (one file = one Upload_Date) — {len(groups)} file(s):")
    print(f"  {'Upload_Date':<20}{'rows':>6}{'invalid':>9}   txn-date range")
    total_invalid = 0
    for date in sorted(groups):
        rows = groups[date]
        invalid = [t for t in rows if not t.valid]
        total_invalid += len(invalid)
        dates = sorted({t.txn_date.isoformat() for t in rows if t.txn_date})
        span = f"{dates[0]} … {dates[-1]}" if dates else "(no txn dates)"
        print(f"  {date:<20}{len(rows):>6}{len(invalid):>9}   {span}")

    if total_invalid:
        reasons: dict[str, int] = {}
        for t in txns:
            if not t.valid and t.invalid_reason:
                reasons[t.invalid_reason] = reasons.get(t.invalid_reason, 0) + 1
        print(f"\n  invalid_record reasons: {reasons}")
    return total_invalid


def main() -> int:
    vendor = next((a for a in sys.argv[1:] if not a.startswith("-")), "MBANK")
    print(f"READ-ONLY pre-flight — VENDOR={vendor}  SOURCE_MODE={settings.source_mode}")
    print("(this writes NOTHING)\n")

    file_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    print(f"file container '{settings.cosmos_file_container}': {len(file_docs)} docs")
    print(f"  live columns: {_cols(file_docs)}")

    sap_docs = _query(settings.cosmos_sap_container,
                      "SELECT * FROM c WHERE c.vendorid=@v", [{"name": "@v", "value": vendor}])
    if not sap_docs:
        print(f"  (no SAP rows for vendorid={vendor}; reading all SAP docs to show columns)")
        sap_docs = _query(settings.cosmos_sap_container, "SELECT * FROM c", [])
    print(f"SAP  container '{settings.cosmos_sap_container}': {len(sap_docs)} docs")
    print(f"  live columns: {_cols(sap_docs)}")

    file_ok = _check_fields(_cols(file_docs), FILE_FIELDS, "BANK FILE")
    sap_ok = _check_fields(_cols(sap_docs), SAP_FIELDS, "SAP")

    invalid = _list_files(file_docs, vendor) if file_docs else 0

    print("\n" + "=" * 60)
    problems = []
    if not file_ok:
        problems.append("bank-file critical/ref field missing")
    if not sap_ok:
        problems.append("SAP critical field missing")
    if file_docs and invalid / max(len(file_docs), 1) > 0.2:
        problems.append(f"{invalid}/{len(file_docs)} rows invalid_record (>20%)")
    if problems:
        print("⚠ NOT READY — fix these before enabling writes:")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✓ Looks good. Next: `python run_cosmos_workflow.py " + vendor + "` (still read-only),")
    print("  then add --write-sap --write-summary once the reconciliation looks right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
