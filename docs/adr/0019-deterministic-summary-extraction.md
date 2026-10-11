# 0019 — Deterministic Summary extraction: predicates, cybersecurity, and `has_pccp` from the clearance letter

**Status:** Accepted · **Date:** 2026-10-11
**Supersedes:** [ADR 0013](0013-openfda-enrichment-architecture.md) **Decision 4** (the PDF fields stay `None`). The rest of ADR 0013 stands.
**Relates to:** [ADR 0007](0007-two-stage-mortality-flag.md), [ADR 0012](0012-silver-schema-and-supplement-handling.md), [ADR 0014](0014-gold-mortality-mart.md), [ADR 0016](0016-leads-filter-pre-curation-signals.md), [ADR 0018](0018-summary-document-acquisition.md) · [finding 0011](../../findings/0011-pdf-spike.md), [finding 0019](../../findings/0019-full-scale-document-measures.md), [finding 0020](../../findings/0020-summary-extraction-built.md) · roadmap Issue 4, PR 4B ([handoff](../handoffs/issue-4-document-pass.md))

## Context

ADR 0013 Decision 4 left `predicate_submission_number`, `predicate_age_days`,
`has_pccp`, `pccp_summary` and `cybersecurity_statement_present` `None`: openFDA
does not carry them; the 510(k) Summary PDF does. PR 4A (ADR 0018) put every
Summary's per-page text into `bronze_summary_documents`; the full fetch got
1,541 / 1,541 (finding 0019). This ADR records how 4B reads those fields out.

Finding 0011 planned for PCCP to stay `None` ("0/60 by Summary heading") and for an
ADR saying so. Finding 0019 overturned that at full scale: the **FDA clearance
letter** bundled at the front of each PDF says so verbatim when a PCCP was cleared,
in 68 documents. That ADR is not written; this one takes the other direction.

Constraints that shape every decision here:

- **Evidence, not judgement** (ADR 0007/0014). Every extracted value carries a page,
  a method and a verbatim quote, and nothing here writes a mortality flag.
- **`None` ≠ `False`** (ADR 0012). A field the text cannot answer is `None`.
- **Offline and deterministic.** Extraction reads bronze only, so any session can
  re-run it, and a re-run gives the same rows.
- **The text layer lies a little** (finding 0011, 0018): split glyphs (`K0921 16`),
  ligatures (`ﬁ`), OCR-style digit misreads (`Kl41248`), and pages whose font
  encoding yields symbols, not words (`K141480`).

## Decision 1 — a separate `silver_document_extraction` table, joined by `build-silver`

`registry extract-documents` reads each submission's newest *found* fetch from
bronze and overwrites `silver_document_extraction` (one row per document,
`DocumentExtractionRecord`, schema generated per ADR 0008). It carries
`extractor_version`, the source fetch's `content_sha256` / `fetched_at`, the page
segmentation, all predicates (parallel arrays: number, page, method, section),
reference devices, and each flag with its basis/method, page and quote.

`build-silver` joins it like it joins enrichment (ADR 0013 Decision 1): a dict read
at build time, `None` for anything absent, the table optional. `pccp_summary` stays
`None` — the letter says *that* a PCCP was cleared, not what it covers (PR 4D).

Overwrite, not append: it is derived state, re-derivable from bronze, which keeps
the history.

## Decision 2 — normalise first; match on normalised text only

`normalise(page)`: NFKC (expands ligatures), fold dashes and curly quotes to ASCII,
collapse whitespace, then rebuild submission numbers in place — a prefix (`K`,
`DEN`, `P`) and six digit-like characters with at most one space between any two,
where `l`/`I` read as `1` and `O` as `0`, and at least four are true digits. Keyword
matching additionally tolerates a space or line-end hyphen inside a word (`phrase`).
Quotes are spans of the normalised text, so they are verbatim *of what was matched*.
A test runs the extractor on the raw layer and shows it getting `K203469` wrong.

## Decision 3 — segmentation by page role, then by label-style heading

Each page is one of: `fda_letter` (a salutation plus the `Re: K…` line or the FDA
letterhead; later pages by their `Page 2 - Name` / `K… - Name Page 2` header),
`ifu_form` (Form FDA 3881, or the older form asking for "510(k) Number (if known)"),
`summary`, or `unreadable` (fewer than 100 non-whitespace characters, or under 50%
letters — a font-encoding failure). Position is not used: older documents put the
letter *after* the Summary (`K092116`).

Within Summary pages, sections open at a heading phrase — Predicate Device(s),
Reference Device(s), Indications for Use, Intended Use, Substantial Equivalence,
Technological Characteristics, Device Description, Performance…, Conclusion(s).
Flattened text has no layout, so a phrase is a heading only when it starts with a
capital and the token before it is not a lowercase word or a determiner ("to the
predicate device" is prose; "Class II Predicate Device:" is a label; an all-caps
word belongs to the same heading).

## Decision 4 — predicates: all of them, plus a primary; `DeviceRecord` holds the primary

Excluding the subject's own number and anything under a Reference Device label:

| Method | Rule |
|---|---|
| `table` | three or more distinct numbers under one predicate label (`K181892` lists eight) |
| `section` | a number under a predicate label, up to the next heading (capped at 1,500 characters, never past the contiguous Summary pages) |
| `sentence` | a number in a Summary sentence that says "predicate" or "substantially equivalent to" |

A number is reported once, under its strongest method. **Primary:** the number in a
sentence saying "primary predicate" (`stated_primary`), else the first under a
predicate label (`first_in_section`), else the first sentence hit
(`first_in_sentence`); the basis is stored. `DeviceRecord.predicate_submission_number`
holds the primary; the full list lives in the extraction table.

`predicate_age_days` = device decision date − the primary predicate's decision date,
`None` if either is unknown or the span is negative. Predicate dates come from the
existing openFDA client (ADR 0010) via `registry enrich-predicates` into
`silver_predicate_dates` — an enrichment step on a network-permitted host, not a PDF
step; `build-silver` only reads it.

## Decision 5 — the cybersecurity presence rule

`cybersecurity_statement_present` is about the **Summary**, not the FDA letter or the
3881 form:

- `True` (`mention`) — any mention of cybersecurity (glyph-tolerant) in Summary text
  that is not itself negated.
- `False` (`negated`) — only negated mentions: followed by `N/A` / "not applicable" /
  "none" / "not required", or preceded by "no" / "without" within one word.
- `False` (`no_mention`) — readable Summary text, no mention.
- `None` (`no_text`) — no Summary page has a readable text layer (`K003301`; `K241847`,
  whose letter is text but whose Summary is scanned).

A `mixed` document with no mention reads `False`. Its scanned pages could hide a
statement; finding 0019 found those mid-document, and the finding reports how many
`no_mention` documents are `mixed` so the gap is visible rather than assumed away.

## Decision 6 — `has_pccp` from the FDA clearance letter (amends ADR 0013 Decision 4)

Precedence, first match wins:

1. **Correction letter → `False`** (`correction_letter`): "inadvertently" within 600
   characters of a PCCP mention. Checked first because a correction quotes the
   positive sentence it retracts (`K231837`, `K233692`).
2. **Clearance-letter sentence → `True`** (`clearance_letter`): "review and clearance of
   your/the" followed by "Predetermined Change Control Plan" or "PCCP".
3. **Classified Summary-body mentions:** any positive → `True` (`summary_statement`,
   e.g. `K201992`, a submission to modify a PCCP); else only inherited — "PCCP
   (cleared as part of K234009)" — → `False` (`summary_inherited`), the earlier
   number kept in `pccp_reference_submission`; else only negations — "No
   Predetermined Change Control Plan is included", "PCCP: N/A", "does not include a
   PCCP" — → `False` (`summary_negation`).
4. **No mention → `False`** (`no_mention`) when any page is readable; **`None`**
   (`no_text`) when none is.

`has_pccp` therefore means "this submission's clearance included a PCCP". An inherited
plan is `False` here because the plan was cleared with another submission; it is not
lost — the method and the reference number say what it is.

The schema enforces the provenance rule: `has_pccp` is `None` exactly when the method
is `no_text`, and any value read from text has a page.

## Consequences

- `silver_devices` gains real values in four columns for the ~1,541 Summary filers;
  every other device keeps `None`. `registry inspect` reports their coverage instead
  of "awaiting the document pass", and an EXTRACTION section prints the yields.
- These are **derived** fields: the monitor already treats a derived-only change as no
  movement (PR #14), and a test pins that populating them produces no leads. PR 4B+
  builds the `pccp` lead category on `has_pccp` (it supersedes ADR 0016 Decision 3's
  PCCP part there, not here).
- The letter sentence is assumed to be the FDA's standard wording (finding 0019). A
  cleared PCCP whose letter page is scanned, or worded differently, reads
  `no_mention`; the Summary-body positives without a letter sentence are the visible
  edge of that gap, and the finding reports them.
- Rule changes bump `EXTRACTOR_VERSION`; yields are always attributable to one rule set.

## Alternatives considered

- **PCCP absent, `has_pccp` stays `None`** (finding 0011's plan). Refuted by finding
  0019's 68 letter sentences.
- **PCCP by Summary heading.** The spike's method; it found 0/60 and misses the
  authoritative signal, which is in the FDA's own letter.
- **Treat an inherited plan as `True`.** It would conflate "this clearance included a
  PCCP" with "this device operates under one", and the 4B+ year table would count
  plans twice. The method column keeps the distinction available to a consumer.
- **`None` for a `mixed` document with no cybersecurity mention.** Honest about the
  scanned pages but would discard ~29% of documents (finding 0019) on a possibility;
  reporting the count was preferred.
- **Layout-aware parsing (pdfplumber tables).** Bronze stores text, not PDFs (ADR
  0018), and the flattened-text rules recover every predicate the spike recorded on
  the slice. Revisit if the full-scale yield falls well below the proxy ceiling.
- **LLM extraction now.** Right for study facts (4D); unnecessary for numbers and
  presence flags, which regex reads with provenance at no cost.
