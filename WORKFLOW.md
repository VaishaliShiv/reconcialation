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

---

## 2. How the SAP file is named (vendorId + date)

The SAP READ API is called **per date**. Each response is stored as **one document** in the
SAP container, with a unique id built from **vendor + date**:

```
id = "<vendorid>_<datevalue>"      e.g.  nbd1Qmid_20260623
datevalue = datetime.datetimelist.datevalue  (YYYYMMDD)
```

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
