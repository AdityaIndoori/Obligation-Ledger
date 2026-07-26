# Obligation Ledger — app lane

Contract intake → verified obligation register, running entirely on one
machine. An open-weight model proposes; deterministic code verifies; a named
human approves. Nothing reaches the register without all three.

This repository is the **app lane**: ingest, validation, pipeline, approval API,
UI, and the chat interface. The model runtime (vLLM), the retrieval service and
the sandbox policy are separate lanes — see [Seams](#seams).

---

## The claim, stated precisely

Published benchmarks put the best models at roughly **0.64 F1** on clause-level
contract risk identification ([ContractEval](https://arxiv.org/pdf/2508.03080)).
A two-thirds-accurate extractor is useless as a system of record and very useful
as a proposal engine — *if something checks it*. That checking layer is the
product; the model is swappable.

Three rules, enforced in code rather than in prompts:

| Rule | Enforced by | What it does **not** cover |
|---|---|---|
| Every value quotes the contract, and the value must be findable **inside** that quote | `validate.py` V1 + V7 | A correct value quoted from the *wrong clause* — that is the human's job |
| The model never does arithmetic | `validate.py` V3, `app.py::_recompute_deadline` | Wrong *inputs*: correct arithmetic on a misread date is still wrong |
| Nothing changes state without a human | `POST /api/decide`; `pipeline.py` can only write `PROPOSED` | Anyone with a shell inside the sandbox can `UPDATE` the DB directly |

Language discipline is deliberate throughout: **cannot** (structurally
impossible) / **detected** (a check exists, with known limits) / **mitigated**
(no automated check; a human is the control). The UI says `QUOTE MATCHED`, never
`VERIFIED`, because the check is a string match against the cited text — not a
judgement that the right clause was cited.

## Measured, not claimed

On the sample corpus against live inference (`Qwen3.6-35B-A3B-FP8`, ~9 s/doc):

```
quote-match rate    70/71 = 98.6%     ← NOT an accuracy figure
documents refused   1 (no text layer)
```

That figure counts how often a reported value was findable in the text the model
itself cited. It says nothing about whether the right clause was cited. Run
`measure.py` to reproduce it; the script prints that caveat in its own output.

---

## Layout

```
Code/
  ingest.py          32 formats, 3 tiers, refuses documents with no text layer
  validate.py        V1–V7 — the credibility layer
  db.py              SQLite register + provenance columns
  audit.py           append-only hash-chained log
  extract.py         model call, guided JSON, live | record | replay
  pipeline.py        parse → index → extract → validate → PROPOSE
  app.py             approval API, upload, outputs, Ask
  chat.py            retrieval-first Q&A; the model plans, code executes
  retriever.py       the RAG seam (S2)
  models.py          model catalog + served-id discovery
  memo.py            deterministic memo from committed data only
  static/ui.html     the whole UI: vanilla JS, zero CDN, zero webfonts
  *.sh               operational scripts (see below)
  test_*             six suites
Sample-Contracts/    11 PDFs spanning the interesting failure modes
docs/                deployment, decisions, implementation log
```

## Running it

```bash
source Code/ledger.env
python Code/db.py                                    # create the schema
uvicorn app:app --host 0.0.0.0 --port 8443           # from Code/
```

Full setup, including the offline wheel path for an air-gapped box, is in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### Scripts

| Script | Purpose |
|---|---|
| `clean_slate.sh` | Wipe to a genuinely empty system (`--dry-run`, `--yes`) |
| `go_live.sh` | Extract the sample corpus against the real model, then report measured quality |
| `demo_reset.sh` | Reproducible demo state, pinned to `replay` |
| `rag_backfill.py` | Index contracts that predate the retrieval service |
| `unarchive.sh` | Bulk undo for soft-deleted contracts (`--list`) |
| `firewall.sh` | Host firewall, corrected for this machine's subnet |

### Tests

```bash
python test_validate.py       # validators — fix the code, never the test
python test_ingest.py         # 21 format cases incl. 3 loud refusals
python test_acceptance.py     # 53 checks: AT-2, AT-3, AT-3b/c, AT-4/5/7/9/10
./test_models.sh              # model selection never rewrites history
./test_chat.sh                # SELECT-only invariant under hostile input
./test_delete.sh              # soft delete keeps the audit chain intact
./test_rag.sh                 # retrieval is primary; our validators still gate
```

---

## Seams

Three coupling points, each with a working default, so no lane blocks another.

| Seam | Contract | Default when absent |
|---|---|---|
| **Inference** | OpenAI-compatible `POST $LLM_URL`; served id discovered from `/v1/models` | `LLM_MODE=replay` runs on recorded fixtures |
| **Retrieval** | `retriever.index/retrieve/extract` | Falls back to the structured register, and says so |
| **Storage** | env-var paths that become sandbox mounts | Host defaults under `/srv/ledger` |

The retrieval seam records the RAG lane's own per-field status **and** keeps our
verdict as the one that gates Approve. Where they disagree the field fails and
the note states both. That is not distrust: the product's claim is that every
committed value carries a verbatim quote, and that has to be checked by the
thing making the claim.

## What this is not

- Not a legal opinion engine. It extracts facts, quotes sources, computes dates.
- Not accurate by model quality. See the table above for what each rule covers.
- OCR is out of scope for now — **not** a model limitation (the primary model
  has a vision encoder) but because OCR inserts a second error source
  *upstream of the validators*, which would silently weaken V1 from "quoted
  from the document" to "quoted from a transcription of the document".
- We have not benchmarked ourselves against CUAD. Until we do, published
  numbers are cited as published numbers.
