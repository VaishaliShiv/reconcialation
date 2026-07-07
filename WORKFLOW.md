# Complete Reconciliation Workflow — Simple Guide

How an email file is compared to SAP, which dates are used, how SAP files are named, and how
each transaction is matched. All results below are the real deterministic engine output.

---

## 1. The reference fields and the date

Each transaction carries up to **three reference numbers** plus an **amount** and a **date**:

| Meaning | Email field | SAP field |
|---------|-------------|-----------|
| Partner | `partnerTrnReferenceNo` | `partnertransactionid` |
| Payment | `paymentRefNo` | `paymentreferencenumber` |
| DEWA | `dewaTrnReferenceNo` | `dewatransactionid` |
| Amount | `trnAmount` | `amount` |
| Date | `trnDate` | `transactiondate` |

**Which date is used, and where:**
- **To pick the SAP file(s):** the dates found in the email file's transactions (`trnDate` +
  `settlementDate`). One email file can span several dates.
- **To confirm a match:** the email `trnDate` must equal the SAP `transactiondate` (same day).
- **The join itself does NOT use the date** — it joins on the reference numbers; the date is a
  check applied *after* a reference match.
- **The email file's top-level `uploadDate` is NOT used** to pick SAP files — only the
  per-transaction `trnDate` + `settlementDate` are. (The file may be uploaded on a day totally
  different from the transactions it carries.)

**What each date does (at a glance):**

| Date | Side | Role |
|------|------|------|
| email `uploadDate` | email file | **ignored** for pairing and matching |
| email `trnDate` | email txn | the per-row **same-day** check vs SAP `transactiondate` |
| email `settlementDate` | email txn | contributes to **auto-pairing** (which SAP file) — bypassed by `--all-sap` |
| SAP `uploadDate` / `datevalue` | SAP file | **names** the SAP file's date so vendor+date scoping finds it |

So a normal run pairs by `trnDate`+`settlementDate`, and the match itself hinges on
`trnDate` == SAP `transactiondate`.

---

## 2. How the SAP file is named (vendorId + date)

The SAP READ API is called **per date**. Each response is stored as **one document** in the
SAP container, with a unique id built from **vendor + date**:

```
id = "<vendorid>_<datevalue>"      e.g.  nbd1Qmid_20260623
```

**How the SAP file's date is read** (the runner tolerates all three formats we've seen, in
this order):
1. `datetime.datetimelist.datevalue`  → `20260623`  (SAP READ API shape)
2. top-level `uploadDate`             → `2026-05-20` → `20260520`  (current Cosmos source shape)
3. trailing 8 digits of the `id`      → `hbb1Qmid-20260520` → `20260520`  (last-resort fallback)

The vendor is read as `vendorid` **or** `vendorId` (either casing). This is what makes
vendor+date scoping (`--dates`) match your SAP docs.

- One date = one document. The same date coming again updates the **same** document (upsert),
  never a duplicate.
- The document holds all SAP transactions for that date in a `transaction[]` array.

---

## 3. How an email transaction is mapped to a SAP file and searched

For each email transaction:

```
1. Read its date (trnDate)         e.g. 2026-06-23  -> datevalue 20260623
2. Open the SAP file(s) for the email file's dates:  nbd1Qmid_20260623 (+ nbd1Qmid_20260624 …)
3. Inside those SAP files, SEARCH for a SAP row whose references match — using the AND rule:
      every reference the email row HAS must equal SAP's field
      (partner==partner AND payment==payment AND dewa==dewa, for the refs present)
4. If found -> check amount (to the cent) and date (same day) -> classify
   If not found -> missing_in_sap
```

The search is by **reference number**, scoped to the SAP file(s) for the email file's dates.

---

## 4. Example data (one email file, all scenarios)

**Email file** `EF-001` — vendorId `nbd1Qmid`, uploadDate `2026-06-24`:

| # | partner | payment | dewa | amount | trnDate | status | intended |
|---|---------|---------|------|--------|---------|--------|----------|
| 1 | P-001 | PAY-001 | DEWA-001 | 300 | 2026-06-23 | In Progress | matched |
| 2 | P-002 | PAY-002 | DEWA-002 | 500 | 2026-06-23 | In Progress | amount_mismatch |
| 3 | P-003 | PAY-003 | DEWA-003 | 200 | 2026-06-23 | In Progress | date_mismatch |
| 4 | P-004 | PAY-004 | DEWA-004 | 150 | 2026-06-24 | In Progress | missing_in_sap |
| 5 | P-005 | PAY-005 | DEWA-005 | 400 | 2026-06-24 | **Completed** | skipped |
| 6 | *(none)* | *(none)* | *(none)* | 100 | 2026-06-24 | In Progress | invalid_record |
| 7 | P-007 | PAY-007 | DEWA-007 | 700 | 2026-06-24 | In Progress | matched |
| 8 | P-007 | PAY-007 | DEWA-007 | 700 | 2026-06-24 | In Progress | duplicate |

**SAP file** `nbd1Qmid_20260623` (datevalue 20260623):

| partner | payment | dewa | amount | transactiondate |
|---------|---------|------|--------|-----------------|
| P-001 | PAY-001 | DEWA-001 | 300 | 2026-06-23 |
| P-002 | PAY-002 | DEWA-002 | **550** | 2026-06-23 |
| P-999 | PAY-999 | DEWA-999 | 999 | 2026-06-23 | *(no email row)* |

**SAP file** `nbd1Qmid_20260624` (datevalue 20260624):

| partner | payment | dewa | amount | transactiondate |
|---------|---------|------|--------|-----------------|
| P-003 | PAY-003 | DEWA-003 | 200 | **2026-06-24** |
| P-007 | PAY-007 | DEWA-007 | 700 | 2026-06-24 |

The email file spans dates 23 + 24, so it pairs to **both** SAP files.

---

## 5. Result (real engine output)

| # | ref | file_amt | sap_amt | classification | why |
|---|-----|----------|---------|----------------|-----|
| 5 | P-005 | — | — | *(skipped)* | status = Completed |
| 6 | (none) | 100 | — | `invalid_record` | no reference number |
| 1 | P-001 | 300 | 300 | `matched` | refs + amount + date agree |
| 2 | P-002 | 500 | 550 | `amount_mismatch` | refs agree, amount differs |
| 3 | P-003 | 200 | 200 | `date_mismatch` | refs + amount agree, date 23 ≠ 24 |
| 4 | P-004 | 150 | — | `missing_in_sap` | ref not in any SAP file |
| 7 | P-007 | 700 | 700 | `matched` | full match |
| 8 | P-007 | 700 | 700 | `duplicate` | same ref already matched by #7 |
| — | P-999 | — | 999 | `missing_in_file` | in SAP, in no email row |

### Write-back into the SAP files (scoped — only rows this file owns)

```
nbd1Qmid_20260623:  P-001 -> MATCHED   P-002 -> ANOMALY (Amount mismatch)   P-999 -> (untouched)
nbd1Qmid_20260624:  P-003 -> ANOMALY (Date mismatch)   P-007 -> ANOMALY (Duplicate posting)
```

- `reconflag` = `MATCHED` if fully matched, else `ANOMALY`.
- `missing_in_sap` (P-004) has no SAP row → nothing to write (report-only).
- `missing_in_file` (P-999) is left untouched per-file → flagged only by an end-of-day batch.

---

## 6. The complete workflow (simple steps)

```
1. Email file lands in Cosmos (one doc, transactions[] , unique id = file name).
2. Read the file's transaction dates  ->  e.g. {20260623, 20260624}.
3. SAP READ API is called per date  ->  store one SAP doc per date:
       nbd1Qmid_20260623 , nbd1Qmid_20260624   (id = vendorid_datevalue, upsert)
4. Reconcile: for each email transaction (skip 'Completed'):
       - search the paired SAP files for a row whose references match (AND rule)
       - check amount (to the cent) and date (same day)
       - classify: matched / amount_mismatch / date_mismatch /
                   missing_in_sap / duplicate / invalid_record
       (SAP rows referenced by NO email row -> missing_in_file)
5. Write-back (opt-in):
       - stamp reconflag + remarks into ONLY the SAP rows this file references
       - summary doc (one per email file, id = file name) -> summary container
```

## Key rules in one line each
- **Match on references** (partner→payment→DEWA, all present ones must agree); amount and date are checks after.
- **SAP files are per date**, id = `vendorid_datevalue`, upserted (one doc per date).
- **An email file pairs to every SAP file whose date it contains.**
- **Write-back stamps only the rows the email file owns** (never another file's rows).
- **Skip `Completed`** email transactions; flag `invalid_record` for rows with no reference.

---

## 7. Testing ONE email file against the SAP file(s) you choose

You don't hand the runner the SAP file by name. You name **the email file by its id** and the
**SAP file(s) by vendorId + date** — because a SAP file's name *is* `vendorid_datevalue`.

| Input | Flag | Example |
|-------|------|---------|
| The one email file | `--email-id` | `--email-id file-001-20260629` |
| The SAP file(s) | `VENDOR_ID` arg + `--dates` (YYYYMMDD, comma-sep) | `nbd1Qmid --dates 20260629,20260630` |
| Compare against EXACTLY those SAP files | `--all-sap` | (skip date auto-scoping) |
| Read-only preview (writes nothing) | `--dry-run` | |

```bash
# one email file  ->  ONE sap file
python run_cosmos_workflow.py nbd1Qmid --email-id file-001-20260629 --dates 20260629 --all-sap --dry-run

# one email file  ->  MULTIPLE sap files (same vendor, several dates)
python run_cosmos_workflow.py nbd1Qmid --email-id file-001-20260629 --dates 20260629,20260630 --all-sap --dry-run

# one email file, let the code pick the SAP file(s) from the file's own dates
python run_cosmos_workflow.py nbd1Qmid --email-id file-001-20260629 --dry-run
```

- With `--email-id`, only that email doc is read (`SELECT * FROM c WHERE c.id=@id`) — cheap to
  re-run while testing.
- `--dates` scopes SAP to those dates for the vendor. **With** `--all-sap` the email file is
  compared to *exactly* those SAP files; **without** it, they must also overlap the email file's
  own dates (the normal auto-pairing).
- The per-file line prints `sap_docs=N sap_txns=M` — check it to confirm you paired to **one**
  SAP file or **several** before trusting the classifications.
- When the dry-run output looks right, drop `--dry-run` and add `--write-sap` (and
  `--write-summary`) to stamp results back.
