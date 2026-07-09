"""READ-ONLY pre-flight check for the Cosmos reconciliation workflow (NEW nested format).

Run this FIRST on the company laptop. It connects to Cosmos, flattens the nested email
files (transactions[]) and SAP READ docs (transaction[]), checks the live field names
against what the mapper expects, and lists the per-file split. It WRITES NOTHING.

    python check_cosmos.py [VENDOR_ID]

Exit code 0 = looks good; 1 = a critical mapper field is missing or many rows are invalid.
Once this looks right, run the real workflow: `python run_cosmos_workflow.py`.
"""
from __future__ import annotations

import sys

# reuse the exact Cosmos helpers + mapper the real runner uses (no duplication)
from run_cosmos_workflow import _query                    # noqa: E402
from bank_reconciliation.config import settings           # noqa: E402
from bank_reconciliation.schema import canonical_mapper as cmap  # noqa: E402
import check_common as cc                                 # noqa: E402

# OLD flat FILE_FIELDS: Partner_Trn_Reference_No / Payment_Ref_No / DEWATrn_Reference_No / Trn_* ...
# NEW nested email transaction fields (camelCase); uploadDate is injected from the file top level.
FILE_FIELDS = {
    "partnerTrnReferenceNo": "ref key", "paymentRefNo": "ref key",
    "dewaTrnReferenceNo": "ref key", "trnAmount": "critical", "trnDate": "critical",
    "settlementDate": "info", "uploadDate": "file id", "status": "info",
    "type": "info", "sourceChannel": "info",
}
SAP_FIELDS = {
    "vendorid": "info", "transactiondate": "critical", "partnertransactionid": "ref key",
    "paymentreferencenumber": "ref key", "dewatransactionid": "ref key",
    "amount": "critical", "status": "info", "reconflag": "write target", "remarks": "write target",
}


def _keys(rows: list[dict]) -> set[str]:
    """Union of non-system keys across ALL rows (schemaless-safe)."""
    keys: set[str] = set()
    for r in rows:
        keys.update(k for k in r if not k.startswith("_"))
    return keys


def _check_fields(cols: set[str], expected: dict[str, str], label: str) -> bool:
    """Print a present/missing table. Returns False if a 'critical' field is missing."""
    print(f"\n{label} — mapper field check:")
    print(f"  {'field':<28}{'kind':<14}present?")
    ok = True
    for field, kind in expected.items():
        present = field in cols
        if not present and kind == "critical":
            ok = False
        print(f"  {field:<28}{kind:<14}{'✓' if present else '✗'}")
    ref_keys = [f for f, k in expected.items() if k == "ref key"]
    if ref_keys and not any(f in cols for f in ref_keys):
        print(f"  ⚠ NONE of the ref-key fields {ref_keys} are present — rows can't join.")
        ok = False
    extra = sorted(cols - set(expected))
    if extra:
        print(f"  (fields in Cosmos the mapper ignores: {extra})")
    return ok


def _list_files(file_docs: list[dict], vendor: str | None) -> tuple[int, int]:
    """One line per email FILE (one doc). Returns (total_txns, total_invalid)."""
    print(f"\nper-file split (one doc = one file) — {len(file_docs)} file(s):")
    print(f"  {'file (id)':<40}{'upload':<12}{'txns':>5}{'active':>7}{'invalid':>8}   txn-date range")
    total_txns = total_invalid = 0
    reasons: dict[str, int] = {}
    for doc in file_docs:
        filename, _v, upload, txns = cmap.map_email_doc(doc, vendor)
        invalid = [t for t in txns if not t.valid]
        active = sum(1 for t in txns if (t.status or "").lower() != "completed")
        total_txns += len(txns)
        total_invalid += len(invalid)
        for t in invalid:
            if t.invalid_reason:
                reasons[t.invalid_reason] = reasons.get(t.invalid_reason, 0) + 1
        dts = sorted({t.txn_date.isoformat() for t in txns if t.txn_date})
        span = f"{dts[0]} … {dts[-1]}" if dts else "(no txn dates)"
        up = upload.isoformat() if upload else "(none)"
        print(f"  {filename[:38]:<40}{up:<12}{len(txns):>5}{active:>7}{len(invalid):>8}   {span}")
    if reasons:
        print(f"\n  invalid_record reasons: {reasons}")
    return total_txns, total_invalid


def main() -> int:
    vendor = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    print(f"READ-ONLY pre-flight — VENDOR={vendor or 'ALL'}  SOURCE_MODE={settings.source_mode}")
    print("(this writes NOTHING)\n")

    file_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    sap_docs = _query(settings.cosmos_sap_container, "SELECT * FROM c", [])
    file_rows = cc.email_txn_rows(file_docs)
    sap_rows = cc.sap_txn_rows(sap_docs)
    print(f"file container '{settings.cosmos_file_container}': {len(file_docs)} file(s) -> {len(file_rows)} txns")
    print(f"  live fields: {sorted(_keys(file_rows))}")
    print(f"SAP  container '{settings.cosmos_sap_container}': {len(sap_docs)} doc(s) -> {len(sap_rows)} txns")
    print(f"  live fields: {sorted(_keys(sap_rows))}")

    file_ok = _check_fields(_keys(file_rows), FILE_FIELDS, "BANK FILE")
    sap_ok = _check_fields(_keys(sap_rows), SAP_FIELDS, "SAP")

    total_txns, invalid = _list_files(file_docs, vendor) if file_docs else (0, 0)

    print("\n" + "=" * 60)
    problems = []
    if not file_ok:
        problems.append("bank-file critical/ref field missing")
    if not sap_ok:
        problems.append("SAP critical field missing")
    if total_txns and invalid / total_txns > 0.2:
        problems.append(f"{invalid}/{total_txns} txns invalid_record (>20%)")
    if problems:
        print("⚠ NOT READY — fix these before enabling writes:")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✓ Looks good. Next: `python run_cosmos_workflow.py` (still read-only),")
    print("  then add --write-sap --write-summary once the reconciliation looks right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
