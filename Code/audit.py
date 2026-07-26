"""Append-only, hash-chained audit log. Verbatim from MASTER doc T7, with the
audit path env-driven per the portability contract.

Each record commits to its predecessor, so any edit to any earlier line breaks
verification at that point and every point after it.
"""
import hashlib
import json
import os

from db import now

AUDIT_PATH = os.environ.get("LEDGER_AUDIT", "/srv/ledger/data/audit.jsonl")
GENESIS = "0" * 64
CORE_KEYS = ("seq", "ts", "actor", "event", "payload_sha256", "prev")


def _h(obj):
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _lines(path=None):
    path = path or AUDIT_PATH
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def append(actor, event, payload):
    recs = _lines()
    prev = recs[-1]["self"] if recs else GENESIS
    core = {"seq": len(recs) + 1, "ts": now(), "actor": actor,
            "event": event, "payload_sha256": _h(payload), "prev": prev}
    core["self"] = _h(core)
    os.makedirs(os.path.dirname(AUDIT_PATH), exist_ok=True)
    with open(AUDIT_PATH, "a") as f:
        f.write(json.dumps(core) + "\n")
    return core


def verify(path=None):
    prev = GENESIS
    for i, r in enumerate(_lines(path), start=1):
        if r["seq"] != i:
            return False, f"sequence break at line {i}"
        if r["prev"] != prev:
            return False, f"chain break at seq {i}"
        core = {k: r[k] for k in CORE_KEYS}
        if _h(core) != r["self"]:
            return False, f"record altered at seq {i}"
        prev = r["self"]
    return True, "chain intact"


if __name__ == "__main__":
    import sys
    ok, msg = verify(sys.argv[1] if len(sys.argv) > 1 else None)
    print(("OK: " if ok else "FAIL: ") + msg)
    raise SystemExit(0 if ok else 1)
