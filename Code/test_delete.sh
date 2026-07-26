#!/usr/bin/env bash
# Smoke-test soft delete. The property that matters: deleting hides a contract
# from the working views but NEVER breaks the append-only audit chain, and is
# reversible.
#
# NOTE the trap below. An earlier version of this script archived contracts and
# left them archived when an assertion failed mid-run, which emptied the
# register and looked like data loss. Restoring on EXIT is not optional.
set -euo pipefail
U=${LEDGER_URL:-http://127.0.0.1:8443}
B="Authorization: Bearer ${LEDGER_TOKEN:-demo-token}"
J="Content-Type: application/json"
V=/srv/ledger/data/venv/bin/python

restore_all(){
  $V - <<'PY' || true
import sys
sys.path.insert(0, "/srv/ledger/app")
import db
con = db.connect()
n = con.execute("UPDATE contracts SET archived=0, archived_at=NULL,"
                " archived_by=NULL WHERE archived=1").rowcount
con.execute("UPDATE obligations SET status='OPEN' WHERE status='ARCHIVED'")
con.commit()
if n:
    print(f"  [cleanup] restored {n} contract(s)")
PY
}
trap restore_all EXIT
code(){ curl -s -o /tmp/del.out -w "%{http_code}" "$@"; }
field(){ $V -c "import json,sys;print(json.load(sys.stdin)$1)"; }
count(){ curl -s "$1" | $V -c "import json,sys;print(len(json.load(sys.stdin)))"; }

echo "== pick a PROPOSED and a COMMITTED contract =="
PROP=$(curl -s "$U/api/queue" | $V -c '
import json,sys
r=[c for c in json.load(sys.stdin) if c["status"]=="PROPOSED" and not c["archived"]]
print(r[0]["id"] if r else "")')
COMM=$(curl -s "$U/api/queue" | $V -c '
import json,sys
r=[c for c in json.load(sys.stdin) if c["status"]=="COMMITTED" and not c["archived"]]
print(r[0]["id"] if r else "")')
echo "  proposed=$PROP committed=$COMM"
[[ -n "$PROP" ]] || { echo "  need a PROPOSED contract; run ./go_live.sh" >&2; exit 1; }

echo "== auth is required =="
C=$(code -X POST "$U/api/delete" -H "$J" -d "{\"id\":$PROP}")
echo "  HTTP $C"; [[ "$C" == "401" ]] || { echo "  FAIL expected 401" >&2; exit 1; }

echo "== chain intact before =="
curl -s "$U/api/audit" | field '["message"]' | sed 's/^/  /'
REG_BEFORE=$(count "$U/api/register")
DL_BEFORE=$(count "$U/api/deadlines?days=100000")
echo "  register=$REG_BEFORE deadlines=$DL_BEFORE"

echo "== delete the PROPOSED one =="
curl -s -X POST "$U/api/delete" -H "$B" -H "$J" -d "{\"id\":$PROP,\"who\":\"tester\"}"; echo

echo "== it is archived, and its provenance is recorded =="
curl -s "$U/api/queue" | $V -c "
import json,sys
c=[x for x in json.load(sys.stdin) if x['id']==$PROP][0]
assert c['archived']==1, 'not archived'
assert c['archived_by']=='tester', c['archived_by']
assert c['archived_at'], 'no timestamp'
print('  archived=1 by', c['archived_by'], 'at', c['archived_at'][:19])"

echo "== deleting twice is refused =="
C=$(code -X POST "$U/api/delete" -H "$B" -H "$J" -d "{\"id\":$PROP,\"who\":\"tester\"}")
echo "  HTTP $C"; [[ "$C" == "409" ]] || { echo "  FAIL expected 409" >&2; exit 1; }

echo "== a COMMITTED contract needs force =="
if [[ -n "$COMM" ]]; then
  C=$(code -X POST "$U/api/delete" -H "$B" -H "$J" -d "{\"id\":$COMM,\"who\":\"tester\"}")
  echo "  without force: HTTP $C"
  [[ "$C" == "409" ]] || { echo "  FAIL expected 409" >&2; exit 1; }
  curl -s -X POST "$U/api/delete" -H "$B" -H "$J" \
    -d "{\"id\":$COMM,\"who\":\"tester\",\"force\":true}"; echo
  REG_AFTER=$(count "$U/api/register")
  DL_AFTER=$(count "$U/api/deadlines?days=100000")
  echo "  register $REG_BEFORE -> $REG_AFTER   deadlines $DL_BEFORE -> $DL_AFTER"
  [[ "$REG_AFTER" -lt "$REG_BEFORE" ]] || { echo "  FAIL register unchanged" >&2; exit 1; }
  [[ "$DL_AFTER" -lt "$DL_BEFORE" ]] || { echo "  FAIL its dates are still live" >&2; exit 1; }
fi

echo "== the chatbot must not report a deleted contract =="
curl -s "$U/api/ask?prose=false&q=what%20is%20pending%20approval" | $V -c "
import json,sys
d=json.load(sys.stdin)
ids=[r.get('id') for r in d['rows']]
assert $PROP not in ids, 'deleted contract leaked into Ask'
print('  Ask rows:', len(d['rows']), '-- deleted contract absent')"

echo "== THE POINT: the audit chain still verifies =="
curl -s "$U/api/audit" | $V -c "
import json,sys
d=json.load(sys.stdin)
assert d['ok'], d['message']
print(' ', d['message'])"
curl -s "$U/api/audit/log?limit=20" | $V -c "
import json,sys
recs=[r for r in json.load(sys.stdin) if r['event']=='deleted']
print(' ', len(recs), 'deletion(s) recorded in the chain')
raise SystemExit(0 if recs else 1)"

echo "== restore both =="
curl -s -X POST "$U/api/delete" -H "$B" -H "$J" \
  -d "{\"id\":$PROP,\"restore\":true,\"who\":\"tester\"}"; echo
[[ -n "$COMM" ]] && { curl -s -X POST "$U/api/delete" -H "$B" -H "$J" \
  -d "{\"id\":$COMM,\"restore\":true,\"who\":\"tester\"}"; echo; }
REG_BACK=$(count "$U/api/register")
DL_BACK=$(count "$U/api/deadlines?days=100000")
echo "  register back to $REG_BACK (was $REG_BEFORE), deadlines $DL_BACK (was $DL_BEFORE)"
[[ "$REG_BACK" == "$REG_BEFORE" ]] || { echo "  FAIL register did not restore" >&2; exit 1; }
[[ "$DL_BACK" == "$DL_BEFORE" ]] || { echo "  FAIL dates did not restore" >&2; exit 1; }
curl -s "$U/api/audit" | field '["ok"]' | sed 's/^/  chain ok: /'

echo
echo "ALL DELETE CHECKS PASSED (soft delete, forced commit delete, chain intact, reversible)"
