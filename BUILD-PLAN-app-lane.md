# APP LANE — BUILD PLAN & INTERFACE CONTRACT

**Status: DELIVERED.** All phases complete and verified. Evidence in §6 and
`/srv/ledger/manifest/implog.md`. Three seams remain open by design (§1) and
each has a working default, so nothing here blocks on another lane.

**Scope of this lane:** deterministic Python + UI. Part 4 tasks T6–T13 plus the
app half of T14. **Not** this lane: NemoClaw onboarding (T3), OpenShell
sandbox/policy (T4), vLLM serving (T5), OpenClaw cron/skills (T14a–c), network
cutoff (T14g).

**Machine:** `linuxbox` = `promaxgb10-887f` — aarch64, Ubuntu 24.04.4, GB10,
121 GB unified, 3.4 T free, **no WAN**.

---

## 1. THREE SEAMS, AND ONLY THREE

This lane is coupled to other people's work at exactly three points. Everything
else is internal and cannot be broken from outside.

| # | Seam | Contract | Owner |
|---|---|---|---|
| S1 | **Inference** | OpenAI-compatible `POST $LLM_URL/v1/chat/completions`, model name `$LLM_MODEL` | model lane |
| S2 | **Retrieval (RAG)** | `retriever.py` — one module, two functions (§4) | teammate |
| S3 | **Storage** | host dirs that become sandbox mounts (§2) | sandbox lane |

No shared processes, no shared imports, no shared globals. Each seam has a
working default so **this lane never blocks and is never blocked.**

## 2. PATH PORTABILITY

The master doc hard-codes `/work/app` inside the OpenShell sandbox, which does
not exist yet. Building against a path we cannot execute means writing unrunnable
code. **Every path is an env var with a host default**, so identical files run on
the host today and in the sandbox tomorrow — the mount plan makes the two views
the same.

| Env var | Host default (now) | Sandbox (later) |
|---|---|---|
| `LEDGER_DB` | `/srv/ledger/data/ledger.db` | `/work/data/ledger.db` |
| `LEDGER_AUDIT` | `/srv/ledger/data/audit.jsonl` | `/work/data/audit.jsonl` |
| `LEDGER_INTAKE` | `/srv/ledger/intake,/srv/ledger/data/uploads` | `/work/intake,/work/data/uploads` |
| `LEDGER_OUT` | `/srv/ledger/outputs` | `/work/outputs` |
| `LEDGER_STATIC` | `/srv/ledger/app/static` | `/work/app/static` |
| `LEDGER_TOKEN` | env only — never written to a file | same |
| `LLM_URL` / `LLM_MODEL` | `http://inference.local/...` / `Qwen/Qwen3.6-35B-A3B-FP8` | same |
| `LLM_MODE` | `live` \| `record` \| `replay` | `live` |
| `LEDGER_RAG` | `off` \| `on` | `on` |

Code lives at `/srv/ledger/app` → bind-mounts to `/work/app`. No file moves at
handoff. **Deviation from doc, logged:** env-driven paths instead of literal
`/work/...`. All other T6–T11 content verbatim.

## 3. INGEST — MULTI-FORMAT (expanded per direction)

The doc's `read_text()` handles PDF, DOCX, and "everything else as bytes."
That is too narrow: a firm's contract folder is a museum. Ingest becomes its own
module, `ingest.py`, with a **registry keyed by extension with content-sniffing
fallback** — the one place a new format gets added.

### Tier 1 — native parsers, verified importable on the box, offline

| Format | Handler | Status |
|---|---|---|
| `.pdf` | PyMuPDF, per-page text + offsets | ✅ 1.26.0 |
| `.docx` | python-docx — paragraphs **+ tables + headers/footers** | ✅ |
| `.txt` `.md` | charset-normalizer decode; markdown stripped to text | ✅ |
| `.rtf` | striprtf | ✅ |
| `.odt` `.ods` `.odp` | odfpy | ✅ |
| `.xlsx` `.xlsm` | openpyxl — cells flattened per sheet | ✅ |
| `.pptx` | python-pptx — shape text per slide | ✅ |
| `.html` `.htm` | BeautifulSoup + lxml | ✅ |
| `.csv` `.tsv` | stdlib csv, dialect-sniffed | ✅ |
| `.eml` `.msg`* | stdlib `email` — body + walks attachments back through the registry | ✅ |
| `.json` | recursive string harvest | ✅ |
| `.zip` | expands, each member re-enters the registry | ✅ |

*`.msg` (Outlook OLE) is a stretch item; `.eml` is the guaranteed path.

**Why table and header/footer extraction matters here specifically:** the doc's
own star contract hides its renewal clause in **§14.3 near the end**, and fee
schedules live in tables. A paragraph-only DOCX reader silently drops both, and
`source_span` verification would then fail on values that are genuinely in the
document. This is a correctness fix, not a feature.

### Tier 2 — legacy fallback via LibreOffice

**Verified on the box:** LibreOffice 24.2.7.2, `soffice --headless
--convert-to`, round-tripped txt→docx→txt successfully, fully offline. Covers
`.doc`, `.xls`, `.ppt`, `.wpd`, `.pages` — converted to a Tier-1 format, then
parsed normally. Subprocess, hard timeout, no network. Logged in the audit
record as a conversion step so the provenance chain stays honest.

### Tier 3 — refused, loudly

Scanned/image-only PDFs (no text layer) and image files. OCR is explicitly out
of scope (doc §1.7). These are **rejected with a clear reason**, never
silently ingested as empty text — an empty extraction that passes validation
because there was nothing to contradict is the worst possible failure mode.
`ingest.py` asserts a minimum extracted-character threshold and marks the
contract `REJECTED` with `note='no text layer — OCR out of scope'`.

**Upload endpoint** accepts the full Tier-1/2 set and reports the detected
format back to the UI; the UI shows the parser used per contract.

## 4. RAG SEAM — S2 (per direction)

Retrieval is designed in now, built by the teammate, and **cannot break this
lane** because it is optional at every point.

### The contract — `retriever.py`, two functions, nothing else

```python
def index(contract_id: int, doc: ParsedDoc) -> int:
    """Index one parsed document. Returns chunk count.
    Called by pipeline.py AFTER a successful parse, BEFORE extraction.
    Must be idempotent per contract_id — pipeline may retry."""

def retrieve(query: str, k: int = 8, contract_id: int | None = None
             ) -> list[Passage]:
    """Return top-k passages. contract_id=None searches the whole corpus.
    Must NOT raise; on failure return []."""
```

`ParsedDoc` and `Passage` are the only shared types, defined in `ingest.py`
(not in the retriever) so the teammate imports from us and we never import
from them:

```python
@dataclass
class Page:     number: int; text: str; char_start: int
@dataclass
class ParsedDoc:
    text: str            # full normalised text — what validators check against
    pages: list[Page]    # page boundaries with char offsets into .text
    fmt: str             # 'pdf' | 'docx' | ... — the parser that ran
    converted_via: str | None  # 'libreoffice' if Tier 2, else None
@dataclass
class Passage:
    text: str; contract_id: int; page: int
    char_start: int; char_end: int; score: float
```

### Three integration points, each with a working no-RAG default

1. **Index on ingest.** `pipeline.py` calls `index()` after parse. If
   `LEDGER_RAG=off` or the module is absent, it is skipped. Wrapped in
   try/except: **a retrieval failure must never block a proposal.**
2. **Long-document extraction.** Today `extract.py` truncates at 40,000 chars
   (doc §3.3) — which on a long contract can truncate away §14.3, the exact
   clause the demo depends on. With RAG on, we instead retrieve the top-k
   passages for each target field and send those. Default stays truncation, so
   behaviour is unchanged until retrieval exists. **This is the strongest
   technical argument for RAG here and worth saying to judges.**
3. **The Ask tab.** Deterministic SQL answers the register questions (D-C).
   With RAG on, Ask additionally cites document passages. Grounding rule is
   inherited unchanged: **a cited passage is subject to V1** — it must appear
   verbatim in the stored document text or it is not shown.

**Non-negotiable, and the teammate must be told:** retrieval changes *what text
the model sees*. It does **not** get to change what is verified. Every value
still carries a `source_span` checked against the full document text (V1) and
still passes value-in-span (V7). A retrieved passage is an input, never
evidence. If retrieval is wrong, the validator catches it exactly as it catches
a bad quote today.

Chunk store: separate SQLite file (`rag.db`) or table-prefixed in `ledger.db` —
teammate's call, but it **must not alter** the `contracts` / `extractions` /
`obligations` schemas. Those three tables are this lane's contract with the
audit chain.

## 5. `LLM_MODE` — lane independence and stage insurance

`extract.py` supports `live` (call the endpoint), `record` (call + persist raw
response to `fixtures/<sha256>.json`), and `replay` (read the fixture, no
network; fail loudly if absent — never invent one).

Buys three things: the pipeline/API/UI/acceptance suite get built and verified
**today with no model running**; demo timing becomes repeatable instead of
decode-speed dependent; and if vLLM dies on stage, `replay` keeps the demo alive
on genuine recorded output.

**Honesty rule (doc §1.6a).** Mode is written into every audit record and API
response, and the UI shows a visible `REPLAY` banner. We never present replayed
output as live. To a judge: *"that is a recording of this model's real output on
this document, and the banner says so."*

## 6. PHASES

Verification is a separate gate per phase, never folded into the build step.

### PHASE 0 — Foundation ✅ DONE (verified)
- `/srv/ledger/{intake,data,outputs,manifest}` created, owned by `dell`; implog seeded
- `dell` added to `docker` group
- **35 wheels sourced from the Windows workstation** (aarch64/cp312) and pushed
  to `/srv/ledger/data/wheels` — the box has no WAN and **this lane no longer
  needs it**
- venv at `/srv/ledger/data/venv` (PEP-668 makes this mandatory, not stylistic)
- **Check passed:** `fastapi 0.140.0`, `PyMuPDF 1.26.0`, python-docx, dateutil,
  ics, uvicorn, striprtf, openpyxl, python-pptx, bs4, markdown, odfpy all
  import → `MULTIFORMAT DEPS OK`
- **Check passed:** LibreOffice headless round-trip conversion works offline

### PHASE 1 — Ingest + Core credibility layer ✅ DONE (verified)
`ingest.py` (registry + `ParsedDoc`/`Page`/`Passage`), `db.py`, `audit.py`,
`validate.py`, `test_validate.py`, `test_ingest.py`.
- **Check passed:** `ALL VALIDATOR TESTS PASSED` — original T8 assertions plus
  3 new A1 cases and 4 new B3 cases.
- **Check passed:** `21 passed, 0 failed` — 12 native formats, LibreOffice
  legacy path, and 3 loud refusals.
- **Check passed:** audit tamper test → tampered copy `record altered at seq 2`,
  original `chain intact`.

### PHASE 2 — Pipeline ✅ DONE (verified)
`extract.py` (+`LLM_MODE`), `pipeline.py` (sha256 dedupe, `--no-validate`),
optional `index()`, five seed contracts across `.docx`/`.pdf`/`.txt`/`.odt`/`.doc`.
- **Check passed:** 4 contracts proposed; Meridian
  `notice_deadline = 2027-01-30` marked `COMPUTED` with no span.
- **Check passed:** re-run → `nothing new in intake` (dedupe holds).

### PHASE 3 — Interface ✅ DONE (verified)
`app.py`; all four obligation kinds; `.ics` on `notice_deadline` only;
deterministic `/api/ask`; multi-format `/api/upload`; `/api/outputs`;
`/api/contract/{id}/text` for receipts.
- **Check passed:** 401 unauthenticated · 409 on FAIL · 409 on double-commit ·
  clean approve → `COMMITTED` + memo + `.ics` + 3 obligations.
- **Three bugs found and fixed by testing** (see implog T11), including one
  behavioural: a human correction did not recompute `notice_deadline`, which
  would have silently broken the 2:30 demo beat.

### PHASE 4 — UI ✅ DONE (verified)
`static/ui.html`, vanilla JS, zero CDN/fonts/frameworks.
- **Check passed:** driven in headless Chromium against the live API — red
  field renders, `Approve ✕ blocked` disabled, quote receipt highlights the
  span in context with a page number, edit → *"deadline recomputed to
  2027-01-30"* → approve → `COMMITTED`, all four tabs populate.

### PHASE 5 — Acceptance ✅ DONE (verified)
- **Check passed:** `46/46 checks passed` — AT-2, AT-3, AT-3b, AT-3c, AT-4,
  **AT-5** (dedupe), AT-7, AT-9, **AT-10** (format matrix).
- AT-1 / AT-6 / AT-8 and the power-cycle half of AT-5 need the model and
  sandbox lanes; run jointly.

### PHASE 6 — Handoff & cleanup ✅ DONE (verified)
- Deck: preconnects removed **and** the vendored `css2` dropped — it still
  pointed every `@font-face` at `fonts.gstatic.com`, so the deck was silently
  falling back on an air-gapped box. Now a local system font stack.
  **Check passed:** 8 slides, zero non-local requests, zero failures.
- `firewall.sh` written, dry-run verified, **not applied** — apply when the
  demo machine is staged, with a session held open.
- `retriever.py` stub shipped with the full S2 contract; verified harmless
  (`LEDGER_RAG=on` with the stub behaves identically to `off`).
- `demo_reset.sh` — one command restores the exact runbook 0:00 state.
- `memo.py` — deterministic memo hooked into commit.
- `implog.md` written to `/srv/ledger/manifest/`.

## 7. DECISIONS — ALL THREE SIGNED OFF AND IMPLEMENTED

`A1 + B3 + C3`, approved 2026-07-26. Each is now covered by a test that fails
if it regresses.

**D-A — the deck and the code disagree.** Slide 04 shows *Renewal term ·
12 months · **failed** · "value not found in its own quote."* But T8's
`validate.py` only runs `span_ok` on `renewal_term_months` — it **cannot**
produce that verdict. Worse, the doc's own passing fixture pairs
`renewal_term_months: 12` with the span `"at least sixty (60) days prior to
expiry"`, which does not contain 12; adding `int_in_span` there breaks
`assert not has_failures(rows)`.
*DONE:* `int_in_span` enforced on the field; the `auto_renewal` span now quotes
the full renewal clause (contains both "twelve-month" and "sixty (60)"); one
fixture widened. The deck is now honest, and the red field is **reproducible**
rather than hoped-for — `demo_reset.sh` produces it deliberately every time.
Guarded by 3 assertions in `test_validate.py`.

**D-B — span offsets + page.** *DONE:* `span_start`, `span_end`, `page` added to
`extractions`, computed in `ingest.py` from the text layer. The UI turns a quote
into a click that highlights the span in its surrounding document text. Shares a
coordinate system with `Passage`, so the RAG lane's citations line up for free.
Guarded by 4 assertions in `test_validate.py` and 2 in `test_acceptance.py`.

**D-C — deterministic Ask.** *DONE:* `/api/ask` matches intent in Python and
runs SELECT-only SQL; the model is never in the read path. Verified against 3
hostile inputs. A prose/RAG layer can format its rows later without changing it.

## 8. RESUMABILITY

All state on disk: `ledger.db`, `audit.jsonl`, `fixtures/`. Nothing in a context
window. After interruption: re-read this file, query the DB for what exists,
skip completed phases. `pipeline.py`'s sha256 dedupe makes re-running
idempotent by construction.
