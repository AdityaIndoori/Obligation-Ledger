"""SQLite register. Schema per MASTER doc 3.4, plus D-B provenance columns.

D-B: extractions carries span_start / span_end / page so the UI can jump to the
quote in the source document. Computed in Python from the parsed text layer --
the model is never in that code path.
"""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("LEDGER_DB", "/srv/ledger/data/ledger.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sha256 TEXT UNIQUE NOT NULL,
  filename TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PROPOSED','COMMITTED','REJECTED')),
  model TEXT,
  validated INTEGER NOT NULL DEFAULT 1,
  ingested_at TEXT NOT NULL,
  decided_at TEXT,
  decided_by TEXT,
  fmt TEXT,
  converted_via TEXT,
  llm_mode TEXT NOT NULL DEFAULT 'live',
  doctext TEXT,
  note TEXT,
  -- Soft delete. The audit chain is append-only and hash-linked, so a row can
  -- never be physically removed without breaking tamper-evidence. Deleting
  -- hides a contract from the working views and records WHO hid it and WHEN;
  -- the evidence survives, which is the whole point of the product.
  archived INTEGER NOT NULL DEFAULT 0,
  archived_at TEXT,
  archived_by TEXT);
CREATE TABLE IF NOT EXISTS extractions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES contracts(id),
  field TEXT NOT NULL, value TEXT, source_span TEXT,
  validator TEXT NOT NULL CHECK(validator IN ('PASS','FAIL','NA','COMPUTED','HUMAN')),
  note TEXT, edited_by_human INTEGER NOT NULL DEFAULT 0,
  span_start INTEGER, span_end INTEGER, page INTEGER);
CREATE TABLE IF NOT EXISTS obligations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES contracts(id),
  kind TEXT NOT NULL, due_date TEXT NOT NULL, detail TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN');
CREATE INDEX IF NOT EXISTS idx_ex_contract ON extractions(contract_id);
CREATE INDEX IF NOT EXISTS idx_ob_due ON obligations(due_date, status);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(readonly=False):
    if readonly:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(DB_PATH, timeout=15)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
    con.row_factory = sqlite3.Row
    return con


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists, so they are applied explicitly.
MIGRATIONS = [
    ("contracts", "archived", "INTEGER NOT NULL DEFAULT 0"),
    ("contracts", "archived_at", "TEXT"),
    ("contracts", "archived_by", "TEXT"),
]


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = connect()
    con.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    con.commit()
    con.close()


if __name__ == "__main__":
    init()
    print("schema ready at", DB_PATH)
