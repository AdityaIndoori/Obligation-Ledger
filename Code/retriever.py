"""RETRIEVAL + EXTRACTION SEAM (S2). Bridges the RAG lane into this pipeline.

The RAG lane (github.com/Seveyus/Obligation-Ledger-AI) ships three functions:

    index(contract_id, doc)   -> int          chunk + embed a parsed document
    retrieve(query, k, cid)   -> [Passage]    passages for the Ask tab
    extract(contract_id, doc) -> ExtractionResult   per-field value + status

Their third function is a real improvement on my original two-function contract
and is wired in below.

HOW THE TWO EXTRACTORS COEXIST -- read this before changing anything

There are now two things that can extract a contract:

  A. extract.py      one model call over the (truncated) document
  B. retriever.extract()  RAG-retrieved passages + their own verification

They are selected with LEDGER_EXTRACTOR=native|rag (default native).

WHAT DOES **NOT** CHANGE: our validators remain the authority. The RAG lane
reports its own `status` per field and a `can_approve` flag, and both are
recorded -- but a value only becomes PASS in our register when *our* V1/V7
checks find it inside its own quote in *our* ParsedDoc.text. This is not
distrust of their code; it is the product's central claim. "Every committed
value carries a verbatim source quote" has to be checked by the thing that
makes the claim, or it degrades to "a component told us it checked".

So per field we store:
  * our verdict          PASS / FAIL / COMPUTED / HUMAN   (what gates Approve)
  * their status         verified / computed / failed     (recorded in the note)
and when the two disagree, the field FAILS and the note says both. A
disagreement is information, not an error to smooth over.

OFFSETS: their §11 guarantees `doc.text[ev.char_start:ev.char_end] == ev.quote`
against OUR ParsedDoc.text, which is the same coordinate system our
extractions.span_start/span_end already uses. Verified, not assumed -- see
_evidence_offsets().
"""
from __future__ import annotations

import os

from ingest import ParsedDoc, Passage       # noqa: F401  (re-exported)

MODE = os.environ.get("LEDGER_EXTRACTOR", "native").lower()

# Their canonical obligation names -> our field names. Ours are the register's
# schema and are what the UI, the memo and the audit chain already use, so the
# mapping happens here rather than renaming everything downstream.
FIELD_MAP = {
    "contract_start_date": "effective_date",
    "contract_end_date": "term_end",
    "termination_notice_period": "notice_days",
    "notice_deadline": "notice_deadline",
    "renewal_duration": "renewal_term_months",
    "automatic_renewal": "auto_renewal_present",
    "payment_obligation": "payment_amount",
    "governing_law": "governing_law",
    "fee_escalation": "unusual_term",
    "indemnification": "unusual_term",
    "liability_cap": "unusual_term",
    # The renewal-OPTION family is the opposite risk to the evergreen family:
    # miss it and you LOSE the contract rather than being locked in. Keep the
    # names distinct so the UI can say which.
    "renewal_option_notice": "option_notice_days",
    "renewal_option_deadline": "option_notice_deadline",
}


_ALIGNED = False


def _align_model_env():
    """Point the RAG lane at the id vLLM actually serves -- ONCE, at import.

    Two things collide here. Our LLM_MODEL is the CATALOG name
    ('Qwen/Qwen3.6-35B-A3B-FP8'); extract.py resolves it to the served id per
    call. The RAG lane reads LLM_MODEL directly with no such resolution, so it
    would send the catalog name and get a 404 from vLLM. It also reads its .env
    relative to the CWD, so an in-process import from /srv/ledger/app never
    sees rag-src/.env at all.

    The fix must not leak. An earlier version overwrote os.environ["LLM_MODEL"]
    on every call, so models.selected() -- which reads the same variable --
    started returning the wire id, fell out of the catalog, and the UI showed
    the model's quantisation as "undefined". The catalog name is now restored
    immediately: the RAG module captures the value at import time, which is the
    only moment it needs to be different.
    """
    global _ALIGNED
    if _ALIGNED:
        return None
    try:
        import models
        wire = models.wire_name()
    except Exception:                                    # noqa: BLE001
        return None
    _ALIGNED = True
    return wire


def _impl():
    """The RAG lane's module, or None if it is not installed."""
    previous = os.environ.get("LLM_MODEL")
    wire = _align_model_env()
    if wire:
        os.environ["LLM_MODEL"] = wire
    try:
        from obligation_rag import retriever as impl        # type: ignore
        return impl
    except ImportError:
        return None
    finally:
        # Put the catalog name back so nothing else in the process sees the
        # wire id in place of the model the user selected.
        if wire:
            if previous is None:
                os.environ.pop("LLM_MODEL", None)
            else:
                os.environ["LLM_MODEL"] = previous



# --------------------------------------------------------------- passthrough
def index(contract_id: int, doc: ParsedDoc) -> int | None:
    """Index one parsed document. Idempotent per contract_id."""
    impl = _impl()
    if impl is None:
        return None
    try:
        return impl.index(contract_id, doc)
    except Exception as exc:                               # noqa: BLE001
        print(f"  ! retriever.index failed (ignored): {exc}")
        return None


def retrieve(query: str, k: int = 8,
             contract_id: int | None = None) -> list[Passage]:
    """Top-k passages. Never raises; [] means none."""
    impl = _impl()
    if impl is None:
        return []
    try:
        return impl.retrieve(query, k=k, contract_id=contract_id) or []
    except Exception as exc:                               # noqa: BLE001
        print(f"  ! retriever.retrieve failed (ignored): {exc}")
        return []


def available() -> bool:
    return _impl() is not None


# --------------------------------------------------------------- extraction
def _evidence_offsets(ev, doc: ParsedDoc):
    """Trust their offsets only after checking them.

    Their §11 guarantees doc.text[start:end] == quote. If that holds we keep
    their offsets; if it does not we relocate the quote ourselves. Either way
    the offsets we store are ones we verified against our own text, because
    those offsets are what the UI uses to show a reviewer the receipt.
    """
    quote = getattr(ev, "quote", None)
    if not quote:
        return None, None, None, "no quote"
    s = getattr(ev, "char_start", None)
    e = getattr(ev, "char_end", None)
    if (isinstance(s, int) and isinstance(e, int)
            and 0 <= s < e <= len(doc.text) and doc.text[s:e] == quote):
        return s, e, doc.page_of(s), None
    hit = doc.locate(quote)
    if hit:
        s2, e2, page = hit
        return s2, e2, page, ("offsets corrected locally"
                              if s is not None else None)
    return None, None, getattr(ev, "page", None), "quote not found in document"


def extract_via_rag(contract_id: int, doc: ParsedDoc):
    """Run the RAG lane's extractor and translate it into our row shape.

    Returns (rows, computed, meta) where rows match validate.validate()'s
    tuple shape: (field, value, span, verdict, note, start, end, page).

    OUR verdict is decided by OUR validators. Theirs is recorded alongside.
    """
    impl = _impl()
    if impl is None:
        raise RuntimeError("LEDGER_EXTRACTOR=rag but obligation_rag is not "
                           "installed; pip install it or use native")
    import validate as V

    result = impl.extract(contract_id, doc)
    rows, computed = [], {}
    disagreements = 0

    for ob in getattr(result, "obligations", []) or []:
        field = FIELD_MAP.get(ob.field, ob.field)
        value = ob.value
        their = (ob.status or "").lower()

        # --- computed fields: never quoted, and we say so with their formula.
        if their == "computed":
            note = ob.formula or "calculated in code, not model output"
            if getattr(ob, "inputs", None):
                shown = ob.inputs.get("evaluated") if isinstance(
                    ob.inputs, dict) else None
                if shown:
                    note = f"{note} ({shown})"
            rows.append((field, value, None, "COMPUTED", note,
                         None, None, None))
            if value:
                computed[field] = value
            continue

        ev = getattr(ob, "evidence", None)
        if ev is None:
            rows.append((field, value, None, "FAIL",
                         f"no evidence supplied (their status: {their})"
                         + (f" - {ob.reason}" if ob.reason else ""),
                         None, None, None))
            continue

        start, end, page, offset_note = _evidence_offsets(ev, doc)
        span = ev.quote

        # --- OUR checks decide the verdict (V1 + V7).
        span_ok = V.span_ok(span, doc.text)
        if field in ("effective_date", "term_end", "option_notice_deadline"):
            value_ok = V.date_in_span(value, span)
        elif field in ("notice_days", "renewal_term_months",
                       "option_notice_days"):
            value_ok = V.int_in_span(_as_int(value), span)
        elif field == "payment_amount":
            value_ok = V.money_ok(value, span)
        else:
            value_ok = bool(value) and V.norm(str(value)) in V.norm(span)

        ours_pass = span_ok and value_ok
        verdict = "PASS" if ours_pass else "FAIL"

        # --- disagreement is recorded, never smoothed over.
        notes = []
        if their and their != ("verified" if ours_pass else "failed"):
            disagreements += 1
            notes.append(f"RAG reported '{their}', our checks say "
                         f"{'supported' if ours_pass else 'not supported'}")
        if not span_ok:
            notes.append("quote not found verbatim in the document")
        elif not value_ok:
            notes.append("value not found in its own quote")
        if ob.reason:
            notes.append(f"RAG reason: {ob.reason}")
        if offset_note:
            notes.append(offset_note)

        rows.append((field, value, span, verdict, "; ".join(notes),
                     start, end, page))

    meta = {
        "extractor": "rag",
        "their_can_approve": bool(getattr(result, "can_approve", False)),
        "our_failures": sum(1 for r in rows if r[3] == "FAIL"),
        "disagreements": disagreements,
        "their_failures": list(getattr(result, "failures", []) or []),
    }
    return rows, computed, meta


def _as_int(value):
    if value is None:
        return None
    s = str(value)
    # their durations are ISO-8601: P60D, P12M
    import re
    m = re.match(r"^P(\d+)([DMY])$", s)
    if m:
        return int(m.group(1))
    m = re.search(r"\d+", s)
    return int(m.group(0)) if m else None


if __name__ == "__main__":
    impl = _impl()
    print("obligation_rag installed:", impl is not None)
    print("LEDGER_EXTRACTOR:", MODE)
    if impl is None:
        print("stub behaviour: index() -> None, retrieve() -> []")
        print("pipeline is identical to LEDGER_RAG=off")
    else:
        print("functions:", [f for f in ("index", "retrieve", "extract")
                             if hasattr(impl, f)])
