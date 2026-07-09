"""READ-ONLY reference-number search across both containers. Writes NOTHING.

Give it a reference value (e.g. a paymentreferencenumber from SAP) and it searches EVERY
column of both the email-source and SAP containers, reporting which column(s) hold that
value on each side. Use it to confirm whether a SAP ref also exists in the email file and,
if so, which columns should join.

    python find_ref.py 2041141921
    python find_ref.py 2041141921 MBANK      # optional vendor to scope the SAP query

Matching is normalized (whitespace + trailing '.0' stripped) so formatting drift still hits.
"""
from __future__ import annotations

import sys

from run_cosmos_workflow import _query                       # noqa: E402
from bank_reconciliation.config import settings              # noqa: E402
from bank_reconciliation.schema.canonical_mapper import _norm_key  # noqa: E402


def _hits(docs: list[dict], target: str) -> list[tuple[str, str, str]]:
    """Return (column, raw_value, doc_id) for every cell whose normalized value == target."""
    out = []
    for d in docs:
        did = str(d.get("id", "?"))
        for col, val in d.items():
            if col.startswith("_"):
                continue
            if _norm_key(val) == target:
                out.append((col, str(val), did))
    return out


def _report(label: str, docs: list[dict], target: str) -> set[str]:
    hits = _hits(docs, target)
    cols = sorted({c for c, _, _ in hits})
    print(f"\n{label}: {len(docs)} docs — {len(hits)} cell(s) match, in column(s): {cols or 'NONE'}")
    for col, raw, did in hits[:10]:
        print(f"    column={col:<26} value={raw!r:<24} doc.id={did}")
    if len(hits) > 10:
        print(f"    … (+{len(hits) - 10} more)")
    return set(cols)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print("usage: python find_ref.py <reference-value> [VENDOR]")
        return 2
    raw_target = args[0]
    vendor = args[1] if len(args) > 1 else None
    target = _norm_key(raw_target)
    print(f"READ-ONLY ref search — value={raw_target!r} (normalized {target!r})  "
          f"SOURCE_MODE={settings.source_mode}\n(this writes NOTHING)")

    email_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    if vendor:
        sap_docs = _query(settings.cosmos_sap_container,
                          "SELECT * FROM c WHERE c.vendorid=@v", [{"name": "@v", "value": vendor}])
    else:
        sap_docs = _query(settings.cosmos_sap_container, "SELECT * FROM c", [])

    email_cols = _report(f"EMAIL SOURCE '{settings.cosmos_file_container}'", email_docs, target)
    sap_cols = _report(f"SAP '{settings.cosmos_sap_container}'", sap_docs, target)

    print("\n" + "=" * 60)
    if email_cols and sap_cols:
        print(f"✓ present on BOTH sides — email column(s) {sorted(email_cols)} "
              f"↔ SAP column(s) {sorted(sap_cols)}.")
        print("  => the matcher should join on this pair. If those columns differ from the")
        print("     partner→payment→DEWA precedence it's using, that's why it isn't matching.")
    elif sap_cols and not email_cols:
        print("⚠ found in SAP but NOT in the email source — this ref is genuinely missing_in_file.")
    elif email_cols and not sap_cols:
        print("⚠ found in the email source but NOT in SAP — this ref is genuinely missing_in_sap.")
    else:
        print("✗ not found on either side — check the value / whether the file is loaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
