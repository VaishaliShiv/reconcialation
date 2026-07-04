"""Map the NEW-format bank file + SAP T_TXNS rows -> CanonicalTxn. Deterministic.

New canonical contract (see MEMORY.md §10). This replaces the raw-column mapping in
schema/mapper.py for the standardized file/SAP formats. No LLM — it's money math.

- Bank file: standard attributes (Partner_Trn_Reference_No, Payment_Ref_No, ...).
- SAP: Cosmos DB columns (vendorid, transactiondate, partnertransactionid,
  paymentreferencenumber, dewatransactionid, amount, currency, status,
  reconflag, remarks).
- source_channel / bank name is injected from the vendor code (not in either feed).
- Validate only 3 fields (≥1 ref key, amount, date) -> else invalid_record.
  (Type is read for reporting but is NOT required — it may be absent in this feed.)
"""
from __future__ import annotations

from datetime import date, datetime

from ..models import CanonicalTxn

# vendor code -> injected source_channel (bank/agency display name)
VENDOR_SOURCE = {"MBANK": "M-Bank"}


def _s(v) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _norm_key(v) -> str | None:
    """Normalize a reference key so both sides join on the same string.

    Handles the common formatting drift that would otherwise cause false
    missing_in_sap / missing_in_file: leading/trailing/internal whitespace
    (incl. non-breaking space) and a trailing '.0' from numeric imports.
    """
    v = _s(v)
    if v is None:
        return None
    v = "".join(v.split())                      # drop all whitespace
    if v.endswith(".0") and v[:-2].isdigit():   # e.g. 2041141921.0 -> 2041141921
        v = v[:-2]
    return v or None


def _amount(v) -> float | None:
    v = _s(v)
    if v is None:
        return None
    try:
        return round(float(v.replace(",", "")), 2)
    except ValueError:
        return None


def parse_date(v) -> date | None:
    """Normalize a date/datetime string -> date (day-level).

    Handles the real Cosmos email-source formats:
      - ISO datetime with 'T', microseconds and/or timezone
        ('2026-06-24T00:00:00', '2026-07-03T06:18:34.110741+00:00')
      - plain ISO ('2026-06-24')
      - dd/mm/yyyy or mm/dd/yyyy (auto-detected)
    """
    v = _s(v)
    if v is None:
        return None
    try:                                   # ISO datetime (T / tz / microseconds) -> date
        return datetime.fromisoformat(v).date()
    except ValueError:
        pass
    v = v.replace("T", " ").split()[0]     # drop any remaining time part
    try:
        return date.fromisoformat(v)       # ISO yyyy-mm-dd
    except ValueError:
        pass
    parts = v.replace("-", "/").split("/")
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        a, b, c = (int(p) for p in parts)
        if c < 100:
            c += 2000
        if a > 12 and b <= 12:             # first field must be the day
            d, m = a, b
        elif b > 12 and a <= 12:           # second field is the day -> mm/dd
            d, m = b, a
        else:                              # ambiguous -> default dd/mm/yyyy
            d, m = a, b
        try:
            return date(c, m, d)
        except ValueError:
            return None
    return None


def map_bank_file(rows: list[dict], vendor_code: str) -> list[CanonicalTxn]:
    """Normalize standard bank-file attribute rows. Marks invalid rows (3-field check)."""
    src = VENDOR_SOURCE.get(vendor_code, vendor_code)
    out: list[CanonicalTxn] = []
    for row in rows:
        partner = _norm_key(row.get("Partner_Trn_Reference_No"))
        payment = _norm_key(row.get("Payment_Ref_No"))
        # live Cosmos column is 'DEWATrn_Reference_No' (no underscore); accept the
        # underscored spelling too so either standardized feed still joins.
        dewa = _norm_key(row.get("DEWATrn_Reference_No") or row.get("DEWA_Trn_Reference_No"))
        amount = _amount(row.get("Trn_Amount"))
        txn_date = parse_date(row.get("Trn_Date"))
        ttype = _s(row.get("Type"))  # kept for reporting only — NOT a validation gate

        reasons: list[str] = []
        if not (partner or payment or dewa):
            reasons.append("no reference key")
        if amount is None:
            reasons.append("unparseable amount")
        if txn_date is None:
            reasons.append("unparseable date")

        if partner:
            mk, kind = partner, "partner"
        elif payment:
            mk, kind = payment, "payment"
        elif dewa:
            mk, kind = dewa, "dewa"
        else:
            mk, kind = None, None

        out.append(CanonicalTxn(
            source_type="bank", bank_name=vendor_code,
            partner_txn_id=partner or "",
            payment_ref_no=payment, dewa_txn_ref=dewa,
            amount=amount, txn_date=txn_date, txn_type=ttype,
            settlement_date=parse_date(row.get("Settlement_Date")),
            upload_date=parse_date(row.get("Upload_Date")),
            details=_s(row.get("Details")),
            status=_s(row.get("Status")) or "",
            source_channel=src,
            match_key=mk, match_kind=kind,
            valid=not reasons, invalid_reason="; ".join(reasons) or None,
            raw_row=row,
        ))
    return out


def map_sap_txns(rows: list[dict]) -> list[CanonicalTxn]:
    """Normalize SAP Cosmos DB rows into canonical shape.

    New SAP Cosmos structure (lowercase keys):
      vendorid, transactiondate, partnertransactionid, paymentreferencenumber,
      dewatransactionid, amount, currency, status, reconflag, remarks.

    partnertransactionid / paymentreferencenumber hold the partner and payment
    refs; dewatransactionid holds the DEWA ref. The join key mirrors the file
    side's precedence: partner -> payment -> dewa.
    """
    out: list[CanonicalTxn] = []
    for row in rows:
        partner = _norm_key(row.get("partnertransactionid"))
        payment = _norm_key(row.get("paymentreferencenumber"))
        dewa = _norm_key(row.get("dewatransactionid"))
        key = partner or payment or dewa
        out.append(CanonicalTxn(
            source_type="sap", bank_name=_s(row.get("vendorid")) or "",
            partner_txn_id=key or "",
            payment_ref_no=payment,
            dewa_txn_ref=dewa,
            amount=_amount(row.get("amount")),
            txn_date=parse_date(row.get("transactiondate")),
            status=_s(row.get("status")) or "",
            details=_s(row.get("remarks")),
            match_key=key, match_kind="txnid",
            raw_row=row,
        ))
    return out
