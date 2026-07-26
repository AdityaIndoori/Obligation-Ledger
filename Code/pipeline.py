"""Ingest pipeline: parse -> (index) -> extract -> validate -> PROPOSE.

Two invariants this file must never break:
  * It can only ever write status='PROPOSED'. COMMITTED is reachable only
    through POST /api/decide (D7). No path here changes that.
  * sha256 of the file bytes is UNIQUE, so re-running is idempotent and a
    reboot mid-queue cannot duplicate rows (AT-5).
"""
import glob
import hashlib
import os
import sys

import audit
import db
import extract
import ingest
import validate as V

INTAKE_DIRS = [d for d in os.environ.get(
    "LEDGER_INTAKE",
    "/srv/ledger/intake,/srv/ledger/data/uploads").split(",") if d]
RAG = os.environ.get("LEDGER_RAG", "on").lower() == "on"
# native = one model call over the truncated document (extract.py)
# rag    = the RAG lane retrieves passages AND reports per-field status
# Either way OUR validators decide the verdict that gates Approve.
EXTRACTOR = os.environ.get("LEDGER_EXTRACTOR", "native").lower()


def _index(contract_id, doc):
    """Optional RAG seam (S2). A retrieval failure must NEVER block a proposal."""
    if not RAG:
        return None
    try:
        import retriever
    except ImportError:
        return None
    try:
        return retriever.index(contract_id, doc)
    except Exception as exc:              # noqa: BLE001 - deliberately broad
        print(f"  ! retriever.index failed (ignored): {exc}")
        return None


def _retrieve(query, contract_id):
    if not RAG:
        return None
    try:
        import retriever
    except ImportError:
        return None
    try:
        return retriever.retrieve(query, k=8, contract_id=contract_id) or None
    except Exception as exc:              # noqa: BLE001
        print(f"  ! retriever.retrieve failed (ignored): {exc}")
        return None


def _unvalidated_rows(data):
    """Flatten an extraction into (field, value, span, 'NA', note, ...) rows
    WITHOUT running any check.

    Mirrors validate.validate()'s field naming exactly, so the ablation demo
    shows the same fields with the same values and no verdicts -- which is the
    whole point: the model is just as confident, and nothing checked it. The
    span is carried through but deliberately NOT located or verified, so no
    offsets are stored and the UI cannot offer a receipt.
    """
    NOTE = "VALIDATION DISABLED"
    rows = []

    def add(field, value, span):
        rows.append((field, value, span, "NA", NOTE, None, None, None))

    for p in data.get("parties") or []:
        add(f"party:{p.get('role') or 'party'}", p.get("name"),
            p.get("source_span"))
    for name in ("effective_date", "term_end", "governing_law"):
        d = data.get(name) or {}
        if d.get("value") is not None:
            add(name, d.get("value"), d.get("source_span"))
    ar = data.get("auto_renewal") or {}
    if ar.get("present"):
        add("notice_days", ar.get("notice_days"), ar.get("source_span"))
        add("renewal_term_months", ar.get("renewal_term_months"),
            ar.get("source_span"))
    pay = data.get("payment") or {}
    if pay.get("amount"):
        add("payment_amount",
            f"{pay.get('currency','')} {pay.get('amount')}".strip(),
            pay.get("source_span"))
    for u in data.get("unusual_terms") or []:
        add("unusual_term", u.get("summary"), u.get("source_span"))
    return rows


def _reject(con, sha, filename, reason):
    cur = con.execute(
        "INSERT INTO contracts(sha256,filename,status,validated,ingested_at,note)"
        " VALUES(?,?,'REJECTED',0,?,?)", (sha, filename, db.now(), reason))
    cid = cur.lastrowid
    con.commit()
    audit.append("pipeline", "rejected",
                 {"contract_id": cid, "sha256": sha, "reason": reason})
    return cid


def ingest_file(path, do_validate=True):
    """Returns (contract_id, status) or (None, 'duplicate')."""
    with open(path, "rb") as fh:
        raw = fh.read()
    sha = hashlib.sha256(raw).hexdigest()
    filename = os.path.basename(path)
    con = db.connect()
    try:
        if con.execute("SELECT 1 FROM contracts WHERE sha256=?", (sha,)).fetchone():
            return None, "duplicate"                    # dedupe - reboot safe

        try:
            doc = ingest.parse(path)
        except ingest.Unsupported as exc:
            return _reject(con, sha, filename, str(exc)), "REJECTED"

        # The register row is created BEFORE extraction when the RAG lane is
        # driving, because its index()/extract() are keyed on a contract id.
        # It can only ever be PROPOSED, so D7 is untouched.
        cur = con.execute(
            "INSERT INTO contracts(sha256,filename,status,validated,"
            "ingested_at,fmt,converted_via,llm_mode,doctext)"
            " VALUES(?,?,'PROPOSED',?,?,?,?,?,?)",
            (sha, filename, 1 if do_validate else 0, db.now(),
             doc.fmt, doc.converted_via, "pending", doc.text))
        cid = cur.lastrowid
        con.commit()

        meta = {"extractor": "native"}
        if do_validate and EXTRACTOR == "rag":
            import retriever as R
            R.index(cid, doc)
            try:
                rows, computed, meta = R.extract_via_rag(cid, doc)
                model, mode = meta.get("model", "rag"), "rag"
            except Exception as exc:                     # noqa: BLE001
                con.execute("DELETE FROM contracts WHERE id=?", (cid,))
                con.commit()
                return _reject(con, sha, filename,
                               f"RAG extraction failed: {exc}"), "REJECTED"
        else:
            try:
                data, model, mode = extract.extract(
                    doc.text, retrieved=_retrieve(
                        "renewal notice period term end payment", cid))
            except extract.ExtractionUnavailable as exc:
                con.execute("DELETE FROM contracts WHERE id=?", (cid,))
                con.commit()
                return _reject(con, sha, filename,
                               f"extraction unavailable: {exc}"), "REJECTED"
            if do_validate:
                rows, computed = V.validate(data, doc.text, doc=doc)
            else:
                # Ablation mode (D13): no checks at all, and every row says
                # so. Flatten to the SAME field shape the validated path
                # produces -- the point of the ablation is that the output
                # looks equally confident, so it must be equally legible.
                rows = _unvalidated_rows(data)
                computed = {}

        con.execute("UPDATE contracts SET model=?, llm_mode=? WHERE id=?",
                    (model, mode, cid))
        for f, v, s, verdict, note, st, en, pg in rows:
            con.execute(
                "INSERT INTO extractions(contract_id,field,value,source_span,"
                "validator,note,span_start,span_end,page)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (cid, f, str(v) if v is not None else None, s, verdict, note,
                 st, en, pg))
        con.commit()
    finally:
        con.close()

    if EXTRACTOR != "rag":
        _index(cid, doc)
    audit.append("pipeline", "proposed",
                 {"contract_id": cid, "sha256": sha, "validated": do_validate,
                  "fmt": doc.fmt, "llm_mode": mode,
                  "converted_via": doc.converted_via,
                  "extractor": meta.get("extractor"),
                  "rag_can_approve": meta.get("their_can_approve"),
                  "rag_disagreements": meta.get("disagreements"),
                  "failures": sum(1 for r in rows if r[3] == "FAIL")})
    return cid, "PROPOSED"


def scan(do_validate=True):
    seen = []
    for d in INTAKE_DIRS:
        for p in sorted(glob.glob(os.path.join(d, "*"))):
            if os.path.isfile(p):
                cid, status = ingest_file(p, do_validate)
                if cid:
                    seen.append((os.path.basename(p), cid, status))
    return seen


if __name__ == "__main__":
    db.init()
    do_validate = "--no-validate" not in sys.argv
    if not do_validate:
        print("!! VALIDATION DISABLED - ablation mode !!")
    results = scan(do_validate)
    for name, cid, status in results:
        print(f"{status.lower()}: {name} -> contract {cid}")
    if not results:
        print("nothing new in intake")
