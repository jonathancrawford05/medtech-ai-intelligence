# PCCP Summary fixtures — provenance

**Status: recorded 2026-10-11 16:06 UTC** by the maintainer on the Mac, which also
holds the full `bronze_summary_documents` ([finding 0019](../../../../findings/0019-full-scale-document-measures.md)).
`accessdata.fda.gov` is blocked from agent sessions and CI, so re-recording happens
only on such a host:

```bash
uv run pytest -m live_network -k pccp
git add tests/fixtures/summary_documents/pccp && git commit -m "Record PCCP fixtures"
```

`TestRecordedPccpCases` runs against these in CI. The first recording caught a rule
bug: `K252103` read as a positive under extractor `4b.1` and is fixed in `4b.2`
([finding 0020](../../../../findings/0020-summary-extraction-built.md)). Note that
`manifest.json`'s yields were computed by `4b.1`.

What gets recorded (`tests/test_document_extraction.py::TestLivePccpRecording`), one
JSON per document in the same shape as the 4A slice one level up (text, never PDFs):

- **Finding 0019's named cases:** `K231837`, `K233692` (FDA correction letters),
  `K253984` (explicit negation), `K252103` (PCCP inherited from `K234009`),
  `K210484`, `K232699`, `K243449`, `K201992` (Summary-body positives without the
  letter sentence).
- **Three clearance-letter positives,** picked by the extractor from the local full
  bronze (the earliest, middle and latest submission number with the letter
  sentence), then re-fetched live. Their numbers are in `manifest.json`.

`manifest.json` also carries the **full-scale yields** and the full-scale list of
documents per non-trivial PCCP method, so finding 0020's numbers are committed with
the fixtures that back them.
