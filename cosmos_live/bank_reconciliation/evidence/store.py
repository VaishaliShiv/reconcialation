"""Append-only evidence/audit. The proof trail that the 2019 attempt lacked.

fixture mode: append to a local JSONL file. cosmos mode: write to a container.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import settings
from ..models import EvidenceRecord

LOG_DIR = Path(__file__).resolve().parents[2] / "evidence_log"


class EvidenceStore:
    def __init__(self):
        self._records: list[EvidenceRecord] = []

    def write(self, rec: EvidenceRecord) -> None:
        self._records.append(rec)

    def flush(self) -> int:
        if settings.source_mode == "cosmos":
            return self._to_cosmos()
        LOG_DIR.mkdir(exist_ok=True)
        out = LOG_DIR / "evidence.jsonl"
        with out.open("a") as f:
            for r in self._records:
                f.write(json.dumps(r.model_dump(mode="json"), default=str) + "\n")
        n = len(self._records)
        self._records.clear()
        return n

    def _to_cosmos(self) -> int:
        from azure.cosmos import CosmosClient  # lazy

        client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
        container = (client.get_database_client(settings.cosmos_database)
                           .get_container_client(settings.cosmos_evidence_container))
        n = 0
        for r in self._records:
            doc = r.model_dump(mode="json")
            doc["id"] = f"{r.run_id}:{r.partner_txn_id}"
            container.upsert_item(doc)
            n += 1
        self._records.clear()
        return n
