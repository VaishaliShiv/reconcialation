# Reconciliation Matching Rules

How an **email-source** bank record is matched against a **SAP** record. Deterministic —
no AI. This is the agreed business rule for the matcher.

## Reference columns (which field maps to which)

| Reference | Email source column | SAP column |
|-----------|--------------------|------------|
| Partner   | `Partner_Trn_Reference_No` | `partnertransactionid` |
| Payment   | `Payment_Ref_No` | `paymentreferencenumber` |
| DEWA      | `DEWATrn_Reference_No` | `dewatransactionid` |

Amount: `Trn_Amount` ↔ `amount`  ·  Date: `Trn_Date` ↔ `transactiondate` (compared day-level).

## The core rule — driven by what the EMAIL record contains

**For every reference present in the email record, that reference MUST equal SAP's
corresponding field.** All present references must agree (AND). Then amount and date must
also match. Only then is the record `matched`.

| References present in the email row | Requirement to be `matched` |
|-------------------------------------|------------------------------|
| **One key only** (e.g. only Payment) | that one reference must match SAP |
| **Partner + Payment** | **both** must match SAP |
| **Partner + Payment + DEWA** (all three) | **all three** are mandatory and must match SAP |
| **DEWA present** (with any others) | DEWA must also match SAP |

If **any** reference that the email row carries does **not** agree with SAP → the record is
an **anomaly** (not `matched`).

### References not present in the email are SKIPPED

Only the references that actually appear in the email row are checked. A reference the email
does **not** carry is ignored — it is **not** required to match.

| Email row has… | What is checked against SAP |
|----------------|------------------------------|
| Partner only | Partner only (payment + DEWA skipped) |
| Payment only | Payment only (partner + DEWA skipped) |
| Partner + Payment (no DEWA) | Partner **and** Payment — **DEWA skipped** |
| Partner + Payment + DEWA | Partner **and** Payment **and** DEWA (all three) |

Example: if the file has **only Partner and Payment** and **no DEWA reference**, the matcher
checks Partner and Payment against SAP and **skips the DEWA reference entirely** — a missing
DEWA does not cause a mismatch.

## Full match criteria (all must hold)

A record is **`matched`** only when:
1. **References** — every reference present in the email row equals SAP's corresponding field.
2. **Amount** — `Trn_Amount` equals SAP `amount`.
3. **Date** — `Trn_Date` and SAP `transactiondate` are the **same day**.

Otherwise it is classified as one of the outcomes below.

## Outcome classifications

| Classification | Meaning | reconflag | remarks (SAP) |
|----------------|---------|-----------|----------------|
| `matched` | all references + amount + date agree | `MATCHED` | Reconciled with partner ledger |
| `amount_mismatch` | references agree, amount differs | `ANOMALY` | Amount mismatch |
| `date_mismatch` | references + amount agree, date differs | `ANOMALY` | Date mismatch |
| `missing_in_sap` | reference is in the email but not in SAP (or a required ref disagrees) | — (no SAP row to write) | reported in summary only |
| `missing_in_file` | reference is in SAP but not in the email | `ANOMALY` | Not in bank file |
| `duplicate` | more than one SAP row for the same reference | `ANOMALY` | Duplicate posting |
| `invalid_record` | email row failed validation (no reference / bad amount / bad date) | not written | Invalid record |

## Write-back behaviour

- **`reconflag`** is binary: `MATCHED` if fully matched, otherwise `ANOMALY`.
- A SAP column is only stamped if that transaction **exists in SAP**. `missing_in_sap`
  records have no SAP row, so they are reported in the summary but not written to SAP.
- `missing_in_file` records **do** exist in SAP, so they are stamped `ANOMALY` / "Not in bank file".

## Notes / open points

- **Missing date on one side:** currently tolerated (a blank date does not block a match).
  Change to strict (blank date = anomaly) only if the business requires it.
- **Precedence vs AND:** the rule above is the AND rule (all email-present references must
  agree). The join first pairs records on any shared reference, then validates that every
  email-present reference agrees.
