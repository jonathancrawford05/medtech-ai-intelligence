# 0019 — The full Summary fetch, measured: PCCP is in the documents after all

**Verified 2026-10-10** by reading the maintainer's local `bronze_summary_documents`
(the first full run of `registry fetch-summaries`, PR 4A / ADR 0018) with `deltalake`,
and the recorded fixture slice's `manifest.json`. Corrects [finding 0011](0011-pdf-spike.md)
on PCCP. Every number here is reproducible from bronze with the method stated.

## The fetch

| | Result |
|---|---|
| Summary-flagged submissions (enrichment) | 1,541, all `K…` (0 De Novo / PMA deferred) |
| PDFs in bronze | **1,541 / 1,541**, 0 misses, 0 extraction errors |
| Pages | 18,102 (median 10 per document) |
| `text_class` | 1,095 `text` (71%), **443 `mixed` (29%)**, 3 `image` (0.2%) |
| Wall time | ~38 min (02:40–03:18 UTC) at the 1 req/s throttle |

**`mixed` is 29% at full scale against 13% (8/60) in the spike.** The scanned pages are
never page 1: of 576 image pages in mixed documents, 547 sit mid-document and 29 on the
last page. Page 1 (the FDA letter) is always text. The Indications-for-Use wording is
readable somewhere in **1,537 / 1,541 (99.7%)** documents, so the scanned pages are not
blocking PR 4C.

## The text layer through `pypdf` (fixture manifest)

Finding 0011 warned that the pdf.js layer split glyphs badly. Through `pypdf==6.20.0` the
problem is much milder: a naive `\bK\d{6}\b` on the raw layer found the same K-numbers as
on a de-spaced copy in **9 of 10** fixture documents (`K092116`: 1 raw vs 2 de-spaced).
Normalisation stays required (4B), but as a safety net, not a rescue.

## Predicate and cybersecurity, at full scale

- **Predicate (upper-bound proxy):** another submission number appears *and* the word
  "predicate" appears in **1,508 / 1,541 (97.9%)**. The spike's precise regex got 57/60
  (95%). 4B's section-anchored extractor will land at or below the proxy; this is the
  ceiling to measure against.
- **Cybersecurity mentioned: 471 / 1,541 (30.6%)** against 8/60 (13%) in the spike. Every
  first mention is after page 2, so these are Summary content, not FDA-letter boilerplate.
  "Mentioned" is not yet "a cybersecurity statement is present" (e.g. "Cybersecurity: N/A");
  4B must define the presence rule and measure it.

## PCCP — finding 0011's 0/60 does not hold

**76 documents mention a Predetermined Change Control Plan.** Finding 0011 searched the
Summary body for a PCCP *heading* in 60 documents, few of them from 2024 onwards. The
reliable signal is elsewhere: the **FDA clearance letter**, bundled at the front of each
PDF, carries a formulaic sentence when a PCCP was cleared:

> "FDA's substantial equivalence determination also included the review and clearance
> of your Predetermined Change Control Plan (PCCP)."

| Signal | Documents | How to read it |
|---|---|---|
| Letter sentence (above) | **68** | Authoritative positive. Never co-occurs with a correction letter. |
| Summary-body mention, no letter sentence | 8 | Needs classification — see below |

The 8 Summary-body mentions are a mix that a rule must handle:

- **Corrections (negative):** `K231837`, `K233692` — FDA letters stating the original SE
  letter "inadvertently indicated" a PCCP; the submission did not include one.
- **Explicit negation:** `K253984` — "No Predetermined Change Control Plan is included".
- **Inherited:** `K252103` — "PCCP (cleared as part of K234009)": the plan belongs to an
  earlier clearance.
- **Positive in the Summary:** `K210484`, `K232699`, `K243449`, `K201992` (the last is a
  submission to *modify* a PCCP).

**PCCP adoption by decision year** (letter sentence / Summary documents that year):

| Year | 2022 | 2023 | 2024 | 2025 | 2026 (to date) |
|---|---|---|---|---|---|
| Cleared PCCP | 0 / 160 | 1 / 217 (0.5%) | 17 / 224 (7.6%) | 31 / 327 (9.5%) | 19 / 173 (11.0%) |

That rising share is precisely the leading indicator the roadmap names (Issue 3, "PCCP-
flagged devices"), and it was about to be written off.

## Consequences for the plan

- **Do not write the ADR "PCCP is absent from the public Summary"** that PR #11, the
  roadmap and the Issue 4 handoff called for. The 4B ADR amends ADR 0013 Decision 4 the
  other way: `has_pccp` **is** populated, from the clearance-letter sentence (positive),
  with the correction letter and explicit negations as negatives, and the remaining
  Summary-body mentions classified with page + method recorded.
- `pccp_summary` remains `None` for now: the letter states *that* a PCCP was cleared, not
  what it covers. Some Summaries describe it; extracting that is 4D (LLM-assisted) work.
- **ADR 0016 Decision 3 deferred the PCCP lead category for want of a source.** Once 4B
  populates `has_pccp`, a `pccp` lead category becomes possible; that is a new ADR
  superseding Decision 3's PCCP part (4B or 4C), not an edit.

## Verified vs assumed

- **Verified:** every count above, by regex over the stored text (method in each section).
- **Assumed:** that the letter sentence is the FDA's standard wording for every cleared
  PCCP in 510(k)s. A cleared PCCP whose letter page is scanned (mid-document scans exist)
  or worded differently would be missed; 4B should report Summary-body positives without
  the letter sentence (4 today) as the visible edge of that gap.
- **Not measured here:** predicate *precision* (the proxy above is recall-only) and the
  cybersecurity presence rule.
