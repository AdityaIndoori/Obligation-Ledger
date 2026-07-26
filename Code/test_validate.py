"""Validator test suite. MASTER doc T8 assertions verbatim, plus A1/B3 cases.

Fix validate.py, never this test.
"""
from validate import (date_in_span, has_failures, int_in_span, money_ok,
                      span_ok, validate)

DOC = ("This Agreement is effective January 1, 2026 and shall remain in force "
       "until March 31, 2027. It shall automatically renew for successive "
       "twelve-month terms unless either party gives written notice at least "
       "sixty (60) days prior to expiry. Fees are USD 120,000 per annum.")

# The A1 span: the whole renewal clause, so both the renewal term and the
# notice period are recoverable from the text that is actually cited.
RENEWAL_SPAN = ("automatically renew for successive twelve-month terms unless "
                "either party gives written notice at least sixty (60) days "
                "prior to expiry")

assert span_ok("until March 31, 2027", DOC)
assert span_ok("until   MARCH 31, 2027", DOC)      # normalised match
assert not span_ok("until April 30, 2027", DOC)    # hallucinated quote rejected
assert money_ok("120,000", "Fees are USD 120,000 per annum")
assert not money_ok("150,000", "Fees are USD 120,000 per annum")

good = {"parties": [],
        "effective_date": {"value": "2026-01-01",
                           "source_span": "effective January 1, 2026"},
        "term_end": {"value": "2027-03-31",
                     "source_span": "until March 31, 2027"},
        "auto_renewal": {"present": True, "renewal_term_months": 12,
                         "notice_days": 60,
                         "source_span": RENEWAL_SPAN}}
rows, computed = validate(good, DOC)
assert not has_failures(rows), [r for r in rows if r[3] == "FAIL"]
assert computed["notice_deadline"] == "2027-01-30"   # 2027-03-31 minus 60 days

bad = dict(good, term_end={"value": "2028-03-31",
                           "source_span": "until March 31, 2028"})
rows2, _ = validate(bad, DOC)
assert has_failures(rows2)                            # fabricated quote caught

# V7 -- the case that used to slip through: REAL quote, WRONG value
assert date_in_span("2027-03-31", "until March 31, 2027")
assert not date_in_span("2027-03-31", "until March 31, 2026")   # real span, wrong value
assert int_in_span(60, "at least sixty (60) days prior")
assert int_in_span(60, "at least sixty days prior")             # words only
assert not int_in_span(90, "at least sixty (60) days prior")    # real span, wrong value

sneaky = dict(good, term_end={"value": "2027-03-31",
                              "source_span": "effective January 1, 2026"})
rows3, _ = validate(sneaky, DOC)
assert has_failures(rows3)          # quote is genuine, value is not in it -> caught

# --- A1: renewal_term_months is subject to V7b -------------------------------
# The deck shows this field failing with "value not found in its own quote".
# It must be able to actually do that.
def field(rows, name):
    return next(r for r in rows if r[0] == name)

assert field(rows, "renewal_term_months")[3] == "PASS"

wrong_term = dict(good, auto_renewal=dict(good["auto_renewal"],
                                          renewal_term_months=24))
rows4, _ = validate(wrong_term, DOC)
r = field(rows4, "renewal_term_months")
assert r[3] == "FAIL", r
assert r[4] == "value not found in its own quote", r
# and the failure is contained: the notice deadline still computes correctly,
# because notice_days is independently verified.
_, computed4 = validate(wrong_term, DOC)
assert computed4["notice_deadline"] == "2027-01-30"

# A narrow span that omits the renewal term must fail -- this is the exact
# defect A1 exists to catch, not a hypothetical one.
narrow = dict(good, auto_renewal=dict(good["auto_renewal"],
                                      source_span="at least sixty (60) days prior to expiry"))
rows5, _ = validate(narrow, DOC)
assert field(rows5, "renewal_term_months")[3] == "FAIL"
assert field(rows5, "notice_days")[3] == "PASS"   # notice is still fine

# --- B3: span offsets and page resolution ------------------------------------
import ingest

doc = ingest.ParsedDoc(text=DOC, pages=[ingest.Page(1, DOC, 0)], fmt="txt")
rows6, _ = validate(good, DOC, doc=doc)
te = field(rows6, "term_end")
start, end, page = te[5], te[6], te[7]
assert start is not None and end is not None, te
assert DOC[start:end] == "until March 31, 2027", DOC[start:end]
assert page == 1, page

# offsets resolve across a multi-page document
p1, p2 = "Page one text. " * 20, "The term ends until March 31, 2027 here."
multi = ingest.ParsedDoc(
    text=p1 + "\n\n" + p2,
    pages=[ingest.Page(1, p1, 0), ingest.Page(2, p2, len(p1) + 2)],
    fmt="pdf")
rows7, _ = validate(good, multi.text, doc=multi)
te2 = field(rows7, "term_end")
assert te2[7] == 2, te2          # quote lives on page 2
assert multi.text[te2[5]:te2[6]] == "until March 31, 2027"

# a COMPUTED row carries no span -- it is not quoted from anywhere
nd = field(rows6, "notice_deadline")
assert nd[3] == "COMPUTED" and nd[2] is None and nd[7] is None

print("ALL VALIDATOR TESTS PASSED")
