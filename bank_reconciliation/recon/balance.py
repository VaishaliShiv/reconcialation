"""Fund/GL-level balance. Totals are summed from rows, never read from a footer."""
from __future__ import annotations

from ..models import BalanceSnapshot, CanonicalTxn, MatchType, ReconStatus


def snapshot(bank_name: str, file_txns: list[CanonicalTxn],
             sap_txns: list[CanonicalTxn], results: list[dict],
             gl_account: str | None = None) -> BalanceSnapshot:
    file_total = round(sum(t.amount or 0 for t in file_txns), 2)
    sap_total = round(sum(t.amount or 0 for t in sap_txns), 2)
    diff = round(file_total - sap_total, 2)

    has_anomaly = any(r["match_type"] != MatchType.MATCHED for r in results)
    if diff == 0 and not has_anomaly:
        status = ReconStatus.FULLY_RECONCILED
    elif not has_anomaly:
        status = ReconStatus.AUTO_RECONCILED
    else:
        status = ReconStatus.NON_RECONCILED

    file_date = next((t.txn_date for t in file_txns if t.txn_date), None)
    return BalanceSnapshot(
        bank_name=bank_name, recon_date=file_date,
        gl_account=gl_account or next((t.gl_account for t in sap_txns if t.gl_account), None),
        file_total=file_total, file_count=len(file_txns),
        sap_total=sap_total, sap_count=len(sap_txns),
        balance_diff=diff, recon_status=status,
    )
