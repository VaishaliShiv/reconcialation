"""Pydantic schemas — one source of truth for every record shape."""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class MatchType(str, Enum):
    MATCHED = "matched"
    MISSING_IN_SAP = "missing_in_sap"
    MISSING_IN_FILE = "missing_in_file"
    DUPLICATE = "duplicate"
    AMOUNT_MISMATCH = "amount_mismatch"
    DATE_MISMATCH = "date_mismatch"
    INVALID_RECORD = "invalid_record"


class Action(str, Enum):
    NONE = "none"
    POST = "post"
    REVERSE = "reverse"
    REPOST = "repost"
    RETURN_TO_BANK = "return_to_bank"


class ReconStatus(str, Enum):
    AUTO_RECONCILED = "auto_reconciled"
    NON_RECONCILED = "non_reconciled"
    FULLY_RECONCILED = "fully_reconciled"


class CanonicalTxn(BaseModel):
    """Normalized transaction — every template maps into this shape."""
    source_type: str
    bank_name: str
    partner_txn_id: str
    tx_sequence: str | None = None
    our_txn_id: str | None = None
    amount: float | None = None
    txn_date: date | None = None
    payment_ref: str | None = None
    status: str | None = None
    gl_account: str | None = None
    source_channel: str | None = None
    # --- new canonical contract fields (see MEMORY.md §10) ---
    payment_ref_no: str | None = None      # contract acct / easypay ref
    dewa_txn_ref: str | None = None        # DEWA channel txn id (SAP DEWATN)
    txn_type: str | None = None            # Bill/Moveln/Estimate
    settlement_date: date | None = None    # file generated date
    upload_date: date | None = None        # from email
    details: str | None = None             # passthrough
    match_key: str | None = None           # resolved dynamic join value
    match_kind: str | None = None          # partner | payment | dewa | txnid
    valid: bool = True                     # passes the 4-field validation
    invalid_reason: str | None = None      # why it failed (-> invalid_record)
    raw_row: dict = Field(default_factory=dict)


class FieldMapping(BaseModel):
    """Stored per template (registry). The discovered schema for a bank."""
    bank_name: str
    version: int = 1
    active: bool = True
    source_type: str = "bank"
    source_columns: list[str] = Field(default_factory=list)
    field_map: dict[str, str] = Field(default_factory=dict)   # canonical -> source col
    date_format: str | None = None
    decimal: str = "."
    strip_prefix: str | None = None       # e.g. leading "00" on SAP side
    one_to_many: bool = False             # ENBD: 1 txn -> N accounts, sum amount
    key_field: str = "partner_txn_id"     # bank=partner_txn_id; channel=our_txn_id
    drop_rows_without_key: bool = True     # drops footer / Total row
    confidence: float = 1.0
    source: str = "manual"                # manual | llm_inferred
    created_at: str | None = None


class ReconResultRow(BaseModel):
    run_id: str
    bank_name: str
    file_date: date | None = None
    partner_txn_id: str
    tx_sequence_number: str | None = None
    our_txn_id: str | None = None
    file_amount: float | None = None
    sap_amount: float | None = None
    amount_diff: float = 0.0
    txn_date: date | None = None
    gl_account: str | None = None
    source_channel: str | None = None
    match_type: MatchType
    action: Action = Action.NONE
    comment: str = ""
    recon_status: ReconStatus = ReconStatus.NON_RECONCILED
    confidence: float = 1.0


class BalanceSnapshot(BaseModel):
    bank_name: str
    recon_date: date | None = None
    gl_account: str | None = None
    file_total: float
    file_count: int
    sap_total: float
    sap_count: int
    balance_diff: float
    recon_status: ReconStatus


class EvidenceRecord(BaseModel):
    run_id: str
    bank_name: str
    file_date: date | None = None
    partner_txn_id: str
    file_row: dict | None = None
    sap_rows: list[dict] = Field(default_factory=list)
    key_used: str
    file_amount: float | None = None
    sap_amount: float | None = None
    diff: float = 0.0
    match_type: MatchType
    rule_fired: str
    action: Action = Action.NONE
    comment: str = ""
    confidence: float = 1.0
    status: str = ""
    ts: datetime | None = None
