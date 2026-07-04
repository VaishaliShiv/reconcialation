"""READ-ONLY date-column audit for the email-source bank container. Writes NOTHING.

Answers: "which date column is safe to build a unique id from?" For every date-like
column it reports coverage (how many rows have a parseable date), whether the raw
values carry a time part (and are therefore day-normalized), how many distinct days
it holds, and whether it is constant within a file (one file == one Upload_Date).

    python check_dates.py MBANK

Reading of the recommendation:
  - FILE / summary id  -> want a column that is 100% populated AND constant per file
                          (currently Upload_Date -> summary id `VENDOR:DATE`).
  - RECORD id          -> want a column that is 100% populated AND varies per row,
                          combined with a ref key (currently Settlement_Date + partner ref).
"""
from __future__ import annotations

import sys

from run_cosmos_workflow import _query                       # noqa: E402
from bank_reconciliation.config import settings              # noqa: E402
from bank_reconciliation.schema.canonical_mapper import parse_date  # noqa: E402


def _all_keys(docs: list[dict]) -> list[str]:
    """Union of keys across ALL docs (not just docs[0]) — Cosmos is schemaless."""
    keys: set[str] = set()
    for d in docs:
        keys.update(k for k in d if not k.startswith("_"))
    return sorted(keys)


def _raw(v) -> str | None:
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _date_columns(docs: list[dict], keys: list[str]) -> list[str]:
    """A column is 'date-like' if its name mentions date OR most non-null values parse."""
    out = []
    for k in keys:
        if "date" in k.lower():
            out.append(k)
            continue
        vals = [_raw(d.get(k)) for d in docs]
        vals = [v for v in vals if v is not None][:50]
        if vals and sum(parse_date(v) is not None for v in vals) / len(vals) >= 0.8:
            out.append(k)
    return out


def _audit_column(docs: list[dict], col: str) -> dict:
    n = len(docs)
    present = parseable = with_time = 0
    days: set[str] = set()
    for d in docs:
        raw = _raw(d.get(col))
        if raw is None:
            continue
        present += 1
        if ("T" in raw) or (":" in raw):
            with_time += 1
        parsed = parse_date(raw)
        if parsed is not None:
            parseable += 1
            days.add(parsed.isoformat())
    return {
        "col": col, "present": present, "parseable": parseable, "with_time": with_time,
        "coverage": parseable / n if n else 0.0, "distinct": len(days),
        "min": min(days) if days else None, "max": max(days) if days else None,
    }


def _print_table(rows: list[dict], n: int) -> None:
    print(f"\ndate-column audit over {n} docs:")
    hdr = f"  {'column':<26}{'coverage':>10}{'parseable':>11}{'distinct':>10}{'has time?':>11}   range"
    print(hdr)
    for r in sorted(rows, key=lambda x: (-x["coverage"], -x["distinct"])):
        cov = f"{r['coverage']*100:.1f}%"
        tim = "yes" if r["with_time"] else "no"
        rng = f"{r['min']} … {r['max']}" if r["min"] else "(none parseable)"
        print(f"  {r['col']:<26}{cov:>10}{r['parseable']:>11}{r['distinct']:>10}{tim:>11}   {rng}")


def _recommend(rows: list[dict], n: int) -> None:
    full = [r for r in rows if r["parseable"] == n and n]
    print("\nrecommendation:")
    if not full:
        best = max(rows, key=lambda x: x["coverage"], default=None)
        if best:
            miss = n - best["parseable"]
            print(f"  ⚠ NO date column is 100% populated. Best is '{best['col']}' "
                  f"({best['coverage']*100:.1f}%, {miss} row(s) missing).")
            print("    Those missing rows can't get a date-based id — treat them as invalid_record "
                  "or fall back to a non-date key.")
        return
    file_id = [r for r in full if r["distinct"] <= max(1, n // 100)]
    rec_id = [r for r in full if r["distinct"] > 1]
    if file_id:
        c = min(file_id, key=lambda x: x["distinct"])
        print(f"  ✓ FILE/summary id  -> '{c['col']}' (100% populated, {c['distinct']} distinct day(s) "
              f"— constant enough to key one summary per file).")
    if rec_id:
        c = max(rec_id, key=lambda x: x["distinct"])
        print(f"  ✓ RECORD id piece  -> '{c['col']}' (100% populated, {c['distinct']} distinct days "
              f"— varies per row; combine with a ref key).")


def main() -> int:
    vendor = next((a for a in sys.argv[1:] if not a.startswith("-")), "MBANK")
    print(f"READ-ONLY date audit — VENDOR={vendor}  SOURCE_MODE={settings.source_mode}")
    print("(this writes NOTHING)\n")

    docs = _query(settings.cosmos_file_container, "SELECT * FROM c", [])
    n = len(docs)
    print(f"file container '{settings.cosmos_file_container}': {n} docs")
    if not docs:
        print("  (no docs — nothing to audit)")
        return 1

    date_cols = _date_columns(docs, _all_keys(docs))
    print(f"  date-like columns found: {date_cols}")
    rows = [_audit_column(docs, c) for c in date_cols]
    _print_table(rows, n)
    _recommend(rows, n)

    # Confirm day-level normalization is actually happening on a column that has time parts.
    timed = next((r for r in rows if r["with_time"]), None)
    if timed:
        sample = next((_raw(d.get(timed["col"])) for d in docs
                       if _raw(d.get(timed["col"])) and (":" in _raw(d.get(timed["col"])))), None)
        if sample:
            print(f"\nnormalization check: '{timed['col']}' raw {sample!r} "
                  f"-> normalized {parse_date(sample)} (time dropped, day-level).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
