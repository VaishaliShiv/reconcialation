"""Deterministic checks that wrap the untrusted model output (see explaination_agent.md §5a).

Every guardrail fails safe toward the engine: a trip discards AI text, never the row.
"""
from __future__ import annotations

import re

from .schema import TriageOutput

_MONEY_FIELDS = ("amount_source", "amount_sap", "amount_diff")
_NUM = re.compile(r"\d[\d,]*\.?\d*")


def _allowed_amounts(row: dict) -> set[str]:
    out: set[str] = set()
    for k in _MONEY_FIELDS:
        v = row.get(k)
        if v in (None, ""):
            continue
        try:
            out.add(f"{abs(float(v)):.2f}")
        except (TypeError, ValueError):
            pass
    return out


def numbers_are_grounded(text: str | None, row: dict) -> bool:
    """A money-shaped figure (has a decimal point) in the text MUST equal a row amount.

    Bare integers (counts, IDs, date parts) are ignored — only decimal figures are
    treated as monetary claims, so a fabricated amount is caught without false-flagging
    '2 postings' or a reference number.
    """
    allowed = _allowed_amounts(row)
    for tok in _NUM.findall((text or "").replace(",", "")):
        if "." not in tok:            # not a money claim
            continue
        try:
            n = f"{abs(float(tok)):.2f}"
        except ValueError:
            continue
        if n not in allowed:
            return False
    return True


def action_agrees(out: TriageOutput, row: dict) -> bool:
    """True when the model's suggested action matches the engine's (engine always wins)."""
    return out.suggested_action.value == row.get("action")
