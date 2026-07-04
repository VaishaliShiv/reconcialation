"""Map a match_type -> finance action + human-readable comment."""
from __future__ import annotations

from ..models import Action, MatchType

_RULES = {
    MatchType.MATCHED: (Action.NONE, "Reconciled. File and SAP agree."),
    MatchType.MISSING_IN_SAP: (
        Action.POST, "Success in bank file but not posted in SAP — post this transaction."),
    MatchType.MISSING_IN_FILE: (
        Action.REVERSE, "Posted in SAP but absent from bank file — reverse (may be a late txn; re-check next file)."),
    MatchType.DUPLICATE: (
        Action.REVERSE, "Duplicate posting in SAP for this reference — reverse the extra."),
    MatchType.AMOUNT_MISMATCH: (
        Action.REPOST, "Key matches but amount differs — verify and repost the correct amount."),
    MatchType.DATE_MISMATCH: (
        Action.REPOST, "Key and amount match but posting date differs — verify and correct the posting date."),
    MatchType.INVALID_RECORD: (
        Action.RETURN_TO_BANK, "Row failed validation (missing key / amount / date / type) — return to bank."),
}


def decide(match_type: MatchType) -> tuple[Action, str]:
    return _RULES.get(match_type, (Action.NONE, ""))
