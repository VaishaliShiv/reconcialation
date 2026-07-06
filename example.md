# Worked Example — Shared-Date Reconciliation & Write-Back

A concrete walkthrough of what happens when **two email files share a settlement/transaction
date** and SAP delivers **one file per date**. Shows the write-back problem, the fix, and how
every match rule behaves. All examples are the real deterministic engine output.

---

## Setup

Vendor `V`. Two email files both contain transactions dated **2026-06-23**, so they share the
one SAP date-doc `V_20260623`.

**Email file A** (`id = A`):

| ref | amount | date | expected |
|-----|--------|------|----------|
| R-100 | 300 | 2026-06-23 | matched |
| R-200 | 550 | 2026-06-23 | amount_mismatch (SAP has 500) |
| R-888 | 50  | 2026-06-23 | missing_in_sap (not in SAP) |

**Email file B** (`id = B`):

| ref | amount | date | expected |
|-----|--------|------|----------|
| R-300 | 700 | 2026-06-23 | matched |
| R-400 | 400 | 2026-06-23 | duplicate (two SAP rows) |

**SAP doc** `V_20260623` (one doc for the date, holding BOTH files' rows + one extra):

| partnertransactionid | amount | belongs to |
|----------------------|--------|------------|
| R-100 | 300 | file A |
| R-200 | 500 | file A |
| R-300 | 700 | file B |
| R-400 | 400 | file B |
| R-400 | 400 | file B (duplicate posting) |
| R-999 | 999 | **no email file** (genuine missing_in_file) |

---

## The problem — blind write-back clobbers

The naive write-back stamps **every** SAP row from the reconcile, including rows the file
doesn't own (`missing_in_file`). Because each email file only knows its own transactions:

```
process file A:  R-100 -> MATCHED ,  R-200 -> ANOMALY ,  R-300/R-400/R-999 -> ANOMALY "Not in bank file"
process file B:  R-300 -> MATCHED ,  R-400 -> ANOMALY ,  R-100/R-200/R-999 -> ANOMALY "Not in bank file"
```

**Result:** file B overwrites file A's `R-100 = MATCHED` with `ANOMALY "Not in bank file"`.
A perfectly reconciled transaction is now falsely flagged — purely due to processing order.
**Overwriting / re-pulling the SAP doc does NOT fix this** — a fresh pull returns
`reconflag = null` and just re-clobbers.

---

## The fix — scoped write-back (stamp only the rows THIS file owns)

Rule: a SAP row is stamped **only if this email file has a transaction that references it**:

- `matched` → `MATCHED`
- `amount_mismatch` / `date_mismatch` / `duplicate` → `ANOMALY` (with reason)
- `missing_in_file` (a SAP row this file does not reference) → **leave untouched**
- `missing_in_sap` / `invalid_record` → no SAP row exists → report-only (nothing to stamp)

Plus: the SAP date-doc is a **persistent accumulator** — created once per date, **not
overwritten** on later files, so reconflags from different files accumulate.

### Result (real engine output)

```
process file A:  R-100 -> MATCHED , R-200 -> ANOMALY(Amount mismatch) , R-888 -> missing_in_sap (report only)
process file B:  R-300 -> MATCHED , R-400 -> ANOMALY(Duplicate posting)

FINAL SAP doc V_20260623:
    R-100   300   -> MATCHED  'Reconciled with partner ledger'
    R-200   500   -> ANOMALY  'Amount mismatch'
    R-300   700   -> MATCHED  'Reconciled with partner ledger'
    R-400   400   -> ANOMALY  'Duplicate posting'
    R-400   400   -> ANOMALY  'Duplicate posting'
    R-999   999   -> (null)   <-- genuine missing_in_file, see note
```

`R-100` and `R-200` (file A's rows) **survive** file B's run — no clobbering, order-independent.

---

## Coverage of every match rule

| Match type | Example | Per-file result | Correct? |
|------------|---------|-----------------|----------|
| matched | R-100 | `MATCHED` | ✅ |
| amount_mismatch | R-200 (550 vs 500) | `ANOMALY "Amount mismatch"` | ✅ |
| date_mismatch | (same mechanism) | `ANOMALY "Date mismatch"` | ✅ |
| missing_in_sap | R-888 (not in SAP) | report-only (no SAP row) | ✅ |
| duplicate | R-400 ×2 | `ANOMALY "Duplicate posting"` | ✅ |
| invalid_record | bad row | report-only (no SAP row) | ✅ |
| missing_in_file | R-999 (in SAP, in no file) | **not flagged per-file** | ⚠️ needs batch |

---

## The one gap — `missing_in_file`

A SAP posting that **no email file** reported (`R-999`) cannot be judged from a single file —
a single file can't know it's missing from *all* files. Per-file mode therefore leaves it
unflagged (this is exactly what prevents the clobbering).

To flag genuine `missing_in_file`, run an **end-of-day batch per date**: reconcile *all* email
files for that date together against the SAP date-doc. Then a SAP row present in **no** file is
a real anomaly and safe to stamp.

---

## Ingestion rules (SAP pull side)

1. **One document per (vendor, date)**, id = `vendorid_datevalue` (e.g. `V_20260623`).
2. **Upsert by that id** — the same date coming from a second email updates the **same** doc,
   never a duplicate file.
3. **Do not overwrite a date-doc's already-reconciled rows** — merge new transactions in;
   never wipe existing `reconflag`/`remarks`. (Or keep reconflag out of the read mirror and
   push it to SAP via the WRITE API — then the mirror can refresh freely.)

## Summary of the two safety rules

- **(a) Accumulate, don't overwrite** the SAP date-doc.
- **(b) Each email file writes only the rows it owns** (skip `missing_in_file`).

Both together give correct, order-independent detection for shared-date processing.
