# 0018 — 510(k) Summary acquisition: built and tested offline; the live run is still open

**Date:** 2026-10-10 · **Status:** Built; **live verification open** · **Component:** Issue 4 PR 4A, `ingest/summary_documents.py`, `registry fetch-summaries` ([ADR 0018](../docs/adr/0018-summary-document-acquisition.md))

This finding says what is proven and what is not. The code was written in a cloud
session, and that session **cannot reach `accessdata.fda.gov`**. Every request
there fails at the egress proxy (`httpx.ProxyError: 403 Forbidden` when the
recording test runs). Nothing in this PR has yet touched a real Summary PDF.

## Verified (local Spark 4.0.1 / Delta 4.0.1 on host JDK 21, plus ruff; CI is authoritative)

- **URL rule.** Candidates are `pdf{int(yy)}/<K>.pdf` and then bare `pdf/<K>.pdf`.
  The year has no leading zero (`K093456` → `pdf9/`). `DEN…`/`P…` are refused as
  deferred.
- **Fetcher behaviour, against mocked HTTP.** The fallback to bare `pdf/` works,
  and a miss is recorded with its status and URLs. Content, not the URL, decides
  what counts as a PDF: an HTML 200 falls through to the next candidate, and
  `%PDF-` magic is accepted under a generic content type. A 429 or 403 halts at
  once with no retry. 5xx and network errors are retried, then raise. The throttle
  spaces every request, fallbacks included. Bytes `pypdf` cannot parse are kept
  as a hash plus an `extraction_error`. The optional PDF archive writes atomically
  and never fails the fetch.
- **Text measurement.** Per-page non-whitespace counts and the `text`/`mixed`/
  `image` rule reproduce the spike's profiles: `K003301` all zeros → `image`;
  `K203469` → `mixed`; `K182875`'s 93-character page → `mixed`. These checks run
  on synthetic PDFs from `tests/pdf_builder.py`, which test the mechanics only.
- **Bronze.** The schema is generated with field parity. The append-only write
  works. A second run resumes without re-fetching. A `--only` re-fetch of
  unchanged bytes appends nothing. The page text round-trips through Delta, and
  the history shows only `WRITE` operations.
- **CLI and `inspect`.** The operator messages are tested, a halt exits 1, and the
  DOCUMENTS section judges each document by its latest fetch.

## Not verified — needs a network-permitted host (the maintainer's Mac)

1. **The real fixture slice.** Run `uv run pytest -m live_network -k summary`, then
   commit `tests/fixtures/summary_documents/`. Until that happens, the 47
   real-text assertions in `TestRecordedSlice` skip. **PR 4A should not merge
   before this.** It is also the first evidence that the fetcher works against
   the real host.
2. **`pypdf` vs pdf.js.** The spike measured with pdf.js. `manifest.json` records
   raw vs de-spaced K-number counts per document; compare them with the spike's
   predicate column for the same ten documents. If `K181892` does not yield all
   eight predicates de-spaced, 4B should re-think the extractor before building
   on it (ADR 0018 Decision 4).
3. **The full-scale fetch.** After merge, run `uv run registry fetch-summaries -v`,
   which is about 1,541 documents at 1/s, roughly 30 minutes. Then run
   `uv run registry inspect` and fill in this table:

| Measure | Spike (60) | Full scale |
|---|---|---|
| Summary filings with a PDF | 60/60 | _pending_ |
| `text` / `mixed` / `image` | 51 / 8 / 1 | _pending_ |
| Misses (no PDF on either candidate) | 0 | _pending_ |
| Deferred De Novo/PMA Summary filings | n/a | _pending_ |
| Halted on 429/403? | never tripped | _pending_ |

Record the result here, or in a new finding that closes this one, along with
anything the run surfaces: a third URL shape, unreadable PDFs, HTML served at a
`.pdf` path.
