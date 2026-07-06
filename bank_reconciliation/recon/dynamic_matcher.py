"""Dynamic-key deterministic match for the new canonical format. No LLM — it's money.

Keys: partner/payment ref -> SAP TXNID; DEWA ref -> SAP DEWATN. When several refs are
present (e.g. partner + DEWA) they must ALL agree (AND). Invalid rows (failed the
4-field validation) map straight to INVALID_RECORD and never join.

Precedence for a joined pair: duplicate > amount > date > dewa-disagreement > matched.
"""
from __future__ import annotations

from collections import defaultdict

from ..models import CanonicalTxn, MatchType


def _same_day(a, b) -> bool:
    """Day-level compare. Missing date on either side -> not a mismatch."""
    if a is None or b is None:
        return True
    return a == b


def _row(f: CanonicalTxn | None, sap_rows: list[CanonicalTxn], mt: MatchType,
         sap_amt: float | None = None, sap_date=None, note: str = "") -> dict:
    sap_rows = sap_rows or []
    if sap_amt is None and sap_rows:
        sap_amt = round(sum(s.amount or 0 for s in sap_rows), 2)
    if sap_date is None and sap_rows:
        sap_date = sap_rows[0].txn_date
    file_amt = round(f.amount, 2) if (f and f.amount is not None) else None
    partner = (f.partner_txn_id if f else None) or (sap_rows[0].partner_txn_id if sap_rows else "")
    dewa = (f.dewa_txn_ref if f else None) or (sap_rows[0].dewa_txn_ref if sap_rows else None)
    if f and f.match_key:
        key_used = f"{f.match_kind}:{f.match_key}"
    elif sap_rows:
        key_used = f"txnid:{sap_rows[0].partner_txn_id}"
    else:
        key_used = ""
    return {
        "partner_trn_reference_no": partner,
        "payment_ref_no": f.payment_ref_no if f else None,
        "dewa_trn_reference_no": dewa,
        "type": f.txn_type if f else None,
        "match_key_used": key_used,
        "file_amount": file_amt, "sap_amount": sap_amt,
        "amount_diff": round((file_amt or 0) - (sap_amt or 0), 2),
        "date_source": f.txn_date if f else None, "date_sap": sap_date,
        "settlement_date": f.settlement_date if f else None,
        "source_channel": f.source_channel if f else None,
        "match_type": mt, "note": note,
    }


# ============================================================================
# OLD FORMAT (single-key precedence join) — REPLACED by the email-driven AND
# rule below. Kept commented for reference / rollback.
# ============================================================================
# def _resolve_key(f: CanonicalTxn, dewatn_to_txnid: dict) -> str | None:
#     """A DEWA-only file row joins via DEWATN->TXNID so it lands in the TXNID join space."""
#     if f.match_kind == "dewa":
#         return dewatn_to_txnid.get(f.dewa_txn_ref, f.match_key)
#     return f.match_key
#
#
# def reconcile(file_txns: list[CanonicalTxn], sap_txns: list[CanonicalTxn],
#               one_to_many: bool = False) -> list[dict]:
#     """Return one dict per logical transaction with match_type + amounts."""
#     results: list[dict] = []
#
#     # invalid rows bypass matching
#     for t in file_txns:
#         if not t.valid:
#             results.append(_row(t, [], MatchType.INVALID_RECORD, note=t.invalid_reason or ""))
#     valid_file = [t for t in file_txns if t.valid]
#
#     sap_by: dict[str, list[CanonicalTxn]] = defaultdict(list)
#     for s in sap_txns:
#         sap_by[s.partner_txn_id].append(s)
#     dewatn_to_txnid = {s.dewa_txn_ref: s.partner_txn_id for s in sap_txns if s.dewa_txn_ref}
#
#     file_keys: set[str] = set()
#     for t in valid_file:
#         key = _resolve_key(t, dewatn_to_txnid)
#         file_keys.add(key)
#         sap_rows = sap_by.get(key, [])
#         if not sap_rows:
#             results.append(_row(t, [], MatchType.MISSING_IN_SAP))
#             continue
#         if len(sap_rows) > 1 and not one_to_many:
#             results.append(_row(t, sap_rows, MatchType.DUPLICATE))
#             continue
#         sap_amt = round(sum(s.amount or 0 for s in sap_rows), 2)
#         sap_date = sap_rows[0].txn_date
#         sap_dewa = next((s.dewa_txn_ref for s in sap_rows if s.dewa_txn_ref), None)
#         if round(t.amount or 0, 2) != sap_amt:
#             mt, note = MatchType.AMOUNT_MISMATCH, ""
#         elif not _same_day(t.txn_date, sap_date):
#             mt, note = MatchType.DATE_MISMATCH, ""
#         elif t.dewa_txn_ref and sap_dewa and t.dewa_txn_ref != sap_dewa:
#             mt, note = MatchType.MISSING_IN_SAP, "DEWA ref disagreement (AND key)"
#         else:
#             mt, note = MatchType.MATCHED, ""
#         results.append(_row(t, sap_rows, mt, sap_amt=sap_amt, sap_date=sap_date, note=note))
#
#     # SAP-only keys -> missing_in_file (or duplicate)
#     seen: set[str] = set()
#     for s in sap_txns:
#         k = s.partner_txn_id
#         if k in file_keys or k in seen:
#             continue
#         seen.add(k)
#         rows = sap_by[k]
#         mt = MatchType.DUPLICATE if (len(rows) > 1 and not one_to_many) else MatchType.MISSING_IN_FILE
#         results.append(_row(None, rows, mt))
#     return results
# ============================================================================


# ============================================================================
# NEW FORMAT — email-driven AND match (see MATCHING_RULES.md).
# ============================================================================
def _refs(t: CanonicalTxn) -> dict:
    """The three canonical references on a row (None where absent)."""
    return {"partner": t.partner_txn_id or None,
            "payment": t.payment_ref_no or None,
            "dewa": t.dewa_txn_ref or None}


def reconcile(file_txns: list[CanonicalTxn], sap_txns: list[CanonicalTxn],
              one_to_many: bool = False) -> list[dict]:
    """Email-driven AND match.

    A file row matches a SAP row iff EVERY reference present in the FILE row equals
    SAP's corresponding field (references absent from the file are skipped). Then amount
    and same-day date must also agree for MATCHED. The join first pairs on ANY shared ref.
    """
    results: list[dict] = []

    for t in file_txns:                       # invalid rows bypass matching
        if not t.valid:
            results.append(_row(t, [], MatchType.INVALID_RECORD, note=t.invalid_reason or ""))
    valid_file = [t for t in file_txns if t.valid]

    # index SAP rows by each actual reference value
    idx: dict[str, dict[str, list[CanonicalTxn]]] = {"partner": defaultdict(list),
                                                      "payment": defaultdict(list),
                                                      "dewa": defaultdict(list)}
    for s in sap_txns:
        for k, v in _refs(s).items():
            if v:
                idx[k][v].append(s)

    matched_sap: set[int] = set()             # id() of SAP rows consumed by a match/dup
    for t in valid_file:
        present = {k: v for k, v in _refs(t).items() if v}   # refs the FILE row carries
        cand: dict[int, CanonicalTxn] = {}                   # SAP rows sharing ANY present ref
        for k, v in present.items():
            for s in idx[k].get(v, []):
                cand[id(s)] = s
        # full match: SAP row agreeing on EVERY reference the file row carries (AND)
        full = [s for s in cand.values()
                if all(_refs(s).get(k) == v for k, v in present.items())]
        if not full:
            note = "ref disagreement (present refs don't all match SAP)" if cand else ""
            results.append(_row(t, [], MatchType.MISSING_IN_SAP, note=note))
            continue
        if len(full) > 1 and not one_to_many:
            for s in full:
                matched_sap.add(id(s))
            results.append(_row(t, full, MatchType.DUPLICATE))
            continue
        s = full[0]
        matched_sap.add(id(s))
        sap_amt = round(s.amount or 0, 2)
        if round(t.amount or 0, 2) != sap_amt:
            mt, note = MatchType.AMOUNT_MISMATCH, ""
        elif not _same_day(t.txn_date, s.txn_date):
            mt, note = MatchType.DATE_MISMATCH, ""
        else:
            mt, note = MatchType.MATCHED, ""
        results.append(_row(t, [s], mt, sap_amt=sap_amt, sap_date=s.txn_date, note=note))

    for s in sap_txns:                        # SAP rows never matched -> missing_in_file
        if id(s) not in matched_sap:
            results.append(_row(None, [s], MatchType.MISSING_IN_FILE))
    return results
