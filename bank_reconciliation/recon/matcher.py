"""Deterministic match: file canonical vs SAP canonical. No LLM — it's money."""
from __future__ import annotations

import pandas as pd

from ..models import CanonicalTxn, MatchType


def _df(txns: list[CanonicalTxn]) -> pd.DataFrame:
    if not txns:
        return pd.DataFrame(columns=["partner_txn_id", "amount"])
    return pd.DataFrame([t.model_dump() for t in txns])


def _same_day(a, b) -> bool:
    """Compare at DAY granularity. Missing date on either side → not a mismatch."""
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return True
    return pd.Timestamp(a).date() == pd.Timestamp(b).date()


def reconcile(file_txns: list[CanonicalTxn], sap_txns: list[CanonicalTxn],
              one_to_many: bool = False) -> list[dict]:
    """Return one dict per logical transaction with match_type + amounts.

    ENBD one_to_many: a single file txn maps to N SAP rows -> SAP summed by key.
    Otherwise a repeated SAP key is a duplicate posting.
    """
    f, s = _df(file_txns), _df(sap_txns)
    f["amount"] = f["amount"].round(2)
    s["amount"] = s["amount"].round(2)

    sap_counts = s.groupby("partner_txn_id").size().to_dict()
    sap_agg = (s.groupby("partner_txn_id", as_index=False)
                 .agg(sap_amount=("amount", "sum"),
                      sap_date=("txn_date", "first"),
                      gl_account=("gl_account", "first"),
                      source_channel=("source_channel", "first"),
                      tx_sequence=("tx_sequence", "first")))

    merged = f.merge(sap_agg, on="partner_txn_id", how="outer", indicator=True,
                     suffixes=("_file", ""))

    results: list[dict] = []
    for _, r in merged.iterrows():
        results.append(_classify(r, sap_counts, one_to_many))
    return results


def _classify(r, sap_counts: dict, one_to_many: bool) -> dict:
    key = r["partner_txn_id"]
    file_amt = None if pd.isna(r.get("amount")) else round(float(r["amount"]), 2)
    sap_amt = None if pd.isna(r.get("sap_amount")) else round(float(r["sap_amount"]), 2)
    where = r["_merge"]
    base = {
        "partner_txn_id": key,
        "tx_sequence": r.get("tx_sequence"),
        "file_amount": file_amt,
        "sap_amount": sap_amt,
        "amount_diff": round((file_amt or 0) - (sap_amt or 0), 2),
        "txn_date": r.get("txn_date"),
        "gl_account": r.get("gl_account"),
        "source_channel": r.get("source_channel"),
    }
    if where == "left_only":
        return {**base, "match_type": MatchType.MISSING_IN_SAP}
    if where == "right_only":
        if sap_counts.get(key, 1) > 1 and not one_to_many:
            return {**base, "match_type": MatchType.DUPLICATE}
        return {**base, "match_type": MatchType.MISSING_IN_FILE}
    # both sides present — precedence: duplicate > amount > date > matched
    if sap_counts.get(key, 1) > 1 and not one_to_many:
        return {**base, "match_type": MatchType.DUPLICATE}
    if file_amt != sap_amt:
        return {**base, "match_type": MatchType.AMOUNT_MISMATCH}
    if not _same_day(r.get("txn_date"), r.get("sap_date")):
        return {**base, "match_type": MatchType.DATE_MISMATCH}
    return {**base, "match_type": MatchType.MATCHED}
