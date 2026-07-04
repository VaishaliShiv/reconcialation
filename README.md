# Cosmos Reconciliation Workflow — portable runner

Self-contained copy of the reconciliation workflow, to run **from the DEWA network**
(where the Cosmos + SAP endpoints resolve). Reads bank file + SAP from Cosmos, runs the
deterministic compare, optional AI triage/summary, and (opt-in) writes back.

## 1. Setup (on the company laptop)

```bash
cd cosmos_workflow
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Credentials

Create a `.env` **in this folder** (config reads `cosmos_workflow/.env`). Copy your existing
`.env` here, or start from `.env.example` and fill it. Required keys:

```dotenv
COSMOS_ENDPOINT=https://....documents.azure.com:443/   # note: port 443
COSMOS_KEY=...
COSMOS_DATABASE=BankRecon-DB
COSMOS_FILE_CONTAINER=bank_email_source
COSMOS_SAP_CONTAINER=bank-sap-source
COSMOS_RESULTS_CONTAINER=summary        # the summary container
SOURCE_MODE=cosmos                      # IMPORTANT: use live Cosmos, not fixtures

# AI (optional — only if you want the AI explanation + summary text)
TRIAGE_ENABLED=true
LLM_PROVIDER=openai                     # openai | azure
OPENAI_API_KEY=sk-...
TRIAGE_MODEL=gpt-4o-mini
```

> `.env` is git-ignored. Never commit it.

## 3. Run — safe first, writes later

**Step 1 — READ-ONLY dry run (writes nothing).** Confirms connectivity, prints the live
column names of both containers, and shows the full reconciliation + what *would* be written:

```bash
python run_cosmos_workflow.py MBANK
```
- If most rows come back `invalid_record`, the field names in Cosmos don't match the mapper
  (see the printed columns) — fix that before enabling writes.

**Step 2 — enable writes once the dry run looks right:**

```bash
# write reconflag + remarks back into the SAP docs
python run_cosmos_workflow.py MBANK 2026-07-03 --write-sap

# also upsert the run summary (written LAST; idempotent per vendor+day)
python run_cosmos_workflow.py MBANK 2026-07-03 --write-sap --write-summary
```

### Flags
| Flag | Effect |
|---|---|
| *(none)* | READ-ONLY dry run — writes nothing |
| `--write-sap` | upsert `reconflag` + `remarks` into SAP docs |
| `--write-summary` | upsert the summary doc (written **last**) |
| `--all-sap` | don't date-scope SAP; use every SAP row for the vendor |
| `--force` | ignore the "summary already exists" idempotency guard |

## 4. What it writes

- **SAP** (`bank-sap-source`): `reconflag` = `MATCHED`/`ANOMALY`, `remarks` = short deterministic note.
- **Summary** (`summary`): one doc per `vendor_id:date` — `id, vendor_id, date, status, run_id,
  summary` (AI text), `generated_at`. Written **last**; idempotent (re-run overwrites).

## Assumptions to confirm on real data
1. **Field keys** in `bank_email_source` match the mapper (`Partner_Trn_Reference_No`,
   `Payment_Ref_No`, `DEWA_Trn_Reference_No`, `Trn_Amount`, `Trn_Date`, `Type`,
   `Settlement_Date`, `Upload_Date`, `Status`). The dry run prints the real columns.
2. **`Type` present** on file rows (else they fail validation → `invalid_record`).
3. **SAP write is idempotent** (writing the same reconflag twice is harmless) — needed for `--write-sap`.
