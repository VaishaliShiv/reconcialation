"""Read file-side rows (Yasa's parsed bank files).

fixture mode: local JSON (no creds, runs anywhere).
cosmos mode: live azure-cosmos, paged + partitioned by bankName.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import settings

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def read_file_rows(bank_name: str, dates: list[str] | None = None) -> list[dict]:
    if settings.source_mode == "fixture":
        return _from_fixture(bank_name)
    return _from_cosmos(bank_name, dates)


def _from_fixture(bank_name: str) -> list[dict]:
    path = FIXTURE_DIR / f"{bank_name.lower()}_file.json"
    return json.loads(path.read_text())


def _from_cosmos(bank_name: str, dates: list[str] | None) -> list[dict]:
    from azure.cosmos import CosmosClient  # lazy import — only when live

    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    container = (client.get_database_client(settings.cosmos_database)
                       .get_container_client(settings.cosmos_file_container))
    # file docs tag the bank in `Source` (e.g. "mbank")
    query = "SELECT * FROM c WHERE c.Source=@b"
    params = [{"name": "@b", "value": bank_name}]

    rows: list[dict] = []
    for item in container.query_items(query=query, parameters=params,
                                      enable_cross_partition_query=True,
                                      max_item_count=1000):
        rows.append(item)
    return rows
