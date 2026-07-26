"""Measure how the validators judge REAL model output.

This is the first honest number we have. Until now the project quoted
ContractEval's 0.64 F1 (MASTER doc 1.7 states plainly that we had not measured
our own). This script reports what actually happened on the sample corpus.

IMPORTANT about what it does and does not measure:
  * It measures the QUOTE-MATCH RATE -- how often a value the model reported
    could be found inside the text the model cited for it.
  * It does NOT measure correctness. A value can match its quote perfectly and
    still come from the wrong clause. That residual risk is exactly what the
    human approval gate exists for, and no number here should be presented as
    an accuracy figure.
"""
import sys
from collections import Counter

import db


def main():
    con = db.connect(readonly=True)
    totals = Counter()
    field_fails = Counter()
    field_total = Counter()

    print(f"{'contract':<42}{'ok':>4}{'fail':>6}   failing fields")
    print("-" * 100)
    for c in con.execute("SELECT id,filename,status,note,model,llm_mode"
                         " FROM contracts ORDER BY id"):
        if c["status"] == "REJECTED":
            print(f"{c['filename'][:40]:<42}  REJECTED  "
                  f"{(c['note'] or '')[:44]}")
            totals["rejected"] += 1
            continue
        rows = list(con.execute(
            "SELECT field,value,validator,note FROM extractions"
            " WHERE contract_id=?", (c["id"],)))
        ok = sum(1 for r in rows if r["validator"] == "PASS")
        bad = [r for r in rows if r["validator"] == "FAIL"]
        totals["pass"] += ok
        totals["fail"] += len(bad)
        totals["computed"] += sum(1 for r in rows
                                  if r["validator"] == "COMPUTED")
        for r in rows:
            if r["validator"] in ("PASS", "FAIL"):
                field_total[r["field"]] += 1
            if r["validator"] == "FAIL":
                field_fails[r["field"]] += 1
        detail = "; ".join(f"{r['field']}={str(r['value'])[:16]}" for r in bad)
        print(f"{c['filename'][:40]:<42}{ok:>4}{len(bad):>6}   {detail[:46]}")

    checked = totals["pass"] + totals["fail"]
    print("-" * 100)
    print(f"quote-matched {totals['pass']}   "
          f"not-in-quote {totals['fail']}   "
          f"computed {totals['computed']}   "
          f"documents refused {totals['rejected']}")
    if checked:
        print(f"\nQUOTE-MATCH RATE on live model output: "
              f"{totals['pass']}/{checked} = "
              f"{100 * totals['pass'] / checked:.1f}%")
        print("(NOT an accuracy figure -- a value can match its quote and still "
              "cite the wrong clause. That is what the approval gate is for.)")

    if field_fails:
        print("\nfailures by field:")
        for f, n in field_fails.most_common():
            print(f"   {f:<24} {n}/{field_total[f]}")

    # Which contracts a partner could approve without touching anything
    clean = con.execute(
        "SELECT COUNT(*) FROM contracts c WHERE c.status='PROPOSED'"
        " AND NOT EXISTS (SELECT 1 FROM extractions e"
        "   WHERE e.contract_id=c.id AND e.validator='FAIL')").fetchone()[0]
    prop = con.execute("SELECT COUNT(*) FROM contracts"
                       " WHERE status='PROPOSED'").fetchone()[0]
    print(f"\ncommittable without a correction: {clean}/{prop} contracts")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
