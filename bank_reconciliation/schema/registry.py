"""Template registry: bankName -> FieldMapping.

Seeded from verified bank samples; new banks are onboarded manually by adding
a FieldMapping and saving it here. A registry miss stops reconciliation with a
NO_TEMPLATE error (no auto-onboarding — deterministic only).
"""
from __future__ import annotations

from ..models import FieldMapping

# ── SAP-side mapping (one fixed schema the SAP API returns) ──
SAP_MAPPING = FieldMapping(
    bank_name="__SAP__",
    source_type="sap",
    field_map={
        "partner_txn_id": "Selection Value 2",
        "tx_sequence": "Sold-To Party",
        "amount": "Payment Amount",
        "txn_date": "Post. Date",
        "gl_account": "Clrg acct",
        "source_channel": "Additional info",
    },
    date_format="epoch_ms",    # Post. Date is epoch milliseconds in Cosmos
    strip_prefix="0",          # harmless; tolerates any leading-zero refs
)

# ── File-side mappings, keyed by bankName (verified from real M-bank files) ──
_SEED: dict[str, FieldMapping] = {
    "mbank": FieldMapping(
        bank_name="mbank",
        source_type="bank",
        source_columns=[
            "TxSequenceNumber", "Amount", "Datrequest",
            "Payment Reference ", "Type Of Transaction", "Type", "Source",
        ],
        field_map={
            "partner_txn_id": "Payment Reference ",   # NOTE trailing space in real key
            "tx_sequence": "TxSequenceNumber",
            "amount": "Amount",
            "txn_date": "Datrequest",
            "status": "Type",
            "source_channel": "Source",
        },
        date_format="%d/%m/%Y %H:%M",
        key_field="partner_txn_id",
        confidence=1.0,
        source="manual",
    ),
}


class Registry:
    """Template store. Cosmos-backed in live mode; in-memory seed otherwise.

    get()  -> active mapping for a bank (Cosmos first, then None on a miss).
    save() / save_draft() -> persist a mapping (active vs draft) to Cosmos.
    """

    def __init__(self, mappings: dict[str, FieldMapping] | None = None):
        from ..config import settings

        self.use_cosmos = settings.source_mode == "cosmos" and bool(settings.cosmos_key)
        self._cache: dict[str, FieldMapping] = {}
        self._container = None
        if mappings is not None:
            self._cache = dict(mappings)
        elif self.use_cosmos:
            self._connect(settings)
        else:
            self._cache = dict(_SEED)   # offline / fixture / tests

    def _connect(self, settings) -> None:
        from azure.cosmos import CosmosClient

        client = CosmosClient(settings.cosmos_endpoint, settings.cosmos_key)
        self._container = (client.get_database_client(settings.cosmos_database)
                                 .get_container_client(settings.cosmos_registry_container))

    def get(self, bank_name: str) -> FieldMapping | None:
        if bank_name in self._cache:
            return self._cache[bank_name]
        if self.use_cosmos:
            items = list(self._container.query_items(
                "SELECT * FROM c WHERE c.bank_name=@b AND c.active=true",
                parameters=[{"name": "@b", "value": bank_name}],
                enable_cross_partition_query=True))
            if items:
                m = FieldMapping(**items[0])
                self._cache[bank_name] = m
                return m
        return None

    def save(self, mapping: FieldMapping) -> None:
        mapping.active = True
        self._persist(mapping)

    def save_draft(self, mapping: FieldMapping) -> None:
        mapping.active = False
        self._persist(mapping)

    def _persist(self, mapping: FieldMapping) -> None:
        if mapping.active:
            self._cache[mapping.bank_name] = mapping
        if self.use_cosmos:
            doc = mapping.model_dump(mode="json")
            doc["id"] = mapping.bank_name
            self._container.upsert_item(doc)

    def sap_mapping(self) -> FieldMapping:
        return SAP_MAPPING
