# 0021 — Device-level intended use: read verbatim from the Summary, first in `mortality_language`'s precedence

**Status:** Accepted · **Date:** 2026-10-11
**Supersedes:** [ADR 0016](0016-leads-filter-pre-curation-signals.md) **Decision 2, the `mortality_language` text precedence only** (curated text → product-code definition → device name). The rest of Decision 2 — the other signals, what counts as movement, which movements are leads — and the rest of ADR 0016 stand.
**Relates to:** [ADR 0007](0007-two-stage-mortality-flag.md), [ADR 0012](0012-silver-schema-and-supplement-handling.md), [ADR 0014](0014-gold-mortality-mart.md), [ADR 0019](0019-deterministic-summary-extraction.md), [ADR 0020](0020-pccp-lead-category.md) · [finding 0012](../../findings/0012-mortality-seed-curation.md), [finding 0014](../../findings/0014-stage1-keyword-widening.md), [finding 0019](../../findings/0019-full-scale-document-measures.md), [finding 0022](../../findings/0022-device-intended-use.md) · roadmap Issue 4, PR 4C ([handoff §3](../handoffs/issue-4-document-pass.md))

## Context

ADR 0016 Decision 2 named its own weak point: `mortality_language` runs the stage-1
keyword pass over curated intended-use text (only for the 133 curated devices), else
the openFDA product-code definition (shared by every device with that code), else the
device name. No device-specific statement existed for an uncurated device anywhere in
the pipeline.

The Summary PDF carries one. Finding 0019 found the Indications-for-Use wording
readable in 1,537 / 1,541 Summaries, and PR 4A/4B already hold what is needed to read
it: per-page text in `bronze_summary_documents`, and each page's role (FDA letter,
3881 form, Summary) in `silver_document_extraction`.

## Decision 1 — a separate pass that reuses 4B's page roles

`registry extract-intended-use` writes `silver_intended_use` (`IntendedUseRecord`, one
row per extracted Summary, schema generated per ADR 0008). It does **not** re-segment:
it rebuilds each document's page roles from the `letter_pages` / `ifu_form_pages` /
`summary_pages` 4B stored (`page_kinds_from_roles`), and reads the text from the bronze
fetch with the **same** `content_sha256` (joined on submission + sha), so the roles
always describe the text being read. Within the Summary pages it locates headings with
4B's own rules (`sections_for`, factored out of `segment`); only the page
classification is skipped. The row records the 4B `segmentation_version` it used and
its own `extractor_version` (`4c.1`).

A separate table rather than columns on `silver_document_extraction`: the pass is
versioned and re-run on its own, and 4B's yields stay attributable to 4B's rules.
Overwrite, like every silver table: bronze and 4B keep the history.

## Decision 2 — where the statement is read from, and in what order

First hit wins; a candidate needs at least 40 letters ("Intended Use /" and "See
Indications for Use below." are headings, not statements):

| `intended_use_basis` | Rule |
|---|---|
| `form_3881` | On every page 4B classed `ifu_form`: the text after "Indications for Use (Describe)" (or "Indications for Use:" on the pre-3881 page) up to "Type of Use", the Prescription / Over-The-Counter checkbox with its CFR cite, or the form's footer. Several form pages (an OTC and an Rx form, `K190013`) are joined by a blank line. |
| `form_estar` | The eSTAR form, which 4B classes as Summary text: after "Please provide your Indications for Use below." up to "Please select the types of uses" or the checkbox. |
| `summary_section` | The first "Indications for Use" / "Intended Use" section; adjacent such headings read as one ("Intended Use / Indications for Use: …"). It ends at the next heading, a field label ("Technology:", "Comparison"), the end of its page when the statement ends there, or 2,500 characters. |
| `not_found` | Readable form or Summary pages, no statement. Text `None`. |
| `no_text` | No readable form or Summary page (`K003301`, `K241847`). Text `None`. |

The form is the FDA's own field for the statement and the handoff's first choice; the
Summary repeats it, sometimes paraphrased or prefixed ("remains the same … as
follows:"). The checkbox counts only with its CFR cite because a statement may itself
say "intended for over-the-counter (OTC) use" (`K250507`).

The text is a span of the normalised page text (ADR 0019 Decision 2), verbatim of what
was matched, with its page(s) and the PDF's URL. `None` means "not read", never "no
intended use" (ADR 0012). Nothing here forms a judgement (ADR 0007).

## Decision 3 — the new `mortality_language` precedence

Most device-specific first:

1. **`summary_intended_use`** — the device's own statement (this ADR).
2. `intended_use_text` — curated, where a curator captured it.
3. `classification_definition` — the openFDA product-code definition.
4. `device_name`.

The device statement goes above the curated text because the curated text is a
truncated excerpt of the same statement (finding 0012: "truncated to the leading
sentence(s)"), copied from the pdf.js layer; the extracted statement is whole and
re-derivable. Precedence decides only which source is *named*: the signal fires if any
source hits, as before, so adding a source can add hits but never remove one.
`mortality_language_source` gains the value `summary_intended_use`; the column type is
unchanged, so `gold_device_leads` needs no schema change.

## Decision 4 — a tag, never a movement

The statement is derived from the document, so by PR #14's rule (ADR 0016 Decision 2,
"What counts as movement") it must never decide *whether* a device moved. Here that
holds structurally, not just by the differ's field list: the statement is **not a
`silver_devices` column**. `LeadContext` reads `silver_intended_use` at build time,
exactly as it reads the product-code definition, so the silver diff never sees it.
Populating it for the ~1,537 existing devices therefore records no leads, however much
mortality language the statements carry; `test_leads.py::TestSummaryIntendedUse`
pins that, and pins that over the fixture pair the leads are exactly the movements.

It can make a movement that has no other signal into a lead (a `changed` or `removed`
device whose own statement speaks of mortality). That is the signal doing its job, as
for `pccp` (ADR 0020).

## Decision 5 — offered to curation, and measured against it

`registry curation-candidates [--csv PATH]` lists cardiovascular/metabolic devices
(the taxonomy's `mortality_relevant_categories`) whose statement trips the stage-1
pass but which `config/mortality_seed.yaml` has not reviewed — the next batch, newest
first, each with the matched terms, the statement, its page(s) and its PDF. It writes
nothing to the seed: a candidate is "read this", not "this is one".

The same command measures stage 1 on the curated devices: `keyword_disagrees`
(confirmed, stage 1 misses) and negatives flagged, on the curator's excerpt and on the
device's statement side by side, so finding 0014's baseline (0 / 11, 1 negative) is
read against the full text. `registry inspect` adds an INTENDED USE section: coverage
against finding 0019's 99.7%, and how many devices' (and recorded leads')
`mortality_language_source` moves to `summary_intended_use`.

## Consequences

- `mortality_language` stops being name-and-product-code-only for uncurated devices
  with a Summary (~1,537 of 1,614). Devices without one (De Novo, PMA, Statement
  filers, the scanned tail) keep the old sources.
- More text means more stage-1 hits, including on negatives: a full statement can say
  "risk of death" in a caveat. Leads are recall-oriented (ADR 0016 Decision 1), so this
  is accepted; the curated-negatives line of the measure keeps the cost visible.
- A new monitored device is often in silver before its Summary is fetched (ADR 0020's
  consequence holds here too): its first lead reads the old sources. The candidates
  report, not the leads, is where a late statement shows up.
- Two more steps in the Mac run order: `extract-documents` → **`extract-intended-use`**
  → `monitor`; **`curation-candidates`** whenever a curation batch is planned.

## Alternatives considered

- **Re-segment inside the new pass.** Rejected: two classifications of the same pages
  could disagree, and the brief asked to reuse 4B's.
- **Columns on `silver_document_extraction`.** Simpler join, but it couples two rule
  sets under one version and forces a 4B re-run for every 4C rule change.
- **A `silver_devices` column.** Rejected: it would put a derived field in the diff's
  input; Decision 4's guarantee would then rest on `SOURCE_FIELDS` alone.
- **Curated text first.** It is a human-chosen excerpt, but of the same statement and
  shorter; and it exists for 133 devices only.
- **Summary section first.** It is sometimes a paraphrase or a comparison-table row;
  the form is the regulated field.
- **LLM extraction.** Unnecessary: the statement sits under fixed labels. The
  LLM-assisted pass (4D) is for study facts.
