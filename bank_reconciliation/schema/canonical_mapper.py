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


def _bank_txn(*, partner, payment, dewa, amount, txn_date, ttype, settlement,
              upload, details, status, vendor_code, raw) -> CanonicalTxn:
    """Build one bank-side CanonicalTxn from already-extracted values (3-field check)."""
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

    return CanonicalTxn(
        source_type="bank", bank_name=vendor_code,
        partner_txn_id=partner or "", payment_ref_no=payment, dewa_txn_ref=dewa,
        amount=amount, txn_date=txn_date, txn_type=ttype,
        settlement_date=settlement, upload_date=upload, details=details,
        status=status or "", source_channel=VENDOR_SOURCE.get(vendor_code, vendor_code),
        match_key=mk, match_kind=kind,
        valid=not reasons, invalid_reason="; ".join(reasons) or None, raw_row=raw,
    )


def map_bank_file(rows: list[dict], vendor_code: str) -> list[CanonicalTxn]:
    """LEGACY flat format (PascalCase columns). Kept for the old CSV/flat feed."""
    out: list[CanonicalTxn] = []
    for row in rows:
        out.append(_bank_txn(
            partner=_norm_key(row.get("Partner_Trn_Reference_No")),
            payment=_norm_key(row.get("Payment_Ref_No")),
            dewa=_norm_key(row.get("DEWATrn_Reference_No") or row.get("DEWA_Trn_Reference_No")),
            amount=_amount(row.get("Trn_Amount")), txn_date=parse_date(row.get("Trn_Date")),
            ttype=_s(row.get("Type")), settlement=parse_date(row.get("Settlement_Date")),
            upload=parse_date(row.get("Upload_Date")), details=_s(row.get("Details")),
            status=_s(row.get("Status")), vendor_code=vendor_code, raw=row,
        ))
    return out


def map_email_doc(doc: dict, vendor_code: str | None = None) -> tuple[str, str, object, list[CanonicalTxn]]:
    """NEW nested email FILE -> (filename, vendor_id, upload_date, transactions).

    One Cosmos doc = one file. Fields are camelCase inside the `transactions` array;
    vendorId / uploadDate / id (=file name) live at the top level. Status filtering
    (skip 'Completed') is left to the caller so it can report the skipped count.
    """
    filename = _s(doc.get("id")) or ""
    vendor = _s(doc.get("vendorId")) or vendor_code or ""
    upload = parse_date(doc.get("uploadDate"))
    out: list[CanonicalTxn] = []
    for row in doc.get("transactions", []) or []:
        out.append(_bank_txn(
            partner=_norm_key(row.get("partnerTrnReferenceNo")),
            payment=_norm_key(row.get("paymentRefNo")),
            dewa=_norm_key(row.get("dewaTrnReferenceNo")),
            amount=_amount(row.get("trnAmount")), txn_date=parse_date(row.get("trnDate")),
            ttype=_s(row.get("type")), settlement=parse_date(row.get("settlementDate")),
            upload=upload, details=None, status=_s(row.get("status")),
            vendor_code=vendor, raw=row,
        ))
    return filename, vendor, upload, out


def map_sap_txns(rows: list[dict]) -> list[CanonicalTxn]:
    """Normalize SAP transaction rows (lowercase keys) into canonical shape.

    partner_txn_id holds the ACTUAL partnertransactionid (may be empty); payment_ref_no
    and dewa_txn_ref hold their refs. partnertransactionid / paymentreferencenumber may
    arrive as NUMBERS — _norm_key stringifies and strips a trailing '.0'.
    """
    out: list[CanonicalTxn] = []
    for row in rows:
        partner = _norm_key(row.get("partnertransactionid"))
        payment = _norm_key(row.get("paymentreferencenumber"))
        dewa = _norm_key(row.get("dewatransactionid"))
        out.append(CanonicalTxn(
            source_type="sap", bank_name=_s(row.get("vendorid")) or "",
            # OLD FORMAT stored the resolved precedence key here (partner_txn_id=key). NEW rule
            # needs the ACTUAL partner so the AND match can compare each ref field independently.
            partner_txn_id=partner or "", payment_ref_no=payment, dewa_txn_ref=dewa,
            amount=_amount(row.get("amount")), txn_date=parse_date(row.get("transactiondate")),
            status=_s(row.get("status")) or "", details=_s(row.get("remarks")),
            match_key=partner or payment or dewa, match_kind="txnid", raw_row=row,
        ))
    return out


def map_sap_read(docs: list[dict]) -> list[CanonicalTxn]:
    """Flatten SAP READ docs (each has a `transaction` array) -> canonical SAP txns.

    Accepts either a list of READ-response docs or already-flat rows (defensive).
    """
    rows: list[dict] = []
    for doc in docs:
        txns = doc.get("transaction")
        rows.extend(txns if isinstance(txns, list) else [doc])
    return map_sap_txns(rows)
