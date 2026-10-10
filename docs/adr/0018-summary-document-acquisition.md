# 0018 — 510(k) Summary acquisition: per-page text in an append-only bronze table

**Status:** Accepted · **Date:** 2026-10-10
**Relates to:** [ADR 0004](0004-config-driven-table-resolution.md), [ADR 0008](0008-generated-spark-schemas.md), [ADR 0009](0009-fda-ai-list-acquisition-verified.md), [ADR 0010](0010-openfda-client.md), [ADR 0013](0013-openfda-enrichment-architecture.md) Decision 4 · [finding 0011](../../findings/0011-pdf-spike.md) · [finding 0018](../../findings/0018-summary-acquisition-built.md) · roadmap Issue 4, PR 4A ([handoff](../handoffs/issue-4-document-pass.md))

## Context

ADR 0013 Decision 4 left predicate lineage, PCCP and the cybersecurity statement
`None` because they live in the 510(k) Summary PDF, not openFDA. Finding 0011 then
measured 60 Summaries. 100% fetched, 59/60 had a text layer, and 57/60 gave up a
predicate number to a regex. So the pass is fetch-and-parse work, not OCR. Four
things had to be decided before writing it:

1. Where acquisition ends and extraction begins.
2. What bronze stores: text, PDF bytes, or both. Also its grain.
3. Which text extractor to use. The spike used pdf.js, which is not a Python library.
4. Which submissions to fetch. The spike verified the URL only for `K…` numbers.

One constraint shapes all four. `accessdata.fda.gov` is blocked from every agent
environment and from CI. Fetching happens on the maintainer's Mac, or another
network-permitted host. Everything else should be able to happen anywhere.

## Decision 1 — Acquisition is a separate step that stores text

`registry fetch-summaries` (`ingest/summary_documents.py`) is the only code that
requests `accessdata.fda.gov`. It writes `bronze_summary_documents`. Extraction
(4B onwards) reads that table and never touches the network. Every extractor can
then iterate offline, in any session, against real text, and re-running
extraction needs no re-fetch. The split mirrors ADR 0013, where enrichment is a
deliberate remote act and the silver rebuild is offline and free.

Bronze holds the text layer **verbatim**. That includes the glyph splitting finding
0011 documents ("Noti fi cation", "K 2 5 3 6 2 8"). Normalisation is an extraction
concern (4B), and only an un-normalised copy lets 4B change its normalisation
without re-fetching.

## Decision 2 — One row per fetch, page text in parallel arrays, append-only

The schema is generated from `schemas.BronzeSummaryDocumentRecord` (ADR 0008). It
has one row per document fetch:

- **Document:** `submission_number`, `url` (the URL that served the PDF, or the
  last one tried on a miss), `urls_tried`, `http_status`, `content_type`,
  `content_sha256` (of the PDF bytes), `byte_count`, `page_count`, `text_class`
  (`text` | `mixed` | `image`), `extractor`, `extraction_error`, `fetched_at`.
- **Pages:** `page_texts` and `page_char_counts`, where index *i* is page *i + 1*.
  The model validator ties both lengths to `page_count`, so the arrays cannot
  drift apart. Parallel scalar arrays fit the existing schema generator unchanged.
  A nested page struct would have meant extending ADR 0008's generator for one
  table.
- **Stamps:** `ingested_at` (write time) and `source_snapshot_id`. As in
  `bronze_fda_ai_list`, the snapshot id is the content hash of what the source
  served: here, the first 16 hex characters of the PDF's sha256.

The table is **append-only**. A miss (no PDF on either candidate URL) is written
too, with `content_sha256` null. The finding needs the success rate, and a miss
is a fact. Deduplication is by **(submission, sha256)**. A re-fetch whose bytes
match a row already in bronze appends nothing. A re-fetch whose bytes changed is
a new row, so the history of what the FDA served is kept.

`text_class` is finding 0011's rule. A page is an image page below 100
non-whitespace characters. A document is `text` when every page clears that bar,
`image` when none does, and `mixed` otherwise. `None` means no page could be read:
a miss, or bytes `pypdf` could not open, with the reason in `extraction_error`.
A single page `pypdf` fails on is stored as `""` (counted as an image page) and
named in `extraction_error` ("unreadable page 2 (…)"), so one bad page never
costs the rest of the document's text. Because the per-page counts are stored, the rule can be re-applied without a fetch.

## Decision 3 — PDF bytes are optional, on disk, keyed by hash; never in git or Delta

Bronze stores text, not bytes. If `REGISTRY_SUMMARY_PDF_ARCHIVE_DIR` is set, the
fetcher also writes each PDF to `<dir>/<sha256>.pdf`. The write is atomic, and a
failure is logged without failing the fetch. The default is unset. The archive
exists so that a different extractor can be run over the same bytes without
~1,541 more requests to the FDA. It is not a source of truth, it is not a Delta
table (ADR 0010's reasoning about the openFDA cache applies), and PDFs never go
in git (handoff §2). Durable storage for it waits on ADR 0011.

## Decision 4 — `pypdf` extracts the text, and the library is stamped on every row

The extractor is `pypdf`: pure Python, no system dependencies, and one small
wheel in the Docker image. We chose it over `pdfminer.six`, which recovers word
spacing better but is heavier. The spacing is the less important property here.
4B normalises (or de-spaces) before it matches anything, so a library's spacing
behaviour matters less than its page splitting and its handling of scanned pages.
The PR 4A fixture recording re-measures `pypdf` on the real slice. Its manifest
records, per document, how many K-numbers a naive regex finds on the raw layer and
how many it finds on a de-spaced copy. If that measurement shows `pypdf` losing
text that pdf.js recovered, 4B can switch libraries, re-extracting from the PDF
archive (Decision 3) rather than re-fetching. Every row's `extractor` column
(`pypdf==<version>`) tells the two extractions apart.

## Decision 5 — Only `K…` Summaries are fetched; De Novo and PMA are deferred

The URL rule `cdrh_docs/pdf{int(yy)}/<K>.pdf`, with a fallback to bare
`cdrh_docs/pdf/<K>.pdf`, is verified for 510(k)s only. `DEN…` and `P…` documents
live under other paths that nobody has checked. Guessing those paths would make
404s look like "no document". So the pass:

- targets submissions that `silver_device_enrichment.statement_or_summary` says
  filed a **Summary** and that have a `K…` number;
- counts Summary-flagged `DEN…`/`P…` filings as **deferred** and prints the
  count, so the gap is a number, not a silence;
- refuses a `DEN…`/`P…` number passed through `--only`, again as deferred.

Verifying the De Novo and PMA paths is a later, deliberate step with its own
evidence. Statement filers have no public document and are not targets.

## Decision 6 — Citizenship and resumability

- **Host from settings.** `REGISTRY_SUMMARY_DOCUMENTS_BASE_URL` sets the host, and
  the `User-Agent` comes from `http_user_agent`. No host is hard-coded in pipeline
  code.
- **Throttle.** `summary_request_interval_seconds` (default 1.0) spaces **every**
  request: fallbacks and retries included.
- **Stop on 429/403.** Either status raises `FetchHaltedError` at once, with no
  retry. Retries exhausted on 5xx or network errors raise
  `SummarySourceUnavailableError`. Both stop the pass: the pending batch is
  written first, nothing further is requested, and the CLI exits 1 and says to
  re-run later.
- **Batches and resume.** Rows are appended every `summary_write_batch_size`
  documents (default 50), so a crash or halt loses at most one batch. A default
  run skips any submission that already has a PDF in bronze, so it resumes where
  the last one stopped. Misses are retried on the next run, which is a few
  requests, not a re-fetch of everything. `--only` names exact documents and
  re-fetches them; the (submission, sha256) dedupe keeps that from duplicating
  rows.
- **Content decides.** A candidate counts only if it is a PDF: the content type
  says `pdf`, or the body starts with `%PDF-`. A 200 HTML page at a `.pdf` path
  is treated as absent and the next candidate is tried. This is ADR 0009's rule
  applied to this source.

## Consequences

- The real-text fixtures cannot be recorded from the session that wrote this
  code. `tests/test_summary_documents.py::TestLiveRecording` (`live_network`)
  records the slice from handoff §2 into `tests/fixtures/summary_documents/`
  (text and a manifest, no PDFs). The per-document assertions over that slice
  skip, naming the missing file, and one guard test
  (`test_every_slice_document_is_recorded`) **fails** until all ten files and the
  manifest exist. CI is therefore red until the slice is committed, and no
  slice assertion can ever skip silently afterwards. **PR 4A merges only after the
  maintainer has recorded and committed it.**
- The weekly `live-network` CI job also runs the recording test. That job hits
  accessdata about eleven times at 1 request/second and doubles as a drift check on
  the URL rule. What it writes stays in the ephemeral runner.
- `registry inspect` gains a DOCUMENTS section: Summary filings with a PDF, misses
  on the latest fetch, and the `text_class` split next to the spike's
  51/8/1-of-60. A pass that fetched rows but obtained no PDF is reported as a
  problem.
- Bronze grows by roughly 1,541 documents × ~15 pages of text, a few tens of MB.
  That is negligible next to the PDFs it replaces.
- `silver_devices` is unchanged. Nothing reads the new table yet; 4B does.

## Alternatives considered

**Store the PDF bytes in Delta (a blob column or table).** Rejected. It makes
bronze roughly 50 times larger to keep data that only a re-extraction needs, and
the opt-in on-disk archive covers that need.

**One row per page.** Rejected. A miss or an unreadable PDF has no pages, so it
would need a sentinel row. Document-level facts would also repeat on every page,
and any of those copies could disagree.

**Extract at fetch time into silver directly.** Rejected. It ties every
extraction change to a re-fetch, which needs `fda.gov`, which agent sessions do
not have. The handoff's architecture separates the two for exactly this reason.

**Guess the De Novo and PMA document URLs.** Rejected for now (Decision 5). An
unverified URL makes a 404 look like an absence.
