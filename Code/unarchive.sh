#!/usr/bin/env bash
# Restore every soft-deleted contract.
#
# Deleting is reversible by design, but there was no bulk undo -- so a few
# exploratory clicks in the UI (or a test run cut short) could leave most of the
# register hidden, which reads as data loss even though nothing was lost.
# This is that undo.
#
#   ./unarchive.sh          restore everything
#   ./unarchive.sh --list   show what is currently hidden, change nothing
set -euo pipefail
cd /srv/ledger/app
V=/srv/ledger/data/venv/bin/python

if [[ "${1:-}" == "--list" ]]; then
  $V - <<'PY'
import db
con = db.connect(readonly=True)
rows = list(con.execute(
    "SELECT id, filename, status, archived_by, archived_at FROM contracts"
    " WHERE archived=1 ORDER BY id"))
if not rows:
    print("nothing is hidden; the register is complete")
else:
    print(f"{len(rows)} contract(s) hidden:")
    for r in rows:
        print(f"   [{r['id']:>2}] {r['filename'][:40]:<42}"
              f"{r['status']:<10} by {r['archived_by']} "
              f"on {(r['archived_at'] or '')[:10]}")
PY
  exit 0
fi

$V - <<'PY'
import db
con = db.connect()
hidden = con.execute("SELECT COUNT(*) FROM contracts"
                     " WHERE archived=1").fetchone()[0]
if not hidden:
    print("nothing to restore")
else:
    con.execute("UPDATE contracts SET archived=0, archived_at=NULL,"
                " archived_by=NULL WHERE archived=1")
    reopened = con.execute("UPDATE obligations SET status='OPEN'"
                           " WHERE status='ARCHIVED'").rowcount
    con.commit()
    print(f"restored {hidden} contract(s), reopened {reopened} obligation(s)")

con2 = db.connect(readonly=True)
print("register now:",
      con2.execute("SELECT COUNT(*) FROM contracts WHERE archived=0"
                   " AND status='COMMITTED'").fetchone()[0], "committed,",
      con2.execute("SELECT COUNT(*) FROM contracts WHERE archived=0"
                   " AND status='PROPOSED'").fetchone()[0], "awaiting review,",
      con2.execute("SELECT COUNT(*) FROM obligations"
                   " WHERE status='OPEN'").fetchone()[0], "open obligations")
PY

# The restore is a state change like any other, so it belongs in the chain.
$V - <<'PY'
import audit
audit.append("cli:unarchive", "restored_all", {"scope": "all archived"})
ok, msg = audit.verify()
print("audit:", msg)
PY
