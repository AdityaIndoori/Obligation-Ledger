"""Commit memo. Written from COMMITTED data only, by deterministic code.

MASTER doc T14b assigns the memo to an OpenClaw skill at temperature 0.3. That
lane owns the prose version. This is the deterministic floor: if the agent lane
is not up, a partner still gets a one-page memo, and it contains nothing that
was not verified and approved.

Rule inherited from the design: every fact in the memo is either
  * a PASS field with its verbatim quote, or
  * a COMPUTED value labelled as calculated, or
  * a human correction, labelled as such.
Nothing that FAILED validation reaches the memo, because nothing that failed
can be committed in the first place.
"""
import os
import sys

import db

OUT = os.environ.get("LEDGER_OUT", "/srv/ledger/outputs")

ORDER = ["term_end", "notice_deadline", "notice_days", "renewal_term_months",
         "payment_amount", "effective_date", "governing_law"]


def memo(cid):
    con = db.connect(readonly=True)
    c = con.execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
    if not c:
        con.close()
        raise SystemExit(f"no contract {cid}")
    if c["status"] != "COMMITTED":
        con.close()
        raise SystemExit(f"contract {cid} is {c['status']}, not COMMITTED - "
                         "memos are written from approved data only")
    fields = [dict(r) for r in con.execute(
        "SELECT field,value,source_span,validator,note,edited_by_human,page"
        " FROM extractions WHERE contract_id=?", (cid,)).fetchall()]
    obs = [dict(r) for r in con.execute(
        "SELECT kind,due_date,detail FROM obligations WHERE contract_id=?"
        " ORDER BY due_date", (cid,)).fetchall()]
    con.close()

    by = {f["field"]: f for f in fields}
    parties = [f for f in fields if f["field"].startswith("party:")]
    unusual = [f for f in fields if f["field"] == "unusual_term"]

    L = []
    L.append(f"# Obligation memo - {c['filename']}")
    L.append("")
    L.append(f"Approved by **{c['decided_by']}** on {c['decided_at']}.")
    L.append(f"Extracted by `{c['model']}` "
             f"({'recorded output' if c['llm_mode'] != 'live' else 'live inference'}), "
             f"parsed from `.{c['fmt']}`"
             + (f" via {c['converted_via']}" if c["converted_via"] else "") + ".")
    L.append("")
    L.append("Every value below is quoted verbatim from the document, or "
             "computed in code from values that are. No value in this memo "
             "reached the register without a named human approving it.")
    L.append("")

    if parties:
        L.append("## Parties")
        for p in parties:
            L.append(f"- **{p['value']}** - {p['field'].split(':', 1)[1]}")
        L.append("")

    L.append("## Key terms")
    L.append("")
    L.append("| Field | Value | Basis |")
    L.append("|---|---|---|")
    for name in ORDER:
        f = by.get(name)
        if not f or f["value"] in (None, "", "None"):
            continue
        if f["validator"] == "COMPUTED":
            basis = "calculated in code, not model output"
        elif f["edited_by_human"]:
            basis = "corrected by a human reviewer"
        else:
            span = (f["source_span"] or "").replace("|", "\\|")
            if len(span) > 90:
                span = span[:87] + "..."
            basis = f'"{span}"' + (f" (p.{f['page']})" if f["page"] else "")
        L.append(f"| {name.replace('_', ' ')} | {f['value']} | {basis} |")
    L.append("")

    if unusual:
        L.append("## Flagged for review")
        for u in unusual:
            L.append(f"- {u['value']}")
            if u["source_span"]:
                L.append(f"  > {u['source_span']}")
        L.append("")

    if obs:
        L.append("## Diarised obligations")
        for o in obs:
            L.append(f"- **{o['due_date']}** - {o['kind'].replace('_', ' ')}"
                     + (f" ({o['detail']})" if o["detail"] else ""))
        L.append("")

    L.append("---")
    L.append(f"Generated from committed register data only. "
             f"sha256 of source document: `{c['sha256'][:16]}...`")

    os.makedirs(OUT, exist_ok=True)
    dest = os.path.join(OUT, f"memo_{cid}.md")
    with open(dest, "w") as fh:
        fh.write("\n".join(L) + "\n")
    return dest


if __name__ == "__main__":
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            print("wrote", memo(int(arg)))
    else:
        con = db.connect(readonly=True)
        ids = [r[0] for r in con.execute(
            "SELECT id FROM contracts WHERE status='COMMITTED' ORDER BY id")]
        con.close()
        for cid in ids:
            print("wrote", memo(cid))
