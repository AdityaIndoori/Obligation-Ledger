# Retrieval integration (seam S2)

The retrieval service lives in its own repository and is included here as a
**pinned submodule** at `rag-service/`, authored by
[@Seveyus](https://github.com/Seveyus)
([Obligation-Ledger-AI](https://github.com/Seveyus/Obligation-Ledger-AI), MIT).

A submodule rather than a copy, for three reasons: it preserves their
authorship, it pins an exact commit so this repository describes a
reproducible system, and it cannot silently drift from upstream the way a
vendored copy does.

```bash
git clone --recurse-submodules https://github.com/AdityaIndoori/Obligation-Ledger
# or, after a plain clone:
git submodule update --init --recursive
```

---

## What each half owns

| | Retrieval service (`rag-service/`) | App lane (`Code/`) |
|---|---|---|
| Reads | the document text | the extracted register |
| Answers | "what does this contract say about X" | counts, dates, statuses, workflow |
| Returns | ranked passages, per-field status, `can_approve` | the verdict that gates Approve |
| Storage | its own SQLite + indexes under `RAG_DATA_DIR` | `ledger.db` + `audit.jsonl` |

Both are consumed through one module, `Code/retriever.py`, which exposes the
three functions the pipeline calls:

```python
index(contract_id, doc)   -> int          # after parse, before extract
retrieve(query, k, cid)   -> [Passage]    # the Ask tab
extract(contract_id, doc) -> ExtractionResult
```

`extract()` is the function that makes the trust guarantees reachable. `Passage`
carries `text, page, char_start, char_end, score` and has nowhere to put a
verification status, so with `retrieve()` alone the Register view cannot render a
per-field badge, cannot show the supporting quote, and cannot disable Approve
when a field fails. That was a correct call on their part and is why the seam has
three functions rather than two.

## The boundary, and why it is where it is

**Their status is recorded. Our verdict gates Approve.**

The RAG lane reports a per-field `status` (`verified` / `computed` / `failed`)
and a `can_approve` flag. Both are stored. But a value only becomes `PASS` in
this register when **our** V1/V7 checks find it inside its own quote in **our**
`ParsedDoc.text`.

This is not distrust of their code. The product's central claim is that every
committed value carries a verbatim source quote — and that has to be checked by
the thing making the claim, or it degrades to *"a component told us it checked."*
Where the two disagree, the field **fails** and the note states both readings,
because a disagreement is information rather than an error to smooth over.

That boundary did real work on its first live run. Their normalisation rewrites
`EUR 240,000` → `EUR 240000.00` and `automatic_renewal` → `"true"`, so those
values no longer appear verbatim in the quotes they cite. Two fields their
pipeline marked `verified` failed ours and blocked Approve:

```
RAG can_approve=False   our failures=3   disagreements=2
```

Worth flagging upstream: this is a normalisation-versus-evidence question, not a
bug. A normalised value is more useful to a machine and less checkable by a
human, and only one of those is what a receipt is for.

## Coordinate system

Their §11 guarantees `doc.text[ev.char_start:ev.char_end] == ev.quote` against
**our** `ParsedDoc.text`. We **verify that rather than assume it**, and relocate
the quote ourselves if it does not hold — those offsets are what shows a
reviewer the receipt, so they cannot be taken on trust:

```python
# Code/retriever.py::_evidence_offsets
if doc.text[s:e] == quote:      # their offsets are good, keep them
    ...
else:
    hit = doc.locate(quote)     # relocate against our own text
```

It holds in practice: verified across passages in `test_rag.sh`.

## Field name mapping

Their canonical names are more precise than ours; ours are the register's schema
and are what the UI, the memo and the audit chain already use, so the mapping
happens at the seam rather than by renaming everything downstream. See
`FIELD_MAP` in `Code/retriever.py`.

Two families are kept **deliberately distinct**, because they are opposite
risks:

| Family | Miss the deadline and… |
|---|---|
| `termination_notice_period` → `notice_days` | the contract renews and you pay for another term |
| `renewal_option_notice` → `option_notice_days` | the option lapses and you **lose** the contract |

## Running it

```bash
pip install -e rag-service
cp Code/rag.env rag-service/.env
uvicorn obligation_rag.api:app --host 0.0.0.0 --port 8001
python Code/rag_backfill.py        # index contracts that predate the service
```

Three things that are easy to get wrong, all learned by getting them wrong:

1. **`LLM_MODEL` must be the id vLLM actually serves.** Their example defaults to
   `gpt-oss-120b`; this box serves `qwen3-35b`. Sending the wrong id is a 404.
   `Code/retriever.py::_align_model_env` resolves it automatically, scoped to
   the import and restored immediately — an earlier version left the wire id in
   the environment, which `models.selected()` also reads, and the UI started
   rendering the model's quantisation as `undefined`.

2. **Their `Settings` reads `.env` relative to the working directory.** An
   in-process import from `/srv/ledger/app` never sees `rag-service/.env` and
   silently falls back to defaults. `Code/ledger.env` therefore exports their
   variables explicitly.

3. **Contracts ingested before the service existed are not indexed.** Retrieval
   returns nothing for them, so every question falls back to SQL — reported
   honestly as `FELL BACK`, but with no obvious cause. `rag_backfill.py` indexes
   from **stored** text rather than re-reading files, so the indexed text is
   byte-identical to what the validators check and the reviewer reads.

## Retrieval mode

Running **BM25-only**. No sentence-transformers weights are staged and nothing
may be downloaded at runtime — that is the product's central claim, not a
preference. This is their documented supported mode and is sufficient for
extraction; the dense side only sharpens natural-language Ask.

## Why retrieval matters here, measured

`Sample-Contracts/07-ridgeline-supply-long.pdf` is **57,614 characters** with its
renewal clause at offset **57,391** — beyond the 40,000-character truncation in
`extract.py`. The native path extracts four fields and no renewal data at all:
not a hallucination, an honest blind spot.

Retrieval answers it, citing the correct section. That is the concrete argument
for this seam rather than a hypothetical one.
