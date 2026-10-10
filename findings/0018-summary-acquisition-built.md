# 0018 — 510(k) Summary acquisition: built, verified on the real slice; full-scale run open

**Date:** 2026-10-10 · **Status:** Built; real slice recorded and verified; **full-scale fetch open** · **Component:** Issue 4 PR 4A, `ingest/summary_documents.py`, `registry fetch-summaries` ([ADR 0018](../docs/adr/0018-summary-document-acquisition.md))

This finding says what is proven and what is not. The code was written in a cloud
session, and that session **cannot reach `accessdata.fda.gov`**. Every request
there fails at the egress proxy (`httpx.ProxyError: 403 Forbidden` when the
recording test runs). The maintainer therefore recorded the real fixture slice on a
network-permitted host, and that recording is the live evidence below.

## Verified (local Spark 4.0.1 / Delta 4.0.1 on host JDK 21, plus ruff; CI is authoritative)

- **URL rule.** Candidates are `pdf{int(yy)}/<K>.pdf` and then bare `pdf/<K>.pdf`.
  The year has no leading zero (`K093456` → `pdf9/`). `DEN…`/`P…` are refused as
  deferred.
- **Fetcher behaviour, against mocked HTTP.** The fallback to bare `pdf/` works,
  and a miss is recorded with its status and URLs. Content, not the URL, decides
  what counts as a PDF: an HTML 200 falls through to the next candidate, and
  `%PDF-` magic is accepted under a generic content type. A 429 or 403 halts at
  once with no retry. 5xx and network errors are retried, then raise. The throttle
  spaces every request, fallbacks included. Bytes `pypdf` cannot open are kept
  as a hash plus an `extraction_error`. A single page `pypdf` fails on becomes
  `""` and is named in `extraction_error`; the other pages keep their text. The optional PDF archive writes atomically
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

## Verified live: the real fixture slice (recorded 2026-10-10 21:57 UTC, maintainer's Mac)

`uv run pytest -m live_network -k summary` fetched the ten-document slice from
`accessdata.fda.gov` through `SummaryFetcher` itself, with the throttle on and no
429/403. The fixtures are committed in `tests/fixtures/summary_documents/`, and
all 49 `TestRecordedSlice` assertions pass in CI against them.

- **URL rule holds.** Each document resolved on its expected directory. That
  includes `pdf9/` for `K092116` (no leading zero) and the bare `pdf/` fallback for
  `K003301`.
- **pypdf vs pdf.js: same measurements.** Per-page non-whitespace counts are
  identical to the spike's for `K181892` (`[2144, 1596, 2125, 1479, 1687, 1909]`),
  `K203469` and `K003301` (all zeros). `K241847` has the same total (4,903). All ten
  `text_class` values match the spike: 7 `text`, 2 `mixed` (`K203469`, `K241847`)
  and 1 `image` (`K003301`). No document recorded an
  `extraction_error`.
- **Predicates are in the text layer.** `K181892` yields all eight predicate
  K-numbers from the spike once de-spaced. `K141922`, `K190013` and `K250177` yield
  their known predicates. `K243239` and `K190013` carry the cybersecurity clause.
- **Glyph splitting survives pypdf, but rarely.** `manifest.json` counts
  K-numbers raw vs de-spaced. Only `K092116` differs (1 vs 2): its own number
  appears in the raw layer as `K0921 16`. That is enough to confirm finding 0011's
  rule that 4B must match on normalised text. pypdf stays the extractor (ADR 0018
  Decision 4); nothing here argues for switching.

## Not verified — needs a network-permitted host (the maintainer's Mac)

1. **The full-scale fetch.** After merge, run `uv run registry fetch-summaries -v`,
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
