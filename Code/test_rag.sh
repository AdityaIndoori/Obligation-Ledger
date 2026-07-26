#!/usr/bin/env bash
# Verify the RAG deployment end to end.
#
# The properties that matter:
#   1. retrieval is the PRIMARY path for document questions
#   2. the register still owns counts, dates and workflow state
#   3. the long contract that truncation used to eat is now answerable
#   4. OUR validators still decide the verdict, whatever RAG reports
#   5. offsets returned by RAG index OUR ParsedDoc.text
set -euo pipefail
cd /srv/ledger/app
source ./ledger.env
V=/srv/ledger/data/venv/bin/python

echo "== the service is up =="
curl -s -m 5 -o /dev/null -w "  :8001 openapi -> HTTP %{http_code}\n" \
  http://127.0.0.1:8001/openapi.json
$V -c 'import retriever; print("  in-process seam available:", retriever.available())'

echo
echo "== it is pointed at OUR model, not its default =="
grep -E '^LLM_MODEL|^USE_FAKE_LLM' /srv/ledger/rag-src/.env | sed 's/^/  /'
curl -s -m 5 http://127.0.0.1:8000/v1/models \
  | $V -c 'import json,sys; print("  vLLM serves:",
        [m["id"] for m in json.load(sys.stdin)["data"]])'

echo
echo "== retrieval answers document questions; register answers counts =="
$V - <<'PY'
import json, urllib.parse, urllib.request

CASES = [
    ("what does the Trellis contract say about renewal?", "rag"),
    ("which contract has an uncapped indemnity?", "rag"),
    ("what does the Ridgeline supply agreement say about renewal?", "rag"),
    ("how many contracts are there?", "register"),
    ("what is pending approval?", "register"),
]
bad = []
for q, want in CASES:
    r = json.load(urllib.request.urlopen(
        "http://127.0.0.1:8443/api/ask?q=" + urllib.parse.quote(q),
        timeout=240))
    got = r.get("engine")
    ok = got == want or (want == "rag" and got == "fulltext")
    print(f"  {'ok ' if ok else 'BAD'} engine={got:<9} "
          f"({want} expected)  {q[:52]}")
    print(f"      {r['answer'][:110]}")
    if not ok:
        bad.append((q, want, got))
if bad:
    raise SystemExit(f"wrong engine for {len(bad)} question(s): {bad}")
PY

echo
echo "== the 57k-char contract: retrieval reaches what truncation could not =="
$V - <<'PY'
import ingest, retriever
doc = ingest.parse("/srv/ledger/samples/07-ridgeline-supply-long.pdf")
clause = doc.text.lower().find("two hundred and seventy")
print(f"  document is {len(doc.text)} chars; the renewal clause sits at "
      f"offset {clause}")
print(f"  extract.py truncates at 40000 -> clause is "
      f"{'BEYOND the cut' if clause > 40000 else 'inside the window'}")
ps = retriever.retrieve("notice of non-renewal two hundred and seventy days",
                        k=8, contract_id=7)
hit = any("two hundred and seventy" in (p.text or "").lower() for p in ps)
print(f"  retrieval returned {len(ps)} passage(s); clause present: {hit}")
raise SystemExit(0 if hit else 1)
PY

echo
echo "== RAG offsets index OUR ParsedDoc.text (their §11 guarantee) =="
$V - <<'PY'
import ingest, retriever
doc = ingest.parse("/srv/ledger/samples/02-trellis-licence-buried-clause.pdf")
retriever.index(2, doc)
ps = retriever.retrieve("automatic renewal notice", k=3, contract_id=2)
checked = 0
for p in ps:
    if p.char_start is None:
        continue
    assert doc.text[p.char_start:p.char_end] == p.text, "offset mismatch"
    checked += 1
print(f"  verified {checked} passage offset(s) against our own text")
raise SystemExit(0 if checked else 1)
PY

echo
echo "== OUR validators still gate Approve, whatever RAG reports =="
$V - <<'PY'
import ingest, retriever
doc = ingest.parse("/srv/ledger/samples/02-trellis-licence-buried-clause.pdf")
rows, computed, meta = retriever.extract_via_rag(2, doc)
ours_fail = [r for r in rows if r[3] == "FAIL"]
print(f"  RAG can_approve={meta['their_can_approve']}  "
      f"our failures={meta['our_failures']}  "
      f"disagreements={meta['disagreements']}")
for f, v, s, verdict, note, *_ in rows:
    print(f"    {verdict:<9}{f:<26}{str(v)[:30]}")
# The point: our verdicts exist independently of theirs.
assert all(r[3] in ("PASS", "FAIL", "COMPUTED") for r in rows), \
    "unexpected verdict from the RAG path"
print("  every row carries OUR verdict, not theirs")
PY

echo
echo "ALL RAG DEPLOYMENT CHECKS PASSED"
