"""CLI entrypoint. Usage: python -m bank_reconciliation.main MBank"""
from __future__ import annotations

import sys

from .orchestrator import run


def main() -> None:
    bank = sys.argv[1] if len(sys.argv) > 1 else "MBank"
    out = run(bank)
    s = out["summary"]
    print(f"\n=== Reconciliation: {out['bank_name']} (run {out['run_id']}) ===")
    print(f"rows={s['total_rows']}  by_type={s['by_match_type']}")
    print(f"file_total={s['file_total']}  sap_total={s['sap_total']}  diff={s['balance_diff']}")
    print(f"status={s['recon_status']}  evidence_written={out['evidence_written']}\n")
    for r in out["results"]:
        if r.match_type.value != "matched":
            print(f"  [{r.match_type.value}] {r.partner_txn_id} "
                  f"file={r.file_amount} sap={r.sap_amount} -> {r.action.value}: {r.comment}")


if __name__ == "__main__":
    main()
