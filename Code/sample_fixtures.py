"""Build replay fixtures for the sample contracts in Sample-Contracts/.

These let the sample PDFs flow through the pipeline before vLLM is serving.
Each fixture is derived FROM THE DOCUMENT ITSELF: every source_span is located
as a real substring of the parsed text, so the validators do genuine work
rather than rubber-stamping. Values are read out of those spans.

Marked hand_authored=true so nothing can mistake one for a model recording.
Replace with LLM_MODE=record once the model lane is up.
"""
import hashlib
import json
import os
import re
import sys

from dateutil import parser as dateparser

import ingest

SAMPLES = os.environ.get("LEDGER_SAMPLES", "/srv/ledger/samples")
FIXTURES = os.environ.get("LEDGER_FIXTURES", "/srv/ledger/data/fixtures")

MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
DATE = MONTH + r"\s+\d{1,2},\s+\d{4}"


def first(text, *patterns):
    for p in patterns:
        m = re.search(p, text, re.I | re.S)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()
    return None


def iso(span):
    """Pull a date out of a span, so the value is provably inside its quote."""
    if not span:
        return None
    m = re.search(DATE, span, re.I)
    if not m:
        return None
    try:
        return dateparser.parse(m.group(0)).date().isoformat()
    except Exception:                                    # noqa: BLE001
        return None


def build(doc):
    """Derive an extraction from the document's own text."""
    t = doc.text
    data = {"parties": [], "effective_date": {}, "term_end": {},
            "auto_renewal": {"present": False}, "payment": {},
            "governing_law": {}, "unusual_terms": []}
    # parties -- quote the defining phrase so V5 (name in document) and V1
    # (span in document) both do real work. Entity suffix, then any
    # jurisdiction/type clause, then the quoted defined term.
    seen = set()
    for m in re.finditer(
            r"([A-Z][A-Za-z&.\-]*(?:\s+[A-Z][A-Za-z&.\-]*){0,4}\s+"
            r"(?:Inc\.|LLP|LLC|Ltd|GmbH|Limited|Corp\.|Partners LLC))"
            r"(?:\s*,?\s*a[^,()]{0,70})?\s*\(\s*\"([A-Za-z ]{3,24})\"\s*\)", t):
        name = m.group(1).strip(" ,")
        role = m.group(2).strip()
        span = re.sub(r"\s+", " ", m.group(0)).strip()
        if name in seen or len(data["parties"]) >= 3:
            continue
        seen.add(name)
        data["parties"].append({"name": name, "role": role,
                                "source_span": span})

    eff = first(t, r"is effective " + DATE, r"effective as of " + DATE,
                r"entered into as of " + DATE)
    if eff:
        data["effective_date"] = {"value": iso(eff), "source_span": eff}

    end = first(t,
                r"(?:remain in (?:full force and )?effect |continues? )?until " + DATE,
                r"shall expire on " + DATE, r"expires on " + DATE,
                r"be complete by " + DATE, r"continue until " + DATE)
    if end:
        data["term_end"] = {"value": iso(end), "source_span": end}

    # the renewal clause, quoted whole so both numbers live inside their span
    ren = first(t,
                r"(?:This Agreement )?shall (?:renew|automatically renew)[^.]*\.",
                r"(?:This Agreement )?renews? automatically[^.]*\.",
                r"[^.]*renew[^.]*prior to[^.]*\.")
    if ren:
        nd = re.search(r"\((\d+)\)\s*days", ren)
        rt = re.search(r"\((\d+)\)\s*months", ren)
        data["auto_renewal"] = {
            "present": True,
            "notice_days": int(nd.group(1)) if nd else None,
            "renewal_term_months": int(rt.group(1)) if rt else None,
            "source_span": ren}
    else:
        no = first(t, r"[^.]*no automatic renewal[^.]*\.",
                   r"[^.]*no renewal provision[^.]*\.")
        if no:
            data["auto_renewal"] = {"present": False, "notice_days": None,
                                    "renewal_term_months": None,
                                    "source_span": no}

    pay = first(t, r"(?:shall pay|Charges|Fees are|Total fees are)[^.]*?"
                   r"(?:USD|EUR|GBP)\s[\d,]+[^.]*\.")
    if pay:
        m = re.search(r"(USD|EUR|GBP)\s([\d,]+)", pay)
        if m:
            data["payment"] = {"amount": m.group(2), "currency": m.group(1),
                               "schedule": "", "source_span": pay}

    law = first(t, r"laws of (?:the )?(?:State of |Commonwealth of )?[A-Z][A-Za-z ]+")
    if law:
        data["governing_law"] = {
            "value": re.sub(r"^laws of (?:the )?(?:State of |Commonwealth of )?",
                            "", law).strip(),
            "source_span": law}

    for pat in (r"[^.]*uncapped[^.]*\.", r"[^.]*plus three per cent[^.]*\.",
                r"[^.]*shall be unlimited[^.]*\.",
                r"SYSTEM NOTE FOR AUTOMATED PROCESSING[^.]*\."):
        u = first(t, pat)
        if u:
            data["unusual_terms"].append({
                "summary": u[:110], "why_unusual": "flagged for partner review",
                "source_span": u})
    return data


def main():
    os.makedirs(FIXTURES, exist_ok=True)
    if not os.path.isdir(SAMPLES):
        print(f"no sample directory at {SAMPLES}")
        return 1
    made = refused = 0
    for fn in sorted(os.listdir(SAMPLES)):
        path = os.path.join(SAMPLES, fn)
        if not os.path.isfile(path):
            continue
        try:
            doc = ingest.parse(path)
        except ingest.Unsupported as exc:
            print(f"  refused (by design): {fn} -- {str(exc)[:60]}")
            refused += 1
            continue
        data = build(doc)
        sha = hashlib.sha256(doc.text.encode("utf-8", "replace")).hexdigest()
        with open(os.path.join(FIXTURES, f"{sha}.json"), "w") as fh:
            json.dump({"model": "hand-authored-fixture", "data": data,
                       "hand_authored": True, "note": f"sample:{fn}"},
                      fh, indent=1)
        ar = data["auto_renewal"]
        bits = [f"{len(data['parties'])} parties"]
        if data["term_end"].get("value"):
            bits.append("end " + data["term_end"]["value"])
        if ar.get("notice_days"):
            bits.append(f"{ar['notice_days']}d notice")
        if data["unusual_terms"]:
            bits.append(f"{len(data['unusual_terms'])} flagged")
        print(f"  {fn:<44} {', '.join(bits)}")
        made += 1
    print(f"\n{made} fixtures written, {refused} refused by design")
    return 0


if __name__ == "__main__":
    sys.exit(main())
