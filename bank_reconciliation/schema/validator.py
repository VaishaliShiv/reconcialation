"""Pre-match validation: blank / damaged / drift detection."""
from __future__ import annotations

from ..models import FieldMapping


class ValidationError(Exception):
    """Carries an alert reason for finance/IT."""
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason          # NO_FILE | BLANK_FILE | DAMAGED | DRIFT
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


def validate(rows: list[dict], canonical, mapping: FieldMapping) -> None:
    if not rows:
        raise ValidationError("NO_FILE", f"{mapping.bank_name}: no rows in source")

    # drift: a mapped source column is missing from the file
    cols = set(rows[0].keys())
    needed = set(mapping.field_map.values())
    missing = needed - cols
    if missing:
        raise ValidationError("DRIFT", f"{mapping.bank_name}: columns missing {sorted(missing)}")

    # blank: rows present but none carry a valid transaction key (footer-only)
    if not canonical:
        raise ValidationError("BLANK_FILE", f"{mapping.bank_name}: no transaction rows")

    # damaged: amount failed to parse on a row that has a key
    bad = [c.partner_txn_id for c in canonical if c.amount is None]
    if bad:
        raise ValidationError("DAMAGED", f"{mapping.bank_name}: unparseable amount for {bad[:5]}")
