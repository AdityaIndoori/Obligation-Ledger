#!/usr/bin/env bash
# Reset to a known-good demo state. Idempotent; safe to run repeatedly.
#
#   ./demo_reset.sh          seed + propose + pre-commit the register
#   ./demo_reset.sh --raw    seed + propose only (nothing committed)
#
# Leaves exactly the state the runbook expects at 0:00:
#   * Delta + Northgate COMMITTED (the "two already in the register" beat)
#   * Sterling COMMITTED, with a notice deadline ~75 days out
#   * Meridian PROPOSED with one RED field -> the slide-04 moment
#   * acme-services held back for the offline beat
set -euo pipefail

APP=/srv/ledger/app
DATA=/srv/ledger/data
V=$DATA/venv/bin/python
TOKEN=${LEDGER_TOKEN:-demo-token}
URL=${LEDGER_URL:-http://127.0.0.1:8443}
# ALWAYS replay. This script exists to produce one exact state: the seeded
# contracts with a DELIBERATE red field on Meridian. Inheriting LLM_MODE=live
# from ledger.env made it re-extract for real, which ignored the poisoned
# fixture, produced zero failures and committed the contract that is supposed
# to stay PROPOSED. Use ./go_live.sh for real inference.
MODE=replay

cd "$APP"

echo "== wiping register, audit chain, outputs, fixtures =="
rm -f "$DATA"/ledger.db "$DATA"/ledger.db-wal "$DATA"/ledger.db-shm
rm -f "$DATA"/audit.jsonl
rm -f /srv/ledger/outputs/*.ics /srv/ledger/outputs/*.md 2>/dev/null || true
rm -rf "$DATA"/fixtures "$DATA"/uploads
rm -f /srv/ledger/intake/* 2>/dev/null || true
rm -rf /srv/ledger/holdback
mkdir -p "$DATA"/uploads

echo "== schema =="
$V db.py

echo "== seeding contracts =="
$V seed_contracts.py

# Replay fixtures for the Sample-Contracts PDFs, so anything dropped on the
# Intake box during a demo extracts instead of being refused for want of a
# fixture. Derived from each document's own text; see sample_fixtures.py.
if [[ -d /srv/ledger/samples ]]; then
  echo "== fixtures for the sample contracts =="
  $V sample_fixtures.py | tail -1
fi

echo "== poisoning ONE field on Meridian (A1: real quote, wrong value) =="
# This is the slide-04 red field. Deliberate and reproducible: the model's
# claim of 24 months is checked against a quote that says "twelve-month".
$V - <<'PY'
import hashlib, json, sys
sys.path.insert(0, "/srv/ledger/app")
import ingest
doc = ingest.parse("/srv/ledger/intake/meridian-msa.docx")
sha = hashlib.sha256(doc.text.encode()).hexdigest()
p = f"/srv/ledger/data/fixtures/{sha}.json"
rec = json.load(open(p))
rec["data"]["auto_renewal"]["renewal_term_months"] = 24
rec["note"] = "demo: renewal_term_months fabricated (quote says twelve-month)"
json.dump(rec, open(p, "w"), indent=1)
print("   meridian renewal_term_months -> 24 (quote says twelve-month)")
PY

echo "== running pipeline =="
LLM_MODE=$MODE $V pipeline.py

if [[ "${1:-}" == "--raw" ]]; then
  echo
  echo "raw mode: nothing committed. Queue holds every contract."
  exit 0
fi

echo "== pre-committing the register (all but Meridian) =="
if ! curl -sf -o /dev/null "$URL/api/meta"; then
  echo "   ! API not reachable at $URL - start it, then re-run without --raw" >&2
  exit 1
fi
$V - <<PY
import json, urllib.request
URL = "$URL"; TOKEN = "$TOKEN"
q = json.load(urllib.request.urlopen(f"{URL}/api/queue"))
for c in sorted(q, key=lambda r: r["id"]):
    if "meridian" in c["filename"]:
        continue                       # left PROPOSED on purpose
    body = json.dumps({"id": c["id"], "action": "approve",
                       "who": "A.Sharma"}).encode()
    req = urllib.request.Request(f"{URL}/api/decide", data=body, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req))
        print(f"   committed {c['filename']}: "
              f"{len(r.get('obligations', []))} obligation(s)")
    except Exception as exc:
        print(f"   ! {c['filename']}: {exc}")
PY

echo
echo "== state =="
$V - <<'PY'
import db
con = db.connect(readonly=True)
for r in con.execute("SELECT id,filename,status,fmt,"
                     "(SELECT COUNT(*) FROM extractions e WHERE e.contract_id=c.id"
                     " AND e.validator='FAIL') f FROM contracts c ORDER BY id"):
    flag = f"  <-- {r[4]} RED FIELD" if r[4] else ""
    print(f"   [{r[0]}] {r[1]:<24} {r[2]:<10} .{r[3]}{flag}")
print("   obligations:",
      con.execute("SELECT COUNT(*) FROM obligations").fetchone()[0])
PY
$V audit.py
echo
echo "held back for the offline beat:"
ls -1 /srv/ledger/holdback 2>/dev/null | sed 's/^/   /' || echo "   (none)"
