"""Run-level summary for the `summary` container — money-weighted + structured.

Two layers, and the LLM produces NO numbers:
  - build_summary_facts()  — ALL figures (exposure, largest anomaly, %, health) computed
    deterministically in Python from the engine rows + balance snapshot.
  - summarize() / summarize_structured() — phrase those facts. AI-phrased when triage is
    enabled AND every figure in the text is grounded; otherwise a deterministic template.
    Any failure falls back to deterministic. Numbers are never hallucinated.
"""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from ..config import settings
from . import client, guardrails

log = logging.getLogger(__name__)

# Health thresholds (deterministic). exposure_pct = anomaly exposure / file (or SAP) value.
_INVALID_BLOCK_RATIO = 0.2      # >20% invalid_record => likely a field-name mismatch
_EXPOSURE_BLOCKED_PCT = 10.0
_EXPOSURE_ATTENTION_PCT = 1.0

SYSTEM = (
    "You summarize one bank-reconciliation run in 2-3 plain sentences for a finance manager. "
    "Use ONLY the figures given — never invent a number. Lead with what needs action and the "
    "money at stake. All amounts are in AED (UAE Dirham): write 'AED', never a '$'. "
    "Be concise, factual, audit-appropriate."
)


class RunSummary(BaseModel):
    """Structured run summary — drives a dashboard/notification. Every numeric field is
    computed deterministically; only `headline`/`summary_text` prose may be AI-phrased."""
    vendorid: str
    date: str
    health: Literal["clean", "minor", "needs_attention", "blocked"]
    headline: str
    summary_text: str
    total: int
    matched: int
    anomalies: int
    exposure_aed: float
    unreconciled_pct: float
    file_total: float
    sap_total: float
    balance_diff: float
    recon_status: str
    counts: dict
    largest: dict | None
    top_actions: list[dict]


def _f(v) -> float:
    try:
        return abs(float(v))
    except (TypeError, ValueError):
        return 0.0


def _exposure(r: dict) -> float:
    """AED at risk for one anomaly row (deterministic):
    amount_mismatch -> the difference; missing_in_sap -> bank amount not posted;
    missing_in_file -> SAP amount with no bank row; duplicate -> the duplicated amount;
    date_mismatch / invalid_record -> 0 (amounts agree or are unusable)."""
    c = r.get("classification")
    if c == "amount_mismatch":
        return _f(r.get("amount_diff"))
    if c == "missing_in_sap":
        return _f(r.get("amount_source"))
    if c == "missing_in_file":
        return _f(r.get("amount_sap"))
    if c == "duplicate":
        return _f(r.get("amount_source")) or _f(r.get("amount_sap"))
    return 0.0


def _ref(r: dict) -> str:
    ku = r.get("match_key_used") or ""
    return (ku.split(":", 1)[1] if ":" in ku else ku) or (r.get("partner_trn_reference_no") or "")


def build_summary_facts(rows: list[dict], snap, vendorid: str, date: str) -> dict:
    """All the deterministic numbers the summary needs. No LLM involved."""
    by: dict[str, int] = {}
    for r in rows:
        by[r["classification"]] = by.get(r["classification"], 0) + 1
    matched = by.get("matched", 0)
    total = len(rows)
    anomalies = total - matched

    anom_rows = [r for r in rows if r.get("classification") != "matched"]
    scored = sorted(((_exposure(r), r) for r in anom_rows), key=lambda x: x[0], reverse=True)
    exposure = round(sum(e for e, _ in scored), 2)

    file_total = round(float(snap.file_total or 0), 2)
    sap_total = round(float(snap.sap_total or 0), 2)
    base = file_total or sap_total
    unreconciled_pct = round(100 * exposure / base, 1) if base else 0.0

    largest = None
    if scored and scored[0][0] > 0:
        e, r = scored[0]
        largest = {"ref": _ref(r), "classification": r.get("classification"),
                   "action": r.get("action"), "exposure_aed": round(e, 2),
                   "amount_source": r.get("amount_source"), "amount_sap": r.get("amount_sap")}
    top_actions = [{"ref": _ref(r), "classification": r.get("classification"),
                    "action": r.get("action"), "exposure_aed": round(e, 2)}
                   for e, r in scored[:3] if e > 0]

    invalid_ratio = (by.get("invalid_record", 0) / total) if total else 0.0
    if anomalies == 0:
        health = "clean"
    elif invalid_ratio > _INVALID_BLOCK_RATIO or unreconciled_pct >= _EXPOSURE_BLOCKED_PCT:
        health = "blocked"
    elif unreconciled_pct >= _EXPOSURE_ATTENTION_PCT:
        health = "needs_attention"
    else:
        health = "minor"

    return {
        "vendorid": vendorid, "date": date, "total": total, "matched": matched,
        "anomalies": anomalies, "by": by, "counts": by,
        "exposure_aed": exposure, "unreconciled_pct": unreconciled_pct,
        "file_total": file_total, "sap_total": sap_total,
        "balance_diff": round(float(snap.balance_diff or 0), 2),
        "recon_status": snap.recon_status.value, "health": health,
        "largest": largest, "top_actions": top_actions,
    }


def _headline(f: dict) -> str:
    """Deterministic one-line status chip. Never AI-generated (a dashboard label must be exact)."""
    if f["health"] == "clean":
        return f"CLEAN — {f['matched']}/{f['total']} matched, no anomalies."
    return (f"{f['health'].replace('_', ' ').upper()} — {f['anomalies']} of {f['total']} need "
            f"attention, AED {f['exposure_aed']} exposure ({f['unreconciled_pct']}% of value).")


def _deterministic(f: dict) -> str:
    """Money-weighted deterministic narrative (used when triage off / AI ungrounded / error)."""
    if f["anomalies"] == 0:
        return (f"All {f['total']} transactions reconciled for {f['vendorid']} (email {f['date']}). "
                f"File and SAP agree at AED {f['file_total']} ({f['recon_status']}).")
    parts = [f"{f['anomalies']} of {f['total']} transactions need attention for {f['vendorid']} "
             f"(email {f['date']}) — AED {f['exposure_aed']} exposure "
             f"({f['unreconciled_pct']}% of file value)."]
    if f["largest"]:
        lg = f["largest"]
        parts.append(f"Largest: {lg['classification']} on {lg['ref']} "
                     f"(AED {lg['exposure_aed']}), action: {lg['action']}.")
    anoms = {k: v for k, v in f["by"].items() if k != "matched"}
    parts.append("Breakdown: " + ", ".join(f"{v} {k}" for k, v in anoms.items()) + ".")
    return " ".join(parts)


def _user(f: dict) -> str:
    lg = f["largest"] or {}
    return (f"vendor={f['vendorid']} email_date={f['date']} total={f['total']} "
            f"matched={f['matched']} anomalies={f['anomalies']} breakdown={f['by']} "
            f"exposure_aed={f['exposure_aed']} unreconciled_pct={f['unreconciled_pct']} "
            f"file_total={f['file_total']} sap_total={f['sap_total']} "
            f"balance_diff={f['balance_diff']} status={f['recon_status']} health={f['health']} "
            f"largest_ref={lg.get('ref')} largest_class={lg.get('classification')} "
            f"largest_exposure={lg.get('exposure_aed')} largest_action={lg.get('action')}")


def _allowed_figures(rows: list[dict], f: dict) -> set[float]:
    """Every real figure the summary may legitimately cite — totals, exposure, %, per-row amounts."""
    vals: set[float] = {f["file_total"], f["sap_total"], f["balance_diff"],
                        f["exposure_aed"], f["unreconciled_pct"]}
    if f["largest"]:
        vals.add(f["largest"]["exposure_aed"])
    for r in rows:
        for k in ("amount_source", "amount_sap", "amount_diff"):
            if r.get(k) not in (None, ""):
                vals.add(r[k])
    return vals


def _narrative(rows: list[dict], f: dict, complete_fn) -> str:
    """AI-phrased narrative when grounded, else deterministic. Numbers never hallucinated."""
    base = _deterministic(f)
    if not settings.triage_enabled:
        return base
    try:
        text = ((complete_fn or client.complete_text)(SYSTEM, _user(f)) or "").strip()
        if text and guardrails.numbers_grounded_in(text, _allowed_figures(rows, f)):
            return text
        log.warning("run summary discarded (ungrounded/empty) -> deterministic")
    except Exception as exc:  # noqa: BLE001 - never break the run
        log.warning("run summary AI failed (%s) -> deterministic", exc)
    return base


def summarize(rows: list[dict], snap, vendorid: str, date: str, *, complete_fn=None) -> str:
    """Return the run summary TEXT (money-weighted). Backward-compatible string return."""
    f = build_summary_facts(rows, snap, vendorid, date)
    return _narrative(rows, f, complete_fn)


def summarize_structured(rows: list[dict], snap, vendorid: str, date: str,
                         *, complete_fn=None) -> RunSummary:
    """Return the full structured RunSummary (headline/health/exposure/top_actions + prose).
    All numbers are deterministic; only summary_text prose may be AI-phrased (grounded)."""
    f = build_summary_facts(rows, snap, vendorid, date)
    return RunSummary(
        vendorid=vendorid, date=date, health=f["health"], headline=_headline(f),
        summary_text=_narrative(rows, f, complete_fn),
        total=f["total"], matched=f["matched"], anomalies=f["anomalies"],
        exposure_aed=f["exposure_aed"], unreconciled_pct=f["unreconciled_pct"],
        file_total=f["file_total"], sap_total=f["sap_total"], balance_diff=f["balance_diff"],
        recon_status=f["recon_status"], counts=f["counts"],
        largest=f["largest"], top_actions=f["top_actions"],
    )
