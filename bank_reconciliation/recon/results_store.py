"""Persist the Reconciliation Result Dataset to Cosmos — current-truth, mutable.

Upsert keyed by a STABLE id ("{bank}:{settlement_date}:{partner_txn_ref}") so
re-runs and carry-forward update the same row in place (idempotent). This is
SEPARATE from the immutable `evidence` container.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from ..config import settings


def _id(row: dict) -> str:
    return f"{row['bank_name']}:{row['settlement_date']}:{row['partner_txn_ref']}"


def _jsonable(v):
    return v.isoformat() if isinstance(v, (date, datetime)) else v


def _container():
    from azure.cosmos import CosmosClient  # lazy import

    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    return (client.get_database_client(settings.cosmos_database)
                  .get_container_client(settings.cosmos_results_container))


def upsert(rows: list[dict]) -> int:
    """Upsert result rows. Stable id → idempotent; preserves first created_at."""
    cont = _container()
    now = datetime.now(timezone.utc).isoformat()
    n = 0
    for row in rows:
        doc = {k: _jsonable(v) for k, v in row.items()}
        doc["id"] = _id(row)
        doc["updated_at"] = now
        try:                                   # keep original created_at on update
            existing = cont.read_item(item=doc["id"], partition_key=row["bank_name"])
            doc["created_at"] = existing.get("created_at", now)
            doc["previous_classification"] = existing.get("classification")
        except Exception:                      # noqa: BLE001  (new row)
            doc["created_at"] = now
        cont.upsert_item(doc)
        n += 1
    return n


def read(bank_name: str, limit: int = 100) -> list[dict]:
    return list(_container().query_items(
        f"SELECT * FROM c WHERE c.bank_name=@b OFFSET 0 LIMIT {int(limit)}",
        parameters=[{"name": "@b", "value": bank_name}],
        enable_cross_partition_query=True))
