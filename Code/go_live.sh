#!/usr/bin/env bash
# Bring the ledger up in LIVE mode against the running vLLM endpoint, extract
# the sample corpus for real, and report measured extraction quality.
#
#   ./go_live.sh          extract the 10 samples live, leave them PROPOSED
#   ./go_live.sh --commit also commit everything that passed cleanly
#
# Unlike demo_reset.sh (which uses hand-authored replay fixtures), this runs
# genuine inference and writes real recordings to fixtures/ -- so a later
# LLM_MODE=replay run reproduces actual model output, not authored output.
set -euo pipefail
cd /srv/ledger/app
source ./ledger.env
V=/srv/ledger/data/venv/bin/python

echo "== endpoint =="
$V - <<'PY'
import json, os, urllib.error, urllib.request
probe = os.environ["LLM_URL"].replace("/chat/completions", "/models")
try:
    d = json.load(urllib.request.urlopen(probe, timeout=5))
except (urllib.error.URLError, OSError) as exc:
    raise SystemExit(f"  UNREACHABLE {probe}: {exc}\n"
                     "  Start vLLM, or run ./demo_reset.sh for replay mode.")
for m in d.get("data", []):
    print(f"  serving '{m['id']}'  max_model_len={m.get('max_model_len')}")
PY

echo "== model wiring =="
$V -c '
import models
for m in models.staged():
    if m["live"]:
        print("  %s %s -> wire id %r (%s GB, %s shards)"
              % (m["label"], m["quant"], m["served_as"], m.get("gb"),
                 m.get("shards")))
'

echo "== wiping register =="
rm -f /srv/ledger/data/ledger.db /srv/ledger/data/ledger.db-wal \
      /srv/ledger/data/ledger.db-shm /srv/ledger/data/audit.jsonl
rm -f /srv/ledger/outputs/*.ics /srv/ledger/outputs/*.md 2>/dev/null || true
rm -rf /srv/ledger/data/uploads && mkdir -p /srv/ledger/data/uploads
$V db.py

echo "== live extraction of the sample corpus (real inference) =="
rm -rf /tmp/golive && mkdir -p /tmp/golive
cp /srv/ledger/samples/*.pdf /tmp/golive/
time LLM_MODE=record LEDGER_INTAKE=/tmp/golive $V pipeline.py

if [[ "${1:-}" == "--commit" ]]; then
  echo "== committing everything that passed cleanly =="
  $V - <<'PY'
import json, os, urllib.request
U = "http://127.0.0.1:8443"
tok = os.environ.get("LEDGER_TOKEN", "demo-token")
try:
    q = json.load(urllib.request.urlopen(f"{U}/api/queue"))
except Exception as exc:
    raise SystemExit(f"  API not reachable ({exc}); start it and re-run")
for c in sorted(q, key=lambda r: r["id"]):
    if c["status"] != "PROPOSED" or c["failures"]:
        continue
    body = json.dumps({"id": c["id"], "action": "approve",
                       "who": "A.Sharma"}).encode()
    req = urllib.request.Request(f"{U}/api/decide", data=body, headers={
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(req))
        print(f"  committed {c['filename'][:38]:<40} "
              f"{len(r.get('obligations', []))} obligation(s)")
    except Exception as exc:
        print(f"  ! {c['filename']}: {exc}")
PY
fi

echo
echo "== measured quality on live output =="
$V measure.py
$V audit.py
