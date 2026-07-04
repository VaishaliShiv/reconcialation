"""Persist the run SUMMARY to Cosmos — container = COSMOS_RESULTS_CONTAINER (now 'summary').

One doc per (vendor_id, email-arrival day): one bank sends one file per day, so that pair
is unique. Idempotent upsert keyed by that id — re-running the same day overwrites in place
and preserves the original created_at. Partition key assumed to be /vendor_id.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import settings


def _container():
    from azure.cosmos import CosmosClient  # lazy import — only when live

    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    return (client.get_database_client(settings.cosmos_database)
                  .get_container_client(settings.cosmos_results_container))


def upsert(doc: dict) -> str:
    """Upsert one summary doc. Returns its id. Stable id → idempotent per vendor/day."""
    cont = _container()
    now = datetime.now(timezone.utc).isoformat()
    doc = dict(doc)
    doc.setdefault("id", f"{doc['vendor_id']}:{doc['date']}")
    doc["updated_at"] = now
    try:                                   # keep original created_at on re-run
        existing = cont.read_item(item=doc["id"], partition_key=doc["vendor_id"])
        doc["created_at"] = existing.get("created_at", now)
    except Exception:                      # noqa: BLE001  (new doc)
        doc["created_at"] = now
    cont.upsert_item(doc)
    return doc["id"]
