"""The constrained output contract for the triage agent (structured output)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from ..models import Action


class TriageOutput(BaseModel):
    """What the model is allowed to return. Validated at the tool-call boundary."""
    explanation: str            # plain-language why; references only real figures
    root_cause_hypothesis: str  # e.g. "bank fee deduction", "late posting", "keying error"
    severity: Literal["low", "medium", "high"]
    suggested_action: Action    # checked against the engine action (engine wins)
    confidence: Literal["low", "medium", "high"]
    draft_note: str             # editable analyst comment — never auto-applied


# Single additive column: the whole triage composed into one readable cell.
# Engine columns are never touched. The structured fields above are still produced
# internally (guardrails validate them per-field) then folded into this one column.
AI_COLUMNS = ["ai_explanation"]
