"""Reproduce the two chat defects seen in the UI, then guard against them.

1. "Are you sure?" produced a chatty self-description about being an AI that
   translates questions into JSON plans. That is the PLANNER's system prompt
   leaking into the answer: a question with no queryable content fell through
   to a plan, the plan matched everything, and the narrator was handed the
   planner's framing.

2. "Who are the parties again?" answered about the wrong contract and linked
   to "contract 1", which is ARCHIVED (soft-deleted). Two faults: stale
   conversation context pointing at a deleted contract, and a link built from
   a row id without checking the contract is still live.
"""
import sys

import chat
import db


def show(q, ctx=None):
    r = chat.answer(q, context_id=ctx)
    ids = sorted({x.get("id") for x in r["rows"] if x.get("id")})
    print(f"Q: {q}")
    print(f"   engine={r['engine']} fell_back={r['fell_back']} "
          f"rows={r['count']} row_ids={ids}")
    print(f"   contract_id returned={r.get('contract_id')}")
    print(f"   A: {r['answer'][:140]}")
    print()
    return r


def main():
    con = db.connect(readonly=True)
    archived = {r["id"] for r in con.execute(
        "SELECT id FROM contracts WHERE archived=1")}
    live = {r["id"] for r in con.execute(
        "SELECT id FROM contracts WHERE archived=0")}
    con.close()
    print(f"archived contract ids: {sorted(archived)}")
    print(f"live contract ids:     {sorted(live)}\n")

    fails = []

    print("=== 1. a question with no queryable content ===")
    r = show("Are you sure?")
    leak = any(w in r["answer"].lower() for w in
               ("json", "query plan", "ai assistant", "language model",
                "i am an ai", "instructions provided"))
    if leak:
        fails.append("planner prompt leaked into the answer for 'Are you sure?'")
    if r["count"] and r["engine"] != "none":
        fails.append("'Are you sure?' ran a query instead of asking for a "
                     "real question")

    print("=== 2. stale context must not point at a deleted contract ===")
    if archived:
        stale = sorted(archived)[0]
        r = show("Who are the parties again?", ctx=stale)
        bad = [i for i in
               {x.get("id") for x in r["rows"] if x.get("id")}
               if i in archived]
        if bad:
            fails.append(f"answer included archived contract(s) {bad}")
        if r.get("contract_id") in archived:
            fails.append(f"returned contract_id {r['contract_id']} is archived")
    else:
        print("  (no archived contracts to test with)\n")

    print("=== 3. no answer may ever cite an archived contract ===")
    for q in ("who are the parties?", "show me the payments",
              "any unusual terms?", "what renews next?"):
        r = chat.answer(q)
        cited = {x.get("id") for x in r["rows"] if x.get("id")} & archived
        mark = "BAD " if cited else "ok  "
        print(f"  {mark}{q:<28} rows={r['count']:<3} archived cited={sorted(cited)}")
        if cited:
            fails.append(f"{q!r} cited archived {sorted(cited)}")

    print()
    if fails:
        print("FAILURES:")
        for f in fails:
            print("   - " + f)
        return 1
    print("ALL CHAT CONTEXT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
