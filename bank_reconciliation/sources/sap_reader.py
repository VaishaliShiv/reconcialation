"""Read SAP-side rows.

Modes:
  fixture            -> local JSON (tests / offline)
  cosmos             -> read the `bank-sap-source` container (current setup)
  api (SAP_READ_MODE)-> SAP team's API by date set, onlyUnmatched=true (later)
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import settings

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def read_sap_rows(bank_name: str, dates: list[str], identifier: str | None = None) -> list[dict]:
    if settings.source_mode == "fixture":
        path = FIXTURE_DIR / f"{bank_name.lower()}_sap.json"
        return json.loads(path.read_text())
    if settings.sap_read_mode == "api":
        return _from_api(identifier or bank_name, dates)
    return _from_cosmos(bank_name, dates)


def _from_cosmos(bank_name: str, dates: list[str]) -> list[dict]:
    from azure.cosmos import CosmosClient  # lazy import

    client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
    container = (client.get_database_client(settings.cosmos_database)
                       .get_container_client(settings.cosmos_sap_container))
    # SAP docs tag the bank in `source`; date filter moves to the SAP API later
    query = "SELECT * FROM c WHERE c.source=@b"
    params = [{"name": "@b", "value": bank_name}]

    rows: list[dict] = []
    for item in container.query_items(query=query, parameters=params,
                                      enable_cross_partition_query=True,
                                      max_item_count=1000):
        rows.append(item)
    return rows


def _from_api(identifier: str, dates: list[str]) -> list[dict]:
    import httpx  # lazy import

    headers = {"Authorization": f"Bearer {settings.sap_api_token}"}
    payload = {"identifier": identifier, "dates": dates, "onlyUnmatched": True}
    resp = httpx.post(f"{settings.sap_api_base}/recon/transactions",
                      json=payload, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.json().get("transactions", [])
