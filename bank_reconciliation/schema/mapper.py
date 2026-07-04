"""Map raw source rows -> CanonicalTxn using a FieldMapping. Deterministic."""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..models import CanonicalTxn, FieldMapping


def _to_amount(raw, decimal: str) -> float | None:
    if raw is None or raw == "":
        return None
    s = str(raw).replace(",", "") if decimal == "." else str(raw).replace(".", "").replace(",", ".")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _to_date(raw, fmt: str | None) -> date | None:
    if raw is None or raw == "" or fmt is None:
        return None
    if fmt == "epoch_ms":
        try:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).date()
        except (ValueError, TypeError):
            return None
    try:
        return datetime.strptime(str(raw).strip(), fmt).date()
    except ValueError:
        return None


def _norm_key(raw, strip_prefix: str | None) -> str | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if strip_prefix:
        s = s.lstrip(strip_prefix)
    return s


def _str(raw) -> str | None:
    if raw is None or raw == "":
        return None
    return str(raw).strip()


def map_rows(rows: list[dict], m: FieldMapping) -> list[CanonicalTxn]:
    """Footer/Total rows (no valid key) are dropped — totals are computed, never read."""
    fm = m.field_map
    out: list[CanonicalTxn] = []
    for row in rows:
        partner = _norm_key(row.get(fm.get("partner_txn_id", "")), m.strip_prefix)
        if m.drop_rows_without_key and not (partner and partner.isdigit()):
            continue  # footer / "Total :" / blank-key line
        out.append(CanonicalTxn(
            source_type=m.source_type,
            bank_name=m.bank_name,
            partner_txn_id=partner,
            tx_sequence=_norm_key(row.get(fm.get("tx_sequence", "")), None),
            amount=_to_amount(row.get(fm.get("amount", "")), m.decimal),
            txn_date=_to_date(row.get(fm.get("txn_date", "")), m.date_format),
            payment_ref=partner,
            status=_str(row.get(fm.get("status", ""))) if "status" in fm else None,
            gl_account=_str(row.get(fm.get("gl_account", ""))) if "gl_account" in fm else None,
            source_channel=_str(row.get(fm.get("source_channel", ""))) if "source_channel" in fm else None,
            raw_row=row,
        ))
    return out
