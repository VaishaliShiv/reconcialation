"""READ-ONLY verification that the write-back actually landed in Cosmos. Writes NOTHING.

Run this AFTER `run_cosmos_workflow.py MBANK --write-sap --write-summary` to confirm the
writes really happened:
  - bank-sap-source : how many SAP docs now carry reconflag + remarks (and the value split)
  - recon_results   : which summary docs exist for the vendor (id / date / status)

    python check_writeback.py MBANK

Run it BEFORE the write too — the before/after counts are the proof it wrote.
"""
from __future__ import annotations

import sys
from collections import Counter

from run_cosmos_workflow import _query                       # noqa: E402
from bank_reconciliation.config import settings              # noqa: E402


def _set(v) -> bool:
    return v is not None and str(v).strip() != ""


def _check_sap(vendor: str) -> None:
    docs = _query(settings.cosmos_sap_container,
                  "SELECT * FROM c WHERE c.vendorid=@v", [{"name": "@v", "value": vendor}])
    print(f"\nSAP container '{settings.cosmos_sap_container}': {len(docs)} docs for vendor={vendor}")
    if not docs:
        print("  (no SAP rows for this vendor)")
        return
    with_flag = [d for d in docs if _set(d.get("reconflag"))]
    with_rem = sum(1 for d in docs if _set(d.get("remarks")))
    flag_split = Counter((d.get("reconflag") or "").strip() or "(empty)" for d in docs)
    print(f"  reconflag populated : {len(with_flag)}/{len(docs)}")
    print(f"  remarks populated   : {with_rem}/{len(docs)}")
    print(f"  reconflag values    : {dict(flag_split)}")
    print("  samples:")
    for d in docs[:5]:
        ref = d.get("partnertransactionid") or d.get("paymentreferencenumber") or d.get("dewatransactionid")
        print(f"    ref={str(ref):<22} reconflag={str(d.get('reconflag')):<10} remarks={d.get('remarks')!r}")
    if with_flag:
        print("  => SAP write-back HAS landed (reconflag present).")
    else:
        print("  => no reconflag on any SAP doc yet — write-back has NOT run (or wrote nothing).")


def _check_summary(vendor: str) -> None:
    docs = _query(settings.cosmos_results_container,
                  "SELECT * FROM c WHERE c.vendor_id=@v", [{"name": "@v", "value": vendor}])
    print(f"\nsummary container '{settings.cosmos_results_container}': "
          f"{len(docs)} summary doc(s) for vendor={vendor}")
    if not docs:
        print("  => no summary doc yet — --write-summary has NOT run for this vendor.")
        return
    print(f"  {'id':<28}{'date':<14}{'status':<12}generated_at")
    for d in sorted(docs, key=lambda x: x.get("date") or ""):
        print(f"  {str(d.get('id')):<28}{str(d.get('date')):<14}"
              f"{str(d.get('status')):<12}{d.get('generated_at')}")
    print("  => summary write HAS landed.")


def main() -> int:
    vendor = next((a for a in sys.argv[1:] if not a.startswith("-")), "MBANK")
    print(f"READ-ONLY write-back verification — VENDOR={vendor}  SOURCE_MODE={settings.source_mode}")
    print("(this writes NOTHING)")
    _check_sap(vendor)
    _check_summary(vendor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
