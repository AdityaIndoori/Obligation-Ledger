"""The credibility layer. V1-V7 from MASTER doc 3.6.

Verbatim from T8 except two signed-off changes:
  A1  renewal_term_months is now subject to int_in_span (V7b). The deck claims
      that check; without it the claim is false. Requires the auto_renewal
      source_span to quote the whole renewal clause, which contains both the
      renewal term and the notice period.
  B3  validate() accepts an optional ParsedDoc and returns span offsets + page
      per row so the UI can jump to the quote. Deterministic; no model input.

What these rules do NOT do: they confirm a value is supported by the text it
cites. They cannot confirm the model cited the RIGHT clause. That residual risk
is what the human approval gate is for.
"""
import re
from datetime import timedelta

from dateutil import parser as dateparser

QUOTES = {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'",
          "\u2013": "-", "\u2014": "-"}
MONEY_RE = re.compile(r"\d[\d,]*(?:\.\d{1,2})?")


def norm(s):
    if not s:
        return ""
    for a, b in QUOTES.items():
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip().lower()


def span_ok(span, doctext):
    """V1: the quote must actually exist in the document."""
    if not span or not span.strip():
        return False
    if span in doctext:
        return True
    return norm(span) in norm(doctext)


def parse_date(v):
    try:
        return dateparser.parse(v).date()
    except Exception:
        return None


def money_ok(value, span):
    """V4: the amount must be a real number and appear in its own quote."""
    if not value:
        return False
    m = MONEY_RE.search(str(value))
    if not m:
        return False
    return norm(m.group(0)) in norm(span or "")


# --- V7: value-in-span consistency. This is what catches a real quote
# --- paired with a fabricated value. Do not remove.
MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
DATE_LIKE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|(?:" + MONTHS + r")[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+(?:" + MONTHS + r")[a-z]*\.?,?\s+\d{4})\b",
    re.I)

UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
         "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
         "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
        "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}


def _spelled_ints(text):
    """Yield integers spelled in words: 'sixty', 'forty-five', 'one hundred'."""
    t = re.sub(r"[-\u2013\u2014]", " ", norm(text))
    toks = t.split()
    for i, w in enumerate(toks):
        if w in TENS:
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            yield TENS[w] + (UNITS[nxt] if nxt in UNITS and UNITS[nxt] < 10 else 0)
        elif w in UNITS:
            if toks[i + 1:i + 2] == ["hundred"]:
                yield UNITS[w] * 100
            else:
                yield UNITS[w]


def date_in_span(value, span):
    """V7a: the reported date must be parseable out of its own quote."""
    d = parse_date(value)
    if not d or not span:
        return False
    for cand in DATE_LIKE.findall(span):
        c = parse_date(cand)
        if c and c == d:
            return True
    return False


def int_in_span(value, span):
    """V7b: the reported integer must appear in its quote, as digits or words."""
    if value is None or not span:
        return False
    try:
        n = int(value)
    except (TypeError, ValueError):
        return False
    if re.search(rf"(?<!\d){n}(?!\d)", span):
        return True
    return n in set(_spelled_ints(span))


def validate(data, doctext, doc=None):
    """Returns (rows, computed).

    rows = [(field, value, span, verdict, note, span_start, span_end, page)]
    span_start/span_end/page are None unless `doc` (a ParsedDoc) is supplied.
    """
    rows, computed = [], {}

    def locate(span):
        if doc is None or not span:
            return (None, None, None)
        hit = doc.locate(span)
        return hit if hit else (None, None, None)

    def add(field, value, span, ok, note=""):
        start, end, page = locate(span)
        rows.append((field, value, span, "PASS" if ok else "FAIL", note,
                     start, end, page))

    # parties (V5)
    for p in data.get("parties", []) or []:
        name = p.get("name", "")
        ok = (bool(name) and norm(name) in norm(doctext)
              and span_ok(p.get("source_span"), doctext))
        add(f"party:{p.get('role') or 'party'}", name, p.get("source_span"), ok,
            "" if ok else "name or quote not found in document")

    # dates (V1 + V2 + V7a)
    eff = data.get("effective_date") or {}
    end = data.get("term_end") or {}
    d_eff, d_end = parse_date(eff.get("value")), parse_date(end.get("value"))
    eff_ok = (bool(d_eff)
              and span_ok(eff.get("source_span"), doctext)
              and date_in_span(eff.get("value"), eff.get("source_span")))
    add("effective_date", eff.get("value"), eff.get("source_span"), eff_ok,
        "" if eff_ok else "unparseable, unquoted, or date not present in its own quote")
    end_ok = (bool(d_end)
              and span_ok(end.get("source_span"), doctext)
              and date_in_span(end.get("value"), end.get("source_span")))
    if d_eff and d_end and d_end < d_eff:
        end_ok = False
    add("term_end", end.get("value"), end.get("source_span"), end_ok,
        "" if end_ok else "unparseable, unquoted, date absent from its quote, or before effective date")

    # renewal + THE COMPUTED DEADLINE (V3)
    ar = data.get("auto_renewal") or {}
    if ar.get("present"):
        nd = ar.get("notice_days")
        span = ar.get("source_span")
        ok = (isinstance(nd, int) and nd > 0
              and span_ok(span, doctext)
              and int_in_span(nd, span))                                # V7b
        add("notice_days", nd, span, ok,
            "" if ok else "notice period not quoted, or number absent from its quote")

        # A1: the renewal term is held to the same V7b standard as notice_days.
        rtm = ar.get("renewal_term_months")
        rtm_ok = span_ok(span, doctext) and int_in_span(rtm, span)
        add("renewal_term_months", rtm, span, rtm_ok,
            "" if rtm_ok else "value not found in its own quote")

        if ok and d_end:
            deadline = d_end - timedelta(days=nd)
            computed["notice_deadline"] = deadline.isoformat()
            rows.append(("notice_deadline", deadline.isoformat(), None,
                         "COMPUTED",
                         f"term_end minus {nd} days - calculated, not model output",
                         None, None, None))

    # money (V4)
    pay = data.get("payment") or {}
    if pay.get("amount"):
        ok = (money_ok(pay.get("amount"), pay.get("source_span"))
              and span_ok(pay.get("source_span"), doctext))
        add("payment_amount",
            f"{pay.get('currency','')} {pay.get('amount')}".strip(),
            pay.get("source_span"), ok,
            "" if ok else "amount not found in its quote")

    gl = data.get("governing_law") or {}
    if gl.get("value"):
        add("governing_law", gl.get("value"), gl.get("source_span"),
            span_ok(gl.get("source_span"), doctext))

    for u in data.get("unusual_terms", []) or []:
        add("unusual_term", u.get("summary"), u.get("source_span"),
            span_ok(u.get("source_span"), doctext))

    return rows, computed


def has_failures(rows):
    return any(r[3] == "FAIL" for r in rows)
