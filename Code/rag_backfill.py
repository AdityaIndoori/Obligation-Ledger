"""Index every contract already in the register into the RAG service.

Needed because contracts ingested BEFORE the retrieval service existed were
never indexed, so retrieval returned nothing for them and every question fell
back to SQL. New contracts are indexed by pipeline.py at ingest time; this is
the one-off catch-up (and is safe to re-run -- index() is idempotent per
contract id).

Indexes from the STORED document text, not by re-reading the file, so the
indexed text is byte-identical to the text the validators check quotes against
and the reviewer reads on screen. Re-parsing would risk a second normalisation
and break the shared coordinate system.
"""
import sys

import db
import ingest


def main():
    try:
        import retriever
    except ImportError:
        print("retriever module missing")
        return 1
    if not retriever.available():
        print("RAG service is not installed or not importable; nothing to do")
        return 1

    con = db.connect(readonly=True)
    rows = list(con.execute(
        "SELECT id, filename, doctext, fmt FROM contracts"
        " WHERE archived=0 AND doctext IS NOT NULL AND doctext != ''"
        " ORDER BY id"))
    con.close()

    if not rows:
        print("no contracts with stored text")
        return 0

    total = 0
    for r in rows:
        text = r["doctext"]
        # Rebuild the ParsedDoc from stored text. Single page boundary: page
        # offsets were not persisted, and inventing them would put wrong page
        # numbers on real quotes -- better one honest page than a plausible lie.
        doc = ingest.ParsedDoc(
            text=text,
            pages=[ingest.Page(number=1, text=text, char_start=0)],
            fmt=r["fmt"] or "txt")
        try:
            n = retriever.index(r["id"], doc)
        except Exception as exc:                          # noqa: BLE001
            print(f"  [{r['id']:>2}] {r['filename'][:40]:<42} FAILED: {exc}")
            continue
        got = 0 if n is None else n
        total += got
        print(f"  [{r['id']:>2}] {r['filename'][:40]:<42} {got} chunk(s)")

    print(f"\nindexed {len(rows)} contract(s), {total} chunk(s) total")

    # Prove it: a corpus-wide query must now return passages.
    probe = retriever.retrieve("renewal notice period", k=5)
    print(f"probe 'renewal notice period' -> {len(probe)} passage(s)")
    if probe:
        p = probe[0]
        print(f"  top: contract {p.contract_id} page {p.page} "
              f"score {p.score:.3f}")
    return 0 if probe else 1


if __name__ == "__main__":
    sys.exit(main())
