# 510(k) Summary fixtures — provenance

**Status: not yet recorded.** `accessdata.fda.gov` is blocked from agent sessions
and CI, so PR 4A shipped the recorder rather than the recording
([ADR 0018](../../../docs/adr/0018-summary-document-acquisition.md)). Until the
JSON files below exist, `TestRecordedSlice` in `tests/test_summary_documents.py`
skips, naming the missing file.

## How to record (on a network-permitted host, e.g. the maintainer's Mac)

```bash
uv run pytest -m live_network -k summary      # ~11 requests at ~1/s; stops on 429/403
uv run pytest -q tests/test_summary_documents.py   # the offline assertions now run
git add tests/fixtures/summary_documents && git commit -m "Record real summary fixtures"
```

Then replace the status line above with the recording date and anything surprising
in `manifest.json` (in particular the glyph-splitting re-measure: `k_numbers_raw`
vs `k_numbers_despaced` per document).

## What gets written

- `<K-number>.json`: one bronze row (`BronzeSummaryDocumentRecord`), with the
  verbatim per-page text from `pypdf`, the per-page character counts, the
  `text_class`, the URL that served it and the PDF's sha256. The recording time is
  stored as `fetched_at` / `ingested_at`.
- `manifest.json`: the recording time, the extractor version, and per document why
  it is in the slice plus the raw vs de-spaced K-number counts.
- **No PDFs.** Text only. `test_no_pdf_bytes_are_committed` enforces this.

## The slice (handoff §2, finding 0011)

| Submission | Why |
|---|---|
| `K003301` | Scanned, bare `pdf/` (2000): pins the `image` class and the URL fallback |
| `K181892` | 8-predicate table, the hardest predicate layout |
| `K243239` | Cybersecurity clause |
| `K141480` | Predicate named without a parsable number |
| `K241847` | Predicate named without a parsable number; scanned tail (`mixed`) |
| `K092116` | Ordinary 2010; `pdf9/` (no leading zero) |
| `K141922` | Ordinary 2015 |
| `K190013` | Ordinary 2019; labelled predicate plus reference device; cybersecurity |
| `K203469` | Ordinary 2021; `mixed` |
| `K250177` | Ordinary 2025; labelled `Predicate Device` field |
