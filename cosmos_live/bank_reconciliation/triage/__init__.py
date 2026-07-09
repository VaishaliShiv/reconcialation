"""Anomaly explanation & triage agent — ADVISORY layer on top of the deterministic engine.

Strictly additive: adds `ai_*` columns to anomaly rows, never edits engine fields
(classification / action / amounts). Fails safe — if disabled or the LLM is
unavailable, rows keep their deterministic `comment`. See explaination_agent.md.
"""
from .agent import enrich_anomalies
from .schema import AI_COLUMNS, TriageOutput

__all__ = ["enrich_anomalies", "AI_COLUMNS", "TriageOutput"]
