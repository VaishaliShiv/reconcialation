"""READ-ONLY join-key alignment audit. Writes NOTHING.

The matcher joins email<->SAP on a reference key (partner -> payment -> DEWA). If the two
sides store the same id in DIFFERENT columns, nothing matches. This tool compares EVERY
email ref-column against EVERY SAP ref-column across the whole dataset and prints how many
values overlap — so the true join pair is obvious (the cell with the high overlap).

    python check_keys.py MBANK

High overlap on a pair (e.g. email.Payment_Ref_No <-> SAP.paymentreferencenumber) = that's
the column pair the matcher should join on.
"""
from __future__ import annotations

import sys

from run_cosmos_workflow import _query                       # noqa: E402
from bank_reconciliation.config import settings              # noqa: E402
from bank_reconciliation.schema.canonical_mapper import _norm_key  # noqa: E402

# candidate reference/join columns on each side (the fields the matcher can key on)
EMAIL_KEYS = ["Partner_Trn_Reference_No", "Payment_Ref_No", "DEWATrn_Reference_No"]
SAP_KEYS = ["partnertransactionid", "paymentreferencenumber", "dewatransactionid"]


def _values(docs: list[dict], col: str) -> set[str]:
    """Distinct normalized non-null values of one column."""
    return {v for v in (_norm_key(d.get(col)) for d in docs) if v}


def _samples(docs: list[dict], col: str, n: int = 5) -> list[str]:
    """A few RAW (un-normalized) non-null values — to eyeball format drift."""
    out = []
    for d in docs:
        v = d.get(col)
        if v is not None and str(v).strip():
            out.append(repr(str(v)))
            if len(out) >= n:
                break
    return out


def _print_samples(email_docs: list[dict], sap_docs: list[dict]) -> None:
    print("\nsample RAW values (compare formats — prefixes, leading zeros, number vs text):")
    for c in EMAIL_KEYS:
        print(f"  email.{c:<26} {_samples(email_docs, c)}")
    for c in SAP_KEYS:
        print(f"  sap.{c:<28} {_samples(sap_docs, c)}")


def _matrix(email_docs: list[dict], sap_docs: list[dict]) -> None:
    ev = {c: _values(email_docs, c) for c in EMAIL_KEYS}
    sv = {c: _values(sap_docs, c) for c in SAP_KEYS}

    print("\ndistinct non-null values per column:")
    for c in EMAIL_KEYS:
        print(f"  email.{c:<26} {len(ev[c])}")
    for c in SAP_KEYS:
        print(f"  sap.{c:<28} {len(sv[c])}")

    print("\noverlap matrix (shared values  |  % of the SMALLER column that overlaps):")
    print(f"  {'email column \\ SAP column':<30}" + "".join(f"{c[:16]:>18}" for c in SAP_KEYS))
    best = (0, None, None)
    for ec in EMAIL_KEYS:
        cells = ""
        for sc in SAP_KEYS:
            inter = ev[ec] & sv[sc]
            denom = min(len(ev[ec]), len(sv[sc])) or 1
            pct = 100 * len(inter) / denom
            cells += f"{f'{len(inter)} ({pct:.0f}%)':>18}"
            if len(inter) > best[0]:
                best = (len(inter), ec, sc)
        print(f"  {ec:<30}{cells}")

    print("\n" + "=" * 60)
    if best[1]:
        print(f"✓ strongest join pair: email.{best[1]}  <->  sap.{best[2]}  ({best[0]} shared values)")
        print("  If the matcher's precedence isn't joining on THIS pair, that's why rows don't match.")
    else:
        print("✗ NO overlap on any ref-column pair — the two feeds share no common reference.")
        print("  Reconciliation by ref is impossible until a shared key exists (or a mapping is added).")


def main() -> int:
    vendor = next((a for a in sys.argv[1:] if not a.startswith("-")), "MBANK")
    print(f"READ-ONLY key-alignment audit — VENDOR={vendor}  SOURCE_MODE={settings.source_mode}")
    print("(this writes NOTHING)")

    email_docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    sap_docs = _query(settings.cosmos_sap_container,
                      "SELECT * FROM c WHERE c.vendorid=@v", [{"name": "@v", "value": vendor}])
    if not sap_docs:
        sap_docs = _query(settings.cosmos_sap_container, "SELECT * FROM c", [])
    print(f"email-source '{settings.cosmos_file_container}': {len(email_docs)} docs")
    print(f"SAP          '{settings.cosmos_sap_container}': {len(sap_docs)} docs")
    if not email_docs or not sap_docs:
        print("  (need docs on both sides to compare)")
        return 1
    _matrix(email_docs, sap_docs)
    _print_samples(email_docs, sap_docs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
