# Obligation Ledger

Contract intake → verified obligation register, running entirely on one machine.
An open-weight model proposes, deterministic code verifies, a named human
approves. Nothing reaches the register without all three.

This repository is the **app lane**: ingest, validation, pipeline, approval API,
UI and chat. The model runtime (vLLM) and the sandbox policy are separate lanes;
the retrieval service is included as a submodule. See [Seams](#seams).

```
┌─ Documents ─┐   ┌─ Model proposes ─┐   ┌─ Code verifies ─┐   ┌─ Human ─┐
│ 32 formats  │ → │ + retrieval      │ → │ V1–V7 + Python  │ → │ approves│ → Register
│ or REFUSED  │   │ every value      │   │ arithmetic      │   │ or edits│    + audit
│ with reason │   │ quotes its source│   │ blocks on FAIL  │   │         │      chain
└─────────────┘   └──────────────────┘   └─────────────────┘   └─────────┘
```

**Contents** · [Claim](#the-claim-stated-precisely) · [Quick start](#quick-start)
· [Operations](#operations) · [Maintenance](#maintenance)
· [Troubleshooting](#troubleshooting) · [Tests](#tests) · [Layout](#layout)
· [Seams](#seams) · [Limits](#what-this-is-not)

---

## The claim, stated precisely

Published benchmarks put the best models at roughly **0.64 F1** on clause-level
contract risk identification
([ContractEval](https://arxiv.org/pdf/2508.03080)). A two-thirds-accurate
extractor is useless as a system of record and very useful as a proposal engine
— *if something checks it*. That checking layer is the product; the model is
swappable.

Three rules, enforced in code rather than in prompts:

| Rule | Enforced by | What it does **not** cover |
|---|---|---|
| Every value quotes the contract, and the value must be findable **inside** that quote | `validate.py` V1 + V7 | A correct value quoted from the *wrong clause* — that is the human's job |
| The model never does arithmetic | `validate.py` V3, `app.py::_recompute_deadline` | Wrong *inputs*: correct arithmetic on a misread date is still wrong |
| Nothing changes state without a human | `POST /api/decide`; `pipeline.py` can only write `PROPOSED` | Anyone with a shell inside the sandbox can `UPDATE` the DB directly |

Language discipline is deliberate: **cannot** (structurally impossible) /
**detected** (a check exists, with known limits) / **mitigated** (no automated
check; a human is the control). The UI says `QUOTE MATCHED`, never `VERIFIED`,
because the check is a string match against the cited text — not a judgement
that the right clause was cited.

### Measured, not claimed

On the sample corpus against live inference (`Qwen3.6-35B-A3B-FP8`, ~9 s/doc):

```
quote-match rate    70/71 = 98.6%     ← NOT an accuracy figure
documents refused   1 (no text layer)
```

That counts how often a reported value was findable in the text the model itself
cited. It says nothing about whether the right clause was cited. `measure.py`
reproduces it and prints that caveat in its own output.

---

## Quick start

```bash
git clone --recurse-submodules https://github.com/AdityaIndoori/Obligation-Ledger
cd Obligation-Ledger
```

`rag-service/` is a pinned submodule; a plain clone leaves it empty. Recover with
`git submodule update --init --recursive`.

**Prerequisites:** Python 3.12, and an OpenAI-compatible model endpoint. Without
one, set `LLM_MODE=replay` and the whole app runs on recorded fixtures.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install fastapi uvicorn pymupdf python-docx python-dateutil ics \
            python-multipart striprtf openpyxl python-pptx beautifulsoup4 \
            markdown odfpy

cd Code
source ./ledger.env          # every value is an overridable default
python db.py                 # create the schema
uvicorn app:app --host 0.0.0.0 --port 8443
```

Open `http://<host>:8443`. Full setup for the air-gapped target box, including
the offline wheel path, is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## Operations

All commands run from `Code/` with `source ./ledger.env` applied.

### Start

```bash
# 1. the model endpoint (separate lane; skip for LLM_MODE=replay)
docker start vllm-ledger

# 2. the retrieval service
cd ../rag-service && uvicorn obligation_rag.api:app --host 0.0.0.0 --port 8001 &

# 3. the app
cd ../Code && uvicorn app:app --host 0.0.0.0 --port 8443
```

Start order matters only for cleanliness: the app degrades gracefully if either
dependency is missing, and says which engine answered.

For an unattended host, run them under systemd rather than `&` so they survive
a logout:

```bash
uvicorn app:app --host 0.0.0.0 --port 8443 >> /srv/ledger/app.log 2>&1 &
echo $! > /srv/ledger/app.pid
```

### Stop

```bash
kill "$(cat /srv/ledger/app.pid)"     # if started with a pidfile
pkill -f 'uvicorn app:app'            # the app only
pkill -f 'obligation_rag.api'         # retrieval only
docker stop vllm-ledger               # the model
```

`pkill -f uvicorn` would kill **both** services — name the one you mean.

### Restart after a code change

```bash
pkill -f 'uvicorn app:app'; sleep 2
uvicorn app:app --host 0.0.0.0 --port 8443
```

Uvicorn without `--reload` holds the old module in memory, so an edit does
nothing until you restart. This is the single most common source of "I fixed it
and nothing changed."

### Health

```bash
curl -s localhost:8443/api/meta   | python -m json.tool   # app + audit + model
curl -s localhost:8443/api/models | python -m json.tool   # endpoint reachable?
curl -s localhost:8001/openapi.json > /dev/null && echo "retrieval up"
curl -s localhost:8000/v1/models  | python -m json.tool   # what vLLM serves
python audit.py                                           # verify the chain
```

`/api/meta` is the one to check first: it reports the audit chain state, the
selected model and whether inference is live or replaying.

### Logs

```bash
tail -f /srv/ledger/app.log
ss -ltn | grep -E '8443|8001|8000'        # who is listening
```

---

## Maintenance

### Load documents

```bash
cp mycontract.pdf /srv/ledger/intake/
python pipeline.py                     # extract everything new
```

Uploading through the UI does the same thing and returns immediately —
extraction runs in the background and the queue updates when it lands. There is
no watcher process; `pipeline.py` is idempotent (sha256 dedupe), so running it
twice is safe.

### Reset

```bash
./clean_slate.sh --dry-run     # show exactly what would go
./clean_slate.sh               # wipe, with confirmation
./clean_slate.sh --yes         # wipe, unattended
```

Clears the register, the audit chain, fixtures, intake, uploads, outputs and the
retrieval index — six places, which is why this is a script and not a few `rm`s.
Keeps code, venv, model weights and `Sample-Contracts/`.

**Restart both services afterwards**: each caches a store handle and will
otherwise serve a view of data that no longer exists.

### Demo and evaluation states

```bash
./demo_reset.sh          # reproducible demo state (pinned to replay)
./go_live.sh --commit    # extract the sample corpus for real, then measure
python measure.py        # quote-match rate over whatever is loaded
```

### Retrieval index

```bash
python rag_backfill.py   # index contracts that predate the retrieval service
```

Needed once after installing retrieval, and after any restore from a backup.
Without it retrieval returns nothing for older contracts and every question
falls back to SQL — reported honestly as `FELL BACK`, but with no obvious cause.

### Deleted contracts

Delete is a **soft** delete: the audit chain is append-only and hash-linked, so
removing a row would break the tamper-evidence the product is built on.

```bash
./unarchive.sh --list    # what is currently hidden
./unarchive.sh           # restore all of it
```

### Model selection

Change it in the UI header, or:

```bash
curl -X POST localhost:8443/api/models/select \
  -H "Authorization: Bearer $LEDGER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"model":"openai/gpt-oss-120b"}'
```

Takes effect on the **next** extraction. Contracts already in the register keep
the model that actually produced them — selection never rewrites history.

### Backup

Three files and one directory are the entire durable state:

```bash
tar czf ledger-backup-$(date +%F).tgz \
  /srv/ledger/data/ledger.db /srv/ledger/data/audit.jsonl \
  /srv/ledger/data/fixtures /srv/ledger/outputs
```

Back up `ledger.db` and `audit.jsonl` **together**. The chain is what makes the
register defensible; a register without its chain is just a spreadsheet.

### Firewall

```bash
sudo ./firewall.sh --dry-run    # print the rules
sudo ./firewall.sh              # apply
```

Run it **with an SSH session held open** and verify access from another machine
before closing that session. The subnets are set for the target box; check them
against `ip -4 addr` first.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Failed to fetch` in the browser | app not running, or wrong host | `curl localhost:8443/api/meta`; check `ss -ltn \| grep 8443` |
| Code change had no effect | uvicorn holds the old module | restart the app (see above) |
| Every chat answer says `FELL BACK` | contracts not indexed for retrieval | `python rag_backfill.py` |
| Chat says `RETRIEVAL OFFLINE` | the service on :8001 is down | start it; the app still answers from the register |
| Extraction fails with a 404 | model id ≠ what vLLM serves | `curl localhost:8000/v1/models` and compare |
| `REJECTED: no text layer` | scanned PDF, no extractable text | expected — OCR is out of scope, see [Limits](#what-this-is-not) |
| Approve is greyed out | a field failed verification | fix the red field; the error summary links to it |
| Register looks empty after clicking around | contracts soft-deleted | `./unarchive.sh --list`, then `./unarchive.sh` |
| Hundreds of `unknown cid font type` lines | non-standard embedded fonts | harmless; suppressed into `doc.notes` |

---

## Tests

```bash
python test_validate.py     # validators — fix the code, never the test
python test_ingest.py       # 21 format cases incl. 3 loud refusals
python test_acceptance.py   # 53 checks: AT-2, AT-3, AT-3b/c, AT-4/5/7/9/10
./test_models.sh            # model selection never rewrites history
./test_chat.sh              # SELECT-only invariant under hostile input
./test_delete.sh            # soft delete keeps the audit chain intact
./test_rag.sh               # retrieval is primary; our validators still gate
```

`test_acceptance.py` needs the app running. The rest are self-contained.

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
  retriever.py       the retrieval seam
  models.py          model catalog + served-id discovery
  memo.py            deterministic memo from committed data only
  static/ui.html     the whole UI: vanilla JS, zero CDN, zero webfonts
  *.sh               operational scripts
  test_*             seven suites
Sample-Contracts/    11 PDFs spanning the interesting failure modes
rag-service/         submodule: the retrieval service (@Seveyus, MIT)
docs/                deployment, RAG integration, implementation log
```

---

## Seams

Three coupling points, each with a working default, so no lane blocks another.

| Seam | Contract | Default when absent |
|---|---|---|
| **Inference** | OpenAI-compatible `POST $LLM_URL`; served id discovered from `/v1/models` | `LLM_MODE=replay` runs on recorded fixtures |
| **Retrieval** | `retriever.index/retrieve/extract` | Falls back to the register, and says so |
| **Storage** | env-var paths that become sandbox mounts | Host defaults under `/srv/ledger` |

The retrieval seam records the RAG lane's per-field status **and** keeps our
verdict as the one that gates Approve. Where they disagree the field fails and
the note states both — not distrust, but because the claim "every committed
value carries a verbatim quote" has to be checked by the thing making it.
Details in [docs/RAG-INTEGRATION.md](docs/RAG-INTEGRATION.md).

---

## What this is not

- **Not a legal opinion engine.** It extracts facts, quotes sources and computes
  dates. It does not interpret contracts.
- **Not accurate by model quality.** The guarantees come from the checking layer;
  see the table above for exactly what each rule does and does not cover.
- **OCR is out of scope** — *not* a model limitation (the primary model has a
  vision encoder) but because OCR inserts a second error source **upstream of
  the validators**, silently weakening V1 from "quoted from the document" to
  "quoted from a transcription of the document". Scanned pages are refused with
  a reason rather than ingested as empty text.
- **Not benchmarked against CUAD.** Until it is, published numbers are cited as
  published numbers and our own figure is labelled a quote-match rate.

## Licence

MIT — see [LICENSE](LICENSE). `rag-service/` retains its own copyright.
Sample contracts are synthetic.
