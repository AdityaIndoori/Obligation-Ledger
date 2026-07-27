"""Approval API + UI host. MASTER doc T11, plus:
  * all four obligation kinds (doc's required addition to T11)
  * multi-format upload
  * deterministic SELECT-only Ask (decision D-C)
  * span offsets / page surfaced per field (decision D-B)

Invariant (D7): status='COMMITTED' is reachable ONLY through POST /api/decide,
and only when no extraction row is FAIL. pipeline.py cannot write it.
"""
import os
import re
import sqlite3
import threading
from datetime import date, timedelta

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import audit
import db

TOKEN = os.environ.get("LEDGER_TOKEN", "demo-token")
OUT = os.environ.get("LEDGER_OUT", "/srv/ledger/outputs")
STATIC = os.environ.get("LEDGER_STATIC", "/srv/ledger/app/static")
UPLOADS = os.environ.get("LEDGER_UPLOADS", "/srv/ledger/data/uploads")
MODEL = os.environ.get("LLM_MODEL", "Qwen/Qwen3.6-35B-A3B-FP8")
LLM_MODE = os.environ.get("LLM_MODE", "live")

app = FastAPI(title="Obligation Ledger")


def auth(t):
    if t != f"Bearer {TOKEN}":
        raise HTTPException(401, "unauthorized")


@app.get("/")
def ui():
    return FileResponse(os.path.join(STATIC, "ui.html"))


@app.get("/api/meta")
def meta():
    import ingest
    import models
    ok, msg = audit.verify()
    cur = models.selected()
    # Whether the selection is ACTUALLY answerable, not just chosen. The header
    # claimed "live inference" purely from LLM_MODE, so it read green while
    # every extraction 404'd on a model the endpoint had never loaded. A mode
    # is an intention; this is the fact.
    served = models.live_wire(cur) if LLM_MODE == "live" else None
    return {"model": cur, "model_info": models.describe(cur),
            "llm_mode": LLM_MODE, "airgapped": True,
            "model_served": served is not None,
            "served_as": served,
            "audit_ok": ok, "audit_message": msg,
            "formats": [e.lstrip(".") for e in ingest.supported_extensions()]}


@app.get("/api/models")
def list_models():
    """What is staged on disk, which model is selected, and whether the
    inference endpoint is actually answering.

    The endpoint probe matters because a selection is only *effective* once
    something is serving. Until then the choice is real but LATENT, and the UI
    must say so rather than implying the selected model produced anything.
    """
    import models
    return {"selected": models.selected(),
            "models": models.staged(),
            "llm_mode": LLM_MODE,
            "endpoint": ENDPOINT_STATUS()}


def ENDPOINT_STATUS():
    """Probe $LLM_URL's /v1/models with a short timeout. Never raises."""
    import json as _json
    import urllib.error
    import urllib.request
    base = os.environ.get(
        "LLM_URL", "http://inference.local/v1/chat/completions")
    probe = base.replace("/chat/completions", "/models")
    try:
        with urllib.request.urlopen(probe, timeout=2) as r:
            payload = _json.loads(r.read())
        served = [m.get("id") for m in payload.get("data", []) if m.get("id")]
        return {"url": probe, "reachable": True, "serving": served}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"url": probe, "reachable": False, "error": str(exc)[:120],
                "serving": []}


@app.post("/api/models/select")
def select_model(body: dict, authorization: str = Header(None)):
    """Choose the model for SUBSEQUENT extractions.

    Does not rewrite history: every contract keeps the model that actually
    produced it, and the UI shows that per row. Selecting a model here is a
    forward-looking statement, never a retroactive one.
    """
    auth(authorization)
    import models
    name = (body or {}).get("model")
    try:
        models.select(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    entry = next((m for m in models.staged() if m["name"] == name), None)
    if entry and not entry["present"]:
        # Selectable but not on disk: say so rather than failing at extract time.
        audit.append("ui:model", "model_selected",
                     {"model": name, "staged": False})
        return JSONResponse({"selected": name, "staged": False,
                             "warning": "model is not staged on disk; "
                                        "extraction will fail until it is"},
                            status_code=202)
    if entry and not entry["live"]:
        # On disk, but the endpoint has different weights loaded. This is the
        # trap that produced a 404 mislabelled "endpoint unreachable": the
        # selection looked valid everywhere until the next extraction ran.
        # Warn at the moment of choosing, when it is still cheap to change.
        serving = [s.get("id") for s in models.served() if s.get("id")]
        audit.append("ui:model", "model_selected",
                     {"model": name, "staged": True, "served": False})
        return JSONResponse(
            {"selected": name, "staged": True, "served": False,
             "info": models.describe(name),
             "warning": "these weights are on disk but the inference endpoint "
                        "is not serving them" + (
                            f" (it is serving: {', '.join(serving)})"
                            if serving else "")
                        + "; extraction will be refused until vLLM is "
                          "restarted with them"},
            status_code=202)
    audit.append("ui:model", "model_selected", {"model": name, "staged": True})
    return {"selected": name, "staged": True, "info": models.describe(name)}


@app.get("/api/queue")
def queue():
    """Every contract, with FACT COUNTS per row.

    Deliberately counts (quotes, computed, human-set, failed) rather than
    scoring them. A "4/5 verified - 80%" summary would invent a reliability
    claim out of a mechanical string match; counting what exists does not.
    """
    con = db.connect(readonly=True)
    rows = con.execute(
        "SELECT c.id,c.filename,c.status,c.model,c.validated,c.ingested_at,"
        "c.fmt,c.converted_via,c.llm_mode,c.note,"
        "c.archived,c.archived_at,c.archived_by,"
        "(SELECT COUNT(*) FROM extractions e WHERE e.contract_id=c.id"
        "  AND e.validator='FAIL') AS failures,"
        "(SELECT COUNT(*) FROM extractions e WHERE e.contract_id=c.id"
        "  AND e.validator='PASS') AS quoted,"
        "(SELECT COUNT(*) FROM extractions e WHERE e.contract_id=c.id"
        "  AND e.validator='COMPUTED') AS computed,"
        "(SELECT COUNT(*) FROM extractions e WHERE e.contract_id=c.id"
        "  AND e.validator='HUMAN') AS human_set,"
        "(SELECT COUNT(*) FROM extractions e WHERE e.contract_id=c.id) AS fields"
        " FROM contracts c ORDER BY c.id DESC").fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/api/contract/{cid}")
def contract(cid: int):
    con = db.connect(readonly=True)
    c = con.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
    if not c:
        con.close()
        raise HTTPException(404, "not found")
    ex = con.execute(
        "SELECT id,field,value,source_span,validator,note,edited_by_human,"
        "span_start,span_end,page FROM extractions WHERE contract_id=?"
        " ORDER BY id", (cid,)).fetchall()
    ob = con.execute("SELECT kind,due_date,detail,status FROM obligations"
                     " WHERE contract_id=? ORDER BY due_date", (cid,)).fetchall()
    con.close()
    out = dict(c)
    doctext = out.pop("doctext", None) or ""
    return {"contract": out,
            "fields": [dict(r) for r in ex],
            "obligations": [dict(r) for r in ob],
            "doctext_chars": len(doctext)}


@app.get("/api/contract/{cid}/text")
def contract_text(cid: int, start: int | None = None, end: int | None = None):
    """The document text behind the receipts. Used by the UI to show a quote in
    context; ranges are clamped, never trusted from the client."""
    con = db.connect(readonly=True)
    row = con.execute("SELECT doctext FROM contracts WHERE id=?", (cid,)).fetchone()
    con.close()
    if not row:
        raise HTTPException(404, "not found")
    text = row["doctext"] or ""
    if start is None:
        return {"text": text, "start": 0, "end": len(text)}
    s = max(0, min(int(start), len(text)))
    e = max(s, min(int(end if end is not None else s + 400), len(text)))
    pad = 300
    ctx_s, ctx_e = max(0, s - pad), min(len(text), e + pad)
    return {"text": text[ctx_s:ctx_e], "start": ctx_s, "end": ctx_e,
            "quote_start": s, "quote_end": e}


def _recompute_deadline(con, cid):
    """Re-run the Python date arithmetic after a human corrects an input.

    D5/V3: the deadline is ALWAYS computed here, never taken from the model or
    from the partner. Correcting notice_days or term_end therefore has to
    recompute the deadline, or the register would carry a stale date that no
    longer follows from its own inputs.
    """
    import validate as V
    rows = {r["field"]: dict(r) for r in con.execute(
        "SELECT field,value,validator FROM extractions WHERE contract_id=?",
        (cid,)).fetchall()}
    nd_row, te_row = rows.get("notice_days"), rows.get("term_end")
    if not nd_row or not te_row:
        return None
    if nd_row["validator"] == "FAIL" or te_row["validator"] == "FAIL":
        return None
    try:
        days = int(str(nd_row["value"]).strip())
    except (TypeError, ValueError):
        return None
    end = V.parse_date(te_row["value"])
    if not end or days <= 0:
        return None
    from datetime import timedelta
    deadline = (end - timedelta(days=days)).isoformat()
    # Disclose WHICH inputs were human-set. The arithmetic is always
    # deterministic, but "computed from a reviewer's number" is a weaker claim
    # than "computed from two quoted values", and the UI must be able to say so.
    human_inputs = [n for n, r in (("notice_days", nd_row), ("term_end", te_row))
                    if r["validator"] == "HUMAN"]
    note = f"term_end minus {days} days - calculated, not model output"
    if human_inputs:
        note += f" (inputs set by reviewer: {', '.join(human_inputs)})"
    cur = con.execute(
        "UPDATE extractions SET value=?,note=? WHERE contract_id=?"
        " AND field='notice_deadline'", (deadline, note, cid))
    if cur.rowcount == 0:
        con.execute(
            "INSERT INTO extractions(contract_id,field,value,source_span,"
            "validator,note) VALUES(?,?,?,NULL,'COMPUTED',?)",
            (cid, "notice_deadline", deadline, note))
    return deadline


def _obligations_for(con, cid, fields):
    """The doc's required addition: emit all four obligation kinds.

    write_ics stays on notice_deadline only - calendar noise undermines the demo.
    """
    fields = [dict(f) for f in fields]
    by = {f["field"]: f for f in fields}
    made = []

    nd = by.get("notice_deadline")
    if nd and nd["value"]:
        con.execute("INSERT INTO obligations(contract_id,kind,due_date,detail)"
                    " VALUES(?,?,?,?)",
                    (cid, "renewal_notice", nd["value"],
                     "notice deadline - computed in code, not model output"))
        made.append(("renewal_notice", nd["value"]))
        write_ics(cid, nd["value"])

    te = by.get("term_end")
    if te and te["value"]:
        con.execute("INSERT INTO obligations(contract_id,kind,due_date,detail)"
                    " VALUES(?,?,?,?)",
                    (cid, "term_expiry", te["value"], "contract term ends"))
        made.append(("term_expiry", te["value"]))

    pay = by.get("payment_amount")
    if pay and pay["value"]:
        # Only when the payment's own quote carries a date - otherwise there is
        # no defensible due date and we do not invent one. Reuses the
        # validator's date grammar so "October 30, 2026" is understood too.
        import validate as V
        due = None
        for src in (pay["source_span"] or "", pay["note"] or ""):
            for cand in V.DATE_LIKE.findall(src):
                d = V.parse_date(cand)
                if d:
                    due = d.isoformat()
                    break
            if due:
                break
        if due:
            con.execute("INSERT INTO obligations(contract_id,kind,due_date,detail)"
                        " VALUES(?,?,?,?)",
                        (cid, "payment", due, f"payment due {pay['value']}"))
            made.append(("payment", due))

    anchor = (by.get("term_end") or {}).get("value")
    if anchor:
        for f in fields:
            if f["field"] == "unusual_term" and f["value"]:
                con.execute("INSERT INTO obligations(contract_id,kind,due_date,detail)"
                            " VALUES(?,?,?,?)",
                            (cid, "review_flag", anchor,
                             f"review before renewal: {f['value']}"))
                made.append(("review_flag", anchor))
    return made


@app.post("/api/decide")
def decide(body: dict, authorization: str = Header(None)):
    auth(authorization)
    try:
        cid, action = int(body["id"]), body["action"]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "id and action are required")
    who = body.get("who", "partner")
    con = db.connect()
    try:
        row = con.execute("SELECT status FROM contracts WHERE id=?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")
        fields = con.execute(
            "SELECT field,value,validator,note,source_span FROM extractions"
            " WHERE contract_id=?", (cid,)).fetchall()

        if action == "approve":
            # V6: commit is blocked while any validator has failed.
            if any(f["validator"] == "FAIL" for f in fields):
                raise HTTPException(409, "cannot commit: unresolved validation failures")
            if row["status"] == "COMMITTED":
                raise HTTPException(409, "already committed")
            con.execute("UPDATE contracts SET status='COMMITTED',decided_at=?,"
                        "decided_by=? WHERE id=?", (db.now(), who, cid))
            con.execute("DELETE FROM obligations WHERE contract_id=?", (cid,))
            made = _obligations_for(con, cid, fields)
            con.commit()
            audit.append(f"ui:{who}", "committed",
                         {"contract_id": cid, "obligations": made})
            # Deterministic memo from committed data only. Best-effort: a memo
            # failure must not un-commit an approved contract.
            memo_path = None
            try:
                import memo as memo_mod
                memo_path = memo_mod.memo(cid)
            except Exception as exc:                       # noqa: BLE001
                print(f"  ! memo generation failed (ignored): {exc}")
            return {"status": "COMMITTED", "obligations": made,
                    "memo": os.path.basename(memo_path) if memo_path else None}

        if action == "reject":
            con.execute("UPDATE contracts SET status='REJECTED',decided_at=?,"
                        "decided_by=? WHERE id=?", (db.now(), who, cid))
            con.commit()
            audit.append(f"ui:{who}", "rejected", {"contract_id": cid})
            return {"status": "REJECTED"}

        if action == "edit":
            field = body.get("field")
            if not field or "value" not in body:
                raise HTTPException(400, "field and value are required")
            # A human-entered value has NO quote provenance, so it must NOT be
            # flipped to PASS -- that would claim a receipt that does not
            # exist. HUMAN is its own verdict: trusted because a named person
            # took responsibility, not because a quote was matched.
            cur = con.execute(
                "UPDATE extractions SET value=?,validator='HUMAN',"
                "note=?,edited_by_human=1"
                " WHERE contract_id=? AND field=?",
                (body["value"], f"set by {who} on {db.now()[:10]}"
                                " - no quote provenance", cid, field))
            if cur.rowcount == 0:
                raise HTTPException(404, f"no such field: {field}")
            recomputed = _recompute_deadline(con, cid)
            con.commit()
            audit.append(f"ui:{who}", "edited",
                         {"contract_id": cid, "field": field,
                          "value": body["value"],
                          "recomputed": recomputed})
            return {"status": "EDITED", "field": field,
                    "recomputed": recomputed}

        raise HTTPException(400, "unknown action")
    finally:
        con.close()


@app.get("/api/register")
def register():
    con = db.connect(readonly=True)
    rows = con.execute(
        "SELECT c.id,c.filename,c.status,c.decided_by,c.decided_at,"
        "(SELECT value FROM extractions e WHERE e.contract_id=c.id"
        "  AND e.field='term_end') AS term_end,"
        "(SELECT value FROM extractions e WHERE e.contract_id=c.id"
        "  AND e.field='notice_deadline') AS notice_deadline,"
        "(SELECT value FROM extractions e WHERE e.contract_id=c.id"
        "  AND e.field='payment_amount') AS payment"
        " FROM contracts c WHERE c.status='COMMITTED' AND c.archived=0"
        " ORDER BY c.id DESC"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/api/deadlines")
def deadlines(days: int = 90):
    horizon = (date.today() + timedelta(days=days)).isoformat()
    con = db.connect(readonly=True)
    rows = con.execute(
        "SELECT o.due_date,o.kind,o.detail,c.filename,c.id AS contract_id"
        " FROM obligations o JOIN contracts c ON c.id=o.contract_id"
        " WHERE o.due_date<=? AND o.status='OPEN' AND c.archived=0"
        " ORDER BY o.due_date",
        (horizon,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/api/audit")
def audit_status():
    ok, msg = audit.verify()
    return {"ok": ok, "message": msg}


@app.get("/api/audit/log")
def audit_log(limit: int = 50):
    recs = audit._lines()[-limit:]
    return list(reversed(recs))


# --------------------------------------------------------------- Ask / chat
# Decision D-C, extended: intent matching and SQL stay deterministic in Python,
# and the model is used ONLY to turn fetched rows into prose. It never writes
# SQL, never sees the database, and cannot invent a contract -- and if it is
# unavailable the endpoint still answers from a deterministic summary, so the
# Ask tab cannot be dead on stage. See chat.py for the full rationale.
@app.get("/api/ask")
def ask(q: str, prose: bool = True, context: int | None = None):
    """Answer a question about the register.

    `context` is the contract id the previous turn was about, so a follow-up
    like "what is its status?" resolves. Only an id crosses the wire -- no chat
    history is stored server-side or handed to the model.
    """
    import chat
    try:
        return chat.answer(q, use_model=prose, context_id=context)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except RuntimeError as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/delete")
def delete(body: dict, authorization: str = Header(None)):
    """Soft-delete (archive) a contract, or restore one.

    NOT a physical delete, deliberately. audit.jsonl is append-only and each
    record commits to its predecessor, so removing history would break the
    tamper-evidence this product is built on. Archiving hides the contract from
    the working views, records who did it and when, and writes the act itself
    into the chain. `restore` reverses it.

    Committed contracts are refused unless force=true: they carry obligations
    a person approved, so hiding one silently would drop dates someone is
    relying on. With force, the obligations are closed rather than deleted.
    """
    auth(authorization)
    try:
        cid = int(body["id"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "id is required")
    who = body.get("who", "partner")
    restore = bool(body.get("restore"))
    force = bool(body.get("force"))

    con = db.connect()
    try:
        row = con.execute("SELECT status,archived,filename FROM contracts"
                          " WHERE id=?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "not found")

        if restore:
            con.execute("UPDATE contracts SET archived=0,archived_at=NULL,"
                        "archived_by=NULL WHERE id=?", (cid,))
            con.execute("UPDATE obligations SET status='OPEN'"
                        " WHERE contract_id=? AND status='ARCHIVED'", (cid,))
            con.commit()
            audit.append(f"ui:{who}", "restored",
                         {"contract_id": cid, "filename": row["filename"]})
            return {"status": "RESTORED", "id": cid}

        if row["archived"]:
            raise HTTPException(409, "already deleted")
        if row["status"] == "COMMITTED" and not force:
            raise HTTPException(
                409, "this contract is committed and its obligations are live; "
                     "resend with force=true to delete it and close them")

        closed = con.execute(
            "UPDATE obligations SET status='ARCHIVED'"
            " WHERE contract_id=? AND status='OPEN'", (cid,)).rowcount
        con.execute("UPDATE contracts SET archived=1,archived_at=?,"
                    "archived_by=? WHERE id=?", (db.now(), who, cid))
        con.commit()
        audit.append(f"ui:{who}", "deleted",
                     {"contract_id": cid, "filename": row["filename"],
                      "prior_status": row["status"],
                      "obligations_closed": closed, "forced": force})
        return {"status": "DELETED", "id": cid, "obligations_closed": closed,
                "recoverable": True}
    finally:
        con.close()


# In-flight uploads. A dict is enough: the register is the durable record, and
# a job entry only exists to report progress on something the user is watching.
JOBS: dict[str, dict] = {}


def _ingest_job(job_id: str, dest: str, name: str, nbytes: int, ext: str,
                unknown_ext: bool):
    """Parse + extract in a worker thread, recording the outcome on the job."""
    import hashlib
    import pipeline
    job = JOBS[job_id]
    try:
        with open(dest, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        cid, status = pipeline.ingest_file(dest)
        if cid is None:                    # sha256 dedupe: identical bytes
            con = db.connect(readonly=True)
            row = con.execute("SELECT id,filename FROM contracts"
                              " WHERE sha256=?", (sha,)).fetchone()
            con.close()
            job.update(state="done", status="DUPLICATE",
                       id=row["id"] if row else None,
                       message="byte-identical to "
                               + (row["filename"] if row
                                  else "an existing document")
                               + ", already in the register")
            return
        job.update(state="done", status=status, id=cid)
        if status == "REJECTED":
            con = db.connect(readonly=True)
            row = con.execute("SELECT note FROM contracts WHERE id=?",
                              (cid,)).fetchone()
            con.close()
            job["reason"] = row["note"] if row else "refused"
        if unknown_ext:
            job["warning"] = (f"'{ext}' is not a known extension; it was "
                              "identified by content instead")
    except Exception as exc:               # noqa: BLE001
        # The bytes are on disk either way; never imply they were processed.
        job.update(state="done", status="SAVED",
                   warning=f"saved, but ingestion failed: {exc}")


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    """Save an uploaded document and start extracting it in the background.

    Returns 202 with a job id immediately. Extraction of a real contract takes
    ~16s against the local model, and holding the HTTP request open for that
    long made the browser abort it -- which surfaced as "Failed to fetch" on
    the queue polls firing alongside it. The work is the same; only the waiting
    moved off the request.

    There is no watcher to do this instead: the 60-second cron is the OpenClaw
    lane (T14a) and does not exist yet. When it lands, sha256 dedupe makes the
    double-scan a no-op.
    """
    import uuid

    import ingest
    os.makedirs(UPLOADS, exist_ok=True)
    name = os.path.basename(file.filename or "upload")
    ext = os.path.splitext(name)[1].lower()
    unknown_ext = bool(ext) and ext not in set(ingest.supported_extensions())
    dest = os.path.join(UPLOADS, name)
    data = await file.read()
    with open(dest, "wb") as fh:
        fh.write(data)

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {"job": job_id, "state": "extracting", "saved": name,
                    "bytes": len(data), "ext": ext, "id": None,
                    "status": None}
    threading.Thread(target=_ingest_job, daemon=True,
                     args=(job_id, dest, name, len(data), ext,
                           unknown_ext)).start()
    return JSONResponse(JOBS[job_id], status_code=202)


@app.get("/api/upload/{job_id}")
def upload_status(job_id: str):
    """Poll an upload. 404 once forgotten; the register is the durable record."""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "unknown or expired upload job")
    if job["state"] == "done":
        # Hand it back once, then stop holding it: this is progress reporting,
        # not a second source of truth.
        JOBS.pop(job_id, None)
    return job


@app.get("/api/formats")
def formats():
    import ingest
    return {"supported": [e.lstrip(".") for e in ingest.supported_extensions()]}


@app.get("/api/outputs/{cid}")
def outputs(cid: int):
    """Artifacts produced for a committed contract: the memo and the .ics."""
    items = []
    for name, kind in ((f"memo_{cid}.md", "memo"),
                       (f"contract_{cid}_notice.ics", "calendar")):
        path = os.path.join(OUT, name)
        if os.path.exists(path):
            items.append({"name": name, "kind": kind,
                          "bytes": os.path.getsize(path)})
    return items


@app.get("/api/outputs/{cid}/{name}")
def output_file(cid: int, name: str):
    """Serve one artifact. Filename is whitelisted, never joined from input."""
    allowed = {f"memo_{cid}.md": "text/markdown",
               f"contract_{cid}_notice.ics": "text/calendar"}
    if name not in allowed:
        raise HTTPException(404, "not found")
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        raise HTTPException(404, "not generated")
    return FileResponse(path, media_type=allowed[name], filename=name)


def write_ics(cid, due):
    os.makedirs(OUT, exist_ok=True)
    d = due.replace("-", "")
    ics = ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Obligation Ledger//EN\n"
           "BEGIN:VEVENT\n"
           f"UID:contract-{cid}-notice@obligation-ledger.local\n"
           f"DTSTART;VALUE=DATE:{d}\nDTEND;VALUE=DATE:{d}\n"
           f"SUMMARY:Contract {cid} - renewal notice due\n"
           "END:VEVENT\nEND:VCALENDAR\n")
    with open(os.path.join(OUT, f"contract_{cid}_notice.ics"), "w") as fh:
        fh.write(ics)
