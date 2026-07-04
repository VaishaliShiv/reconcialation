"""The reconciliation flow. Glues source -> map -> validate -> match -> evidence.

Phase 1: registry is seeded (M-bank). Phase 2 plugs the inference agent into the
registry-miss branch — the rest of this flow does not change.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .evidence.store import EvidenceStore
from .models import (BalanceSnapshot, EvidenceRecord, ReconResultRow, ReconStatus)
from .recon import actions, balance, matcher
from .schema import mapper
from .schema.registry import Registry
from .schema.validator import ValidationError, validate
from .sources import cosmos_reader, sap_reader


def run(bank_name: str, run_id: str | None = None,
        registry: Registry | None = None) -> dict:
    run_id = run_id or f"{bank_name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    registry = registry or Registry()

    # 1-2. read file rows + resolve the template mapping
    file_rows = cosmos_reader.read_file_rows(bank_name)
    if not file_rows:
        raise ValidationError("NO_FILE", f"{bank_name}: no rows in source")
    mapping = registry.get(bank_name)
    if mapping is None:
        raise ValidationError(
            "NO_TEMPLATE",
            f"{bank_name}: no template mapping in registry — add it manually before reconciling")

    # 3-4. map to canonical (drops footer) + validate
    file_txns = mapper.map_rows(file_rows, mapping)
    validate(file_rows, file_txns, mapping)

    # 5. SAP side for the date set found in the file
    dates = sorted({t.txn_date.isoformat() for t in file_txns if t.txn_date})
    sap_rows = sap_reader.read_sap_rows(bank_name, dates)
    sap_txns = mapper.map_rows(sap_rows, registry.sap_mapping())

    # 6. match + classify
    raw = matcher.reconcile(file_txns, sap_txns, one_to_many=mapping.one_to_many)
    snap = balance.snapshot(bank_name, file_txns, sap_txns, raw)
    results, store = _build(run_id, bank_name, raw, mapping.confidence, snap)

    written = store.flush()
    return {
        "run_id": run_id,
        "bank_name": bank_name,
        "results": results,
        "balance": snap,
        "evidence_written": written,
        "summary": _summary(results, snap),
    }


def _build(run_id, bank_name, raw, confidence, snap: BalanceSnapshot):
    store = EvidenceStore()
    results: list[ReconResultRow] = []
    fully = snap.recon_status == ReconStatus.FULLY_RECONCILED
    ts = datetime.now(timezone.utc)
    for r in raw:
        act, comment = actions.decide(r["match_type"])
        row_status = ReconStatus.FULLY_RECONCILED if fully else ReconStatus.NON_RECONCILED
        results.append(ReconResultRow(
            run_id=run_id, bank_name=bank_name,
            partner_txn_id=r["partner_txn_id"], tx_sequence_number=r.get("tx_sequence"),
            file_amount=r["file_amount"], sap_amount=r["sap_amount"],
            amount_diff=r["amount_diff"], txn_date=r.get("txn_date"),
            gl_account=r.get("gl_account"), source_channel=r.get("source_channel"),
            match_type=r["match_type"], action=act, comment=comment,
            recon_status=row_status, confidence=confidence,
        ))
        store.write(EvidenceRecord(
            run_id=run_id, bank_name=bank_name, partner_txn_id=r["partner_txn_id"],
            key_used="partner_txn_id", file_amount=r["file_amount"], sap_amount=r["sap_amount"],
            diff=r["amount_diff"], match_type=r["match_type"], rule_fired=r["match_type"].value,
            action=act, comment=comment, confidence=confidence,
            status=r["match_type"].value, ts=ts,
        ))
    return results, store


def _summary(results: list[ReconResultRow], snap: BalanceSnapshot) -> dict:
    by_type: dict[str, int] = {}
    for r in results:
        by_type[r.match_type.value] = by_type.get(r.match_type.value, 0) + 1
    return {
        "total_rows": len(results),
        "by_match_type": by_type,
        "file_total": snap.file_total,
        "sap_total": snap.sap_total,
        "balance_diff": snap.balance_diff,
        "recon_status": snap.recon_status.value,
    }
