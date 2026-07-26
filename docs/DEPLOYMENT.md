# Deployment

Target: Dell Pro Max with GB10 — `aarch64`, Ubuntu 24.04, 121 GB unified memory,
20 cores. Everything below is what was actually done on that machine, including
the parts that only matter because the box is unusual.

---

## 1. Directories

```bash
sudo mkdir -p /srv/ledger/{intake,data,outputs,manifest,samples}
sudo chown -R "$USER":"$USER" /srv/ledger
```

## 2. Python dependencies — the offline path

The box has no reliable WAN, and PEP 668 marks the system Python
externally-managed, so a venv is mandatory rather than stylistic.

Wheels are **sourced on a networked workstation** and copied across. This is why
`wheels/` is not in the repository: it is 71 MB of platform binaries that this
one command regenerates.

```bash
# on a networked machine
pip download fastapi uvicorn pymupdf python-docx python-dateutil ics \
             python-multipart striprtf openpyxl python-pptx beautifulsoup4 \
             lxml markdown odfpy defusedxml charset-normalizer \
  --platform manylinux_2_28_aarch64 --python-version 312 \
  --only-binary=:all: -d wheels

scp wheels/*.whl box:/srv/ledger/data/wheels/
```

```bash
# on the box
python3 -m venv /srv/ledger/data/venv --system-site-packages
/srv/ledger/data/venv/bin/pip install --no-index \
  --find-links=/srv/ledger/data/wheels \
  fastapi uvicorn pymupdf python-docx python-dateutil ics python-multipart \
  striprtf openpyxl python-pptx beautifulsoup4 markdown odfpy
```

Verify:

```bash
/srv/ledger/data/venv/bin/python -c \
  "import fastapi,fitz,docx,dateutil,ics,uvicorn,striprtf.striprtf,openpyxl,pptx,bs4,markdown,odf.opendocument; print('ok')"
```

**LibreOffice** is used for Tier-2 legacy formats (`.doc`, `.xls`, `.ppt`). It
was already present at 24.2.7.2; `soffice --headless --convert-to` works offline.

## 3. The model runtime

A vLLM container serves the weights. Note the served name — it is **not** the
HuggingFace repo id, and sending the repo id returns a 404:

```
vllm/vllm-openai:v0.20.0
serve --model /model --served-model-name=qwen3-35b
      --max-num-seqs 8 --max-model-len 32768 --trust-remote-code
mount: Models/Qwen3.6-35B-A3B-FP8 -> /model (ro)
```

`models.py` discovers the served id from the container's bind mount plus
`--served-model-name`, because vLLM reports `root` as a generic `/model` that
cannot identify the weights. So an id mismatch is self-correcting rather than a
runtime 404.

Two settings were measured, not guessed:

| Setting | Why |
|---|---|
| `chat_template_kwargs={"enable_thinking": false}` | Qwen3.6 is a thinking model. With thinking on: **17.6 s for 16 tokens**, prefixed with a reasoning monologue. Off: **1.9 s**, clean JSON. |
| `response_format=json_schema` | Guided decoding makes a parse failure structurally impossible. Falls back once to the strict-JSON prompt if an endpoint rejects it. |

Also relevant: `--max-model-len 32768` **tokens** ≈ 130k chars, so the 40,000
character truncation in `extract.py` binds first. That is deliberate but it has
a cost — see the retrieval note below.

## 4. Environment

```bash
source /srv/ledger/app/ledger.env
```

Every value is an overridable default. Two are load-bearing:

- `LLM_URL` — the inference seam. `inference.local` resolves via `/etc/hosts`;
  `localhost:8000` also works.
- `LEDGER_TOKEN` — the shared bearer token. `demo-token` is a POC placeholder
  (D9); override it in the environment for anything real.

`ledger.env` also exports the RAG lane's variables. That is not tidiness: its
`Settings` reads `.env` **relative to the working directory**, so an in-process
import from `/srv/ledger/app` never sees `rag-src/.env` and silently falls back
to defaults — including the wrong model id.

## 5. Retrieval service

```bash
cd /srv/ledger && git clone https://github.com/Seveyus/Obligation-Ledger-AI.git rag-src
/srv/ledger/data/venv/bin/pip install -e rag-src
cp Code/rag.env rag-src/.env          # sets LLM_MODEL=qwen3-35b, not their default
uvicorn obligation_rag.api:app --host 0.0.0.0 --port 8001
```

Running BM25-only: no sentence-transformers weights are staged, and nothing may
be downloaded at runtime. That is their documented supported mode and is
sufficient for extraction; dense retrieval only sharpens natural-language Ask.

**Contracts ingested before the service existed are not indexed**, so retrieval
returns nothing for them and every question falls back to SQL. Catch them up:

```bash
python rag_backfill.py
```

Retrieval is what makes the long-document case work. `07-ridgeline-supply-long.pdf`
is 57,614 chars with its renewal clause at offset **57,391** — beyond the 40,000
truncation. The native path extracts four fields and no renewal data; retrieval
answers it.

## 6. The app

```bash
cd /srv/ledger/app
source ./ledger.env
uvicorn app:app --host 0.0.0.0 --port 8443
```

## 7. Firewall

`firewall.sh`, **not** the command in the master document. That says
`ufw allow from 192.168.0.0/16`; this box is on `10.10.0.0/24` with a
`172.16.0.0/16` wireless interface. Running it verbatim then enabling ufw denies
both SSH and the UI.

Apply it with a session held open, and check `ssh` plus `curl :8443` from
another machine before closing that session.

---

## Operational notes

**Uploads are asynchronous.** Extraction takes ~16 s per contract; holding the
HTTP request open for that long made browsers abort it, which surfaced as
`Failed to fetch` on the queue polls firing alongside. `POST /api/upload` now
returns `202` with a job id in ~8 ms and the UI polls `GET /api/upload/{job}`.

**There is no watcher.** The 60-second cron belongs to the OpenClaw lane and does
not exist. Upload ingests inline instead of promising a watcher that isn't
there. When that lane lands, sha256 dedupe makes the double scan a no-op.

**Delete is a soft delete.** The audit chain is append-only and hash-linked, so
removing a row would break the tamper-evidence the product is built on.
Deleting hides the contract, closes its obligations, records who and when, and
writes the deletion into the chain. `unarchive.sh --list` shows what is hidden.

**MuPDF font warnings are suppressed.** A real 40-page lease produced 400+
`unknown cid font type` lines on stderr, which buried the actual result. They
are advisory — text extracts fine — and are now collected into `doc.notes`.

## Not done by this lane

`AT-1` (local inference), `AT-6` (offline operation), `AT-8` (sandbox
containment) and the power-cycle half of `AT-5` need the model and sandbox
lanes. `AT-8` in particular has nothing to test yet: NemoClaw and OpenShell are
not installed, so the "enforced by kernel-level sandbox policy" claim is not
currently demonstrable. The inference half works; the enforcement half does not
exist.
