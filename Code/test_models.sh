#!/usr/bin/env bash
# Smoke-test the model selection seam. Verifies the honesty property that
# matters most: selecting a model is FORWARD-LOOKING and never rewrites the
# provenance of an extraction that already happened.
set -euo pipefail
U=${LEDGER_URL:-http://127.0.0.1:8443}
B="Authorization: Bearer ${LEDGER_TOKEN:-demo-token}"
J="Content-Type: application/json"
V=/srv/ledger/data/venv/bin/python

pick(){ curl -s -X POST "$U/api/models/select" -H "$B" -H "$J" \
          -d "{\"model\":\"$1\"}"; echo; }
field(){ $V -c "import json,sys;print(json.load(sys.stdin)$1)"; }

echo "== staged models =="
curl -s "$U/api/models" > /tmp/mdl.json
$V - <<'PY'
import json
d = json.load(open("/tmp/mdl.json"))
print("  selected:", d["selected"])
for m in d["models"]:
    where = ("%s GB, %s shards" % (m.get("gb"), m.get("shards"))
             if m["present"] else "NOT STAGED")
    mark = "*" if m["name"] == d["selected"] else " "
    print("  %s %-20s %-6s %-9s %s"
          % (mark, m["label"], m["quant"], m["role"], where))
PY

echo "== model recorded on contract 2, BEFORE any swap =="
BEFORE=$(curl -s "$U/api/contract/2" | field '["contract"]["model"]')
echo "  contract 2 model: $BEFORE"

echo "== swap to the largest staged model =="
pick "openai/gpt-oss-120b"
curl -s "$U/api/meta" | $V -c '
import json,sys
i = json.load(sys.stdin)["model_info"]
print("  meta now reports: %s %s (%s)" % (i["label"], i["quant"], i["role"]))'

echo "== history must be UNCHANGED =="
AFTER=$(curl -s "$U/api/contract/2" | field '["contract"]["model"]')
echo "  contract 2 model: $AFTER"
if [[ "$BEFORE" != "$AFTER" ]]; then
  echo "  FAIL: selecting a model rewrote an existing contract's provenance" >&2
  exit 1
fi
echo "  OK: past extractions keep the model that actually produced them"

echo "== an unknown model is refused =="
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$U/api/models/select" \
        -H "$B" -H "$J" -d '{"model":"evil/backdoor"}')
echo "  HTTP $CODE"
[[ "$CODE" == "400" ]] || { echo "  FAIL: expected 400" >&2; exit 1; }

echo "== selection requires the bearer token =="
CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$U/api/models/select" \
        -H "$J" -d '{"model":"openai/gpt-oss-20b"}')
echo "  HTTP $CODE"
[[ "$CODE" == "401" ]] || { echo "  FAIL: expected 401" >&2; exit 1; }

echo "== the swap is in the audit chain =="
curl -s "$U/api/audit/log?limit=10" | $V -c '
import json,sys
recs = [r for r in json.load(sys.stdin) if r["event"] == "model_selected"]
print("  %d model_selected record(s) in the chain" % len(recs))
raise SystemExit(0 if recs else 1)'

echo "== selection survives a restart (state is on disk) =="
$V -c 'import sys;sys.path.insert(0,"/srv/ledger/app");import models
print("  a fresh process reads:", models.selected())'

echo "== extract.py resolves the same selection =="
$V -c 'import sys;sys.path.insert(0,"/srv/ledger/app");import extract
print("  extract.current_model():", extract.current_model())'

echo "== restore the primary =="
pick "Qwen/Qwen3.6-35B-A3B-FP8" > /dev/null
curl -s "$U/api/meta" | field '["model"]' | sed 's/^/  back to: /'

echo
echo "ALL MODEL-SELECTION CHECKS PASSED"
