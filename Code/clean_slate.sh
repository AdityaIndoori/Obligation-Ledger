#!/usr/bin/env bash
# Wipe to a genuinely empty system. No contracts, no obligations, no audit
# history, no fixtures, no retrieval index, no seeded or sample files in the
# watched directories.
#
#   ./clean_slate.sh            wipe (asks first)
#   ./clean_slate.sh --yes      wipe without asking
#   ./clean_slate.sh --dry-run  list exactly what would go
#
# What is DELIBERATELY kept:
#   * the code, the venv and the wheel cache
#   * /srv/ledger/samples  -- the ten test PDFs stay on disk so you can drop
#     them in by hand. They are only ever ingested when you choose to.
#   * the model weights and the vLLM container
#
# Why a script and not a handful of rm commands: the state lives in six places
# (SQLite + WAL, the audit chain, fixtures, uploads, intake, outputs, and the
# RAG service's own store). Missing one leaves the UI showing rows that the
# register no longer has, or retrieval answering about contracts that are gone.
set -euo pipefail
cd /srv/ledger/app
source ./ledger.env
V=/srv/ledger/data/venv/bin/python

DRY=0; YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --yes|-y)  YES=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

RAG_DIR="${RAG_DATA_DIR:-/srv/ledger/rag}"

echo "== what will be removed =="
$V - <<'PY' 2>/dev/null || echo "  register: (no database yet)"
import db
con = db.connect(readonly=True)
c = con.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
e = con.execute("SELECT COUNT(*) FROM extractions").fetchone()[0]
o = con.execute("SELECT COUNT(*) FROM obligations").fetchone()[0]
print(f"  register:      {c} contract(s), {e} extraction(s), {o} obligation(s)")
PY
printf '  audit chain:   %s record(s)\n' \
  "$(wc -l < /srv/ledger/data/audit.jsonl 2>/dev/null || echo 0)"
printf '  intake:        %s file(s)\n'   "$(ls -1 /srv/ledger/intake 2>/dev/null | wc -l)"
printf '  uploads:       %s file(s)\n'   "$(ls -1 /srv/ledger/data/uploads 2>/dev/null | wc -l)"
printf '  fixtures:      %s file(s)\n'   "$(ls -1 /srv/ledger/data/fixtures 2>/dev/null | wc -l)"
printf '  outputs:       %s file(s)\n'   "$(ls -1 /srv/ledger/outputs 2>/dev/null | wc -l)"
printf '  holdback:      %s file(s)\n'   "$(ls -1 /srv/ledger/holdback 2>/dev/null | wc -l)"
printf '  RAG store:     %s\n'           "$RAG_DIR"
echo
echo "  KEPT: code, venv, wheels, /srv/ledger/samples, model weights"

if [[ $DRY -eq 1 ]]; then
  echo
  echo "dry run: nothing was changed"
  exit 0
fi

if [[ $YES -ne 1 ]]; then
  echo
  read -r -p "Wipe all of the above? [y/N] " reply
  [[ "$reply" == "y" || "$reply" == "Y" ]] || { echo "aborted"; exit 1; }
fi

echo
echo "== wiping =="

# 1. the register. WAL and SHM must go too, or a stale write-ahead log can
#    resurrect rows the main file no longer has.
rm -f /srv/ledger/data/ledger.db \
      /srv/ledger/data/ledger.db-wal \
      /srv/ledger/data/ledger.db-shm
echo "  register removed"

# 2. the audit chain. Starting from GENESIS again is the point of a clean
#    slate: a chain with old records but no contracts would verify and still
#    describe a system that no longer exists.
rm -f /srv/ledger/data/audit.jsonl
echo "  audit chain removed"

# 3. replay fixtures. Left behind, they would let a document extract from a
#    recording instead of the live model without anyone choosing that.
rm -rf /srv/ledger/data/fixtures
echo "  fixtures removed"

# 4. the watched directories. Anything left here is re-ingested the moment
#    something scans, which is exactly the pre-filled state being removed.
rm -rf /srv/ledger/intake /srv/ledger/data/uploads /srv/ledger/holdback
mkdir -p /srv/ledger/intake /srv/ledger/data/uploads
echo "  intake, uploads and holdback emptied"

# 5. generated artifacts
rm -f /srv/ledger/outputs/*.ics /srv/ledger/outputs/*.md 2>/dev/null || true
echo "  memos and calendar files removed"

# 6. the RAG service's own store. Its SQLite and index files are independent
#    of ours, so wiping only our database would leave retrieval answering
#    questions about contracts that no longer exist.
rm -rf "$RAG_DIR"
mkdir -p "$RAG_DIR"
echo "  RAG store removed ($RAG_DIR)"
rm -rf /srv/ledger/app/runtime-data 2>/dev/null || true

# 7. model selection state -- back to the catalog default
rm -f /srv/ledger/data/model_state.json
echo "  model selection reset to default"

echo
echo "== recreating an empty schema =="
$V db.py

echo
echo "== verifying it is actually empty =="
$V - <<'PY'
import os
import db
con = db.connect(readonly=True)
counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
          for t in ("contracts", "extractions", "obligations")}
print("  register:", ", ".join(f"{v} {k}" for k, v in counts.items()))
assert not any(counts.values()), "database is not empty"

for label, path in (("intake", "/srv/ledger/intake"),
                    ("uploads", "/srv/ledger/data/uploads"),
                    ("fixtures", "/srv/ledger/data/fixtures")):
    n = len(os.listdir(path)) if os.path.isdir(path) else 0
    print(f"  {label}: {n} file(s)")
    assert n == 0, f"{label} is not empty"

audit_path = os.environ.get("LEDGER_AUDIT", "/srv/ledger/data/audit.jsonl")
print("  audit chain:", "absent (will start at genesis)"
      if not os.path.exists(audit_path) else "STILL PRESENT")
assert not os.path.exists(audit_path)
print("  verified empty")
PY

# The RAG service caches its store handle, so it has to be restarted to notice
# the wipe. Reported rather than done here: this script does not own that
# process.
echo
echo "NEXT: restart both services so neither serves a cached view of the old data"
echo "   the app :8443  and  the RAG service :8001"
echo
echo "Sample contracts are still on disk if you want to add any by hand:"
ls -1 /srv/ledger/samples 2>/dev/null | sed 's/^/   /' || echo "   (none)"
