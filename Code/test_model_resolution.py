"""Reproduce the 404-as-unreachable defect and verify each fix.

The failure in the UI was 'extraction unavailable: model endpoint unreachable:
HTTP Error 404: Not Found' -- three wrong claims in one line. The endpoint was
up, the document was fine, and the real cause (a selected model the endpoint had
never loaded) appeared nowhere.

State-independent on purpose: it derives an unserved and a served model from the
catalog rather than testing whatever is currently selected. An earlier version
passed only while the box happened to be misconfigured, which is the opposite of
a regression test.
"""
import extract
import models

FAILURES = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + (f"  {detail}" if detail else ""))
    if not cond:
        FAILURES.append(label)


served_ids = [s["id"] for s in models.served()]
staged = models.staged()
live_entry = next((e for e in staged if e["live"]), None)
dead_entry = next((e for e in staged if not e["live"] and e["present"]), None)

print("=== state ===")
print(f"endpoint serving = {served_ids or 'nothing'}")
print(f"served model     = {live_entry['name'] if live_entry else 'none'}")
print(f"unserved model   = {dead_entry['name'] if dead_entry else 'none'}\n")

if not served_ids:
    print("SKIP: no inference endpoint is serving; start vLLM to run this.")
    raise SystemExit(0)

print("=== 1. live_wire refuses to guess an unserved model ===")
if dead_entry:
    name = dead_entry["name"]
    check("live_wire(unserved) is None", models.live_wire(name) is None)
    check("wire_name still falls back for record/replay",
          models.wire_name(name) == name, models.wire_name(name))
else:
    print("     (no staged-but-unserved model to test against)")

if live_entry:
    check("live_wire(served) returns the wire id",
          models.live_wire(live_entry["name"]) in served_ids,
          models.live_wire(live_entry["name"]))
    check("the wire id differs from the catalog name (the whole trap)",
          models.live_wire(live_entry["name"]) != live_entry["name"])

print("\n=== 2. the error names the real cause, not the network ===")
if dead_entry:
    try:
        extract.extract("This Agreement ends March 31, 2027.",
                        mode="live", model=dead_entry["name"])
        check("raised ExtractionUnavailable", False, "no exception")
    except extract.ExtractionUnavailable as exc:
        msg = str(exc)
        print(f"    -> {msg}")
        check("does NOT claim 'unreachable'", "unreachable" not in msg.lower())
        check("does NOT surface a bare 404", "404" not in msg)
        check("names the model that is not loaded", dead_entry["name"] in msg)
        check("says what IS being served", all(s in msg for s in served_ids))
        check("says what to do about it",
              "select" in msg.lower() or "restart" in msg.lower())
else:
    print("     (skipped: every staged model is being served)")

print("\n=== 3. a served model actually extracts ===")
if live_entry:
    try:
        data, model, mode = extract.extract(
            "This Agreement between Acme Corp and Beta LLC ends March 31, 2027.",
            mode="live", model=live_entry["name"])
        check("live extraction returns data", bool(data))
        check("attributed to the model that produced it",
              model == live_entry["name"], model)
        check("term_end found", (data.get("term_end") or {}).get("value")
              == "2027-03-31", str((data.get("term_end") or {}).get("value")))
        span = (data.get("term_end") or {}).get("source_span") or ""
        check("term_end quotes the document", "March 31, 2027" in span, span)
    except extract.ExtractionUnavailable as exc:
        check("served model extracts without refusal", False, str(exc)[:120])

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: " + "; ".join(FAILURES))
    raise SystemExit(1)
print("ALL MODEL RESOLUTION CHECKS PASSED")
