"""enrich_anomalies — the advisory pass over engine result rows.

Strictly additive and fail-safe:
- only anomaly rows (classification != 'matched') are sent to the model;
- engine fields (classification/action/amount_*/comment) are NEVER mutated;
- any failure (disabled, no key, API down, bad JSON, hallucinated number) leaves the
  row with its deterministic comment untouched. See explaination_agent.md §5/§5a/§9.
"""
from __future__ import annotations

import logging
from typing import Callable

from ..config import settings
from . import client, guardrails
from .prompt import user as build_user
from .schema import AI_COLUMNS, TriageOutput

log = logging.getLogger(__name__)

CompleteFn = Callable[[str, str], TriageOutput]


def _blank(row: dict) -> None:
    for col in AI_COLUMNS:
        row.setdefault(col, "")


def _sentence(text: str) -> str:
    """Trim and ensure exactly one trailing period (avoids '..' when folding)."""
    s = (text or "").strip().rstrip(".").strip()
    return f"{s}." if s else ""


def _compose(out: TriageOutput, agrees: bool, engine_action) -> str:
    """Fold the structured triage into one readable cell (prefixed AI-suggested)."""
    act = out.suggested_action.value
    action_txt = f"{act} (matches engine)" if agrees else f"{act} (⚠ DIFFERS from engine '{engine_action}')"
    return (
        f"🤖 AI-suggested — verify. "
        f"[severity: {out.severity} | confidence: {out.confidence}] "
        f"{_sentence(out.explanation)} "
        f"Root cause: {_sentence(out.root_cause_hypothesis)} "
        f"Suggested action: {action_txt}. "
        f"Draft note: {_sentence(out.draft_note)}"
    )


def _apply(row: dict, out: TriageOutput) -> None:
    """Write the single ai_explanation column after guardrails. Ungrounded number → discard."""
    grounded = (guardrails.numbers_are_grounded(out.explanation, row)
                and guardrails.numbers_are_grounded(out.draft_note, row))
    if not grounded:
        log.warning("triage discarded (ungrounded number) key=%s", row.get("match_key_used"))
        _blank(row)
        return
    agrees = guardrails.action_agrees(out, row)
    if not agrees:
        log.warning("triage action disagreement key=%s engine=%s model=%s",
                    row.get("match_key_used"), row.get("action"), out.suggested_action.value)
    row["ai_explanation"] = _compose(out, agrees, row.get("action"))


def enrich_anomalies(rows: list[dict], *, complete_fn: CompleteFn | None = None,
                     history: list[dict] | None = None) -> list[dict]:
    """Enrich anomaly rows in place with ai_* columns; return the same list.

    No-op (rows unchanged, no ai_* columns) when triage is disabled.
    """
    if not settings.triage_enabled:
        return rows
    complete_fn = complete_fn or client.default_complete
    for r in rows:
        if r.get("classification") == "matched":
            continue
        _blank(r)
        try:
            out = complete_fn(client.system_prompt(), build_user(r, history))
            _apply(r, out)
        except Exception as exc:                       # noqa: BLE001 - never break the run
            log.warning("triage skipped key=%s: %s", r.get("match_key_used"), exc)
    return rows
