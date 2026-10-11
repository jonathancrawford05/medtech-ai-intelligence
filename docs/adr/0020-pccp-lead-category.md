# 0020 — PCCP becomes a lead category: a signal from silver's `has_pccp`, never a trigger

**Status:** Accepted · **Date:** 2026-10-11
**Supersedes:** [ADR 0016](0016-leads-filter-pre-curation-signals.md) **Decision 3, the PCCP part only** (the deferral). The foundation-model deferral in that decision, and the rest of ADR 0016, stand.
**Relates to:** [ADR 0012](0012-silver-schema-and-supplement-handling.md), [ADR 0017](0017-leads-output-surface.md), [ADR 0019](0019-deterministic-summary-extraction.md) · [finding 0019](../../findings/0019-full-scale-document-measures.md), [finding 0020](../../findings/0020-summary-extraction-built.md) · roadmap Issue 3/4, PR 4B+ ([handoff §3](../handoffs/issue-4-document-pass.md))

## Context

ADR 0016 Decision 3 deferred the `pccp` lead category because no source carried
PCCP: finding 0011 found 0/60 Summary headings, and openFDA has no field. An
always-empty column would read as "none found", a claim nobody had measured.

That reason no longer holds. Finding 0019 found the FDA clearance letter states a
cleared PCCP verbatim, and ADR 0019 (PR 4B) populates `silver_devices.has_pccp`
from it: 68 clearance-letter positives and 5 classified Summary positives over the
1,541 Summary filers, `False` for corrections, negations, inherited plans and no
mention, `None` where nothing was read. Finding 0019 also measured the trend that
makes PCCP a leading indicator: 0 / 160 clearances in 2022, 11.0% of 2026's so far.

## Decision 1 — `pccp` is a live category; `signal_pccp` is its column

`pccp` moves from `leads.DEFERRED_CATEGORIES` to `LEAD_CATEGORIES`, so the
`LeadRecord` category validator accepts it and `registry monitor` and `inspect`
report its count. `LeadRecord` gains `signal_pccp: bool | None`, generated into
the Spark schema like every field (ADR 0008):

- `True` — `has_pccp` is true on either side of the movement (current or previous
  silver row). A lead is tagged `pccp` exactly when this is true.
- `False` — read on at least one side and true on neither.
- `None` — `None` on both sides: no Summary was fetched or extracted. Never "no
  PCCP" (ADR 0012's distinction).

"Either side" follows ADR 0016 Decision 2: a device that was cleared with a
change-control plan leaving the list deserves the same look as one arriving.

## Decision 2 — a signal, never a movement trigger

`has_pccp` is derived from the document, not the FDA list, so by PR #14's rule
(ADR 0016 Decision 2, "What counts as movement") it never decides *whether* a
submission moved. `differ.SOURCE_FIELDS` is unchanged. A `has_pccp` difference
alone — the first build after `extract-documents`, or an extractor-version bump
that flips a value — is curation drift and records no lead; it is reported in
`changed_fields` only when a source field also moved.

Like `cardiometabolic`, the signal decides whether a `changed` or `removed`
movement is worth a lead; every `added` device is already one. Tests pin that
populating `has_pccp` for existing devices, and flipping it, record nothing.

## Decision 3 — the adoption trend is stock, shown by `registry inspect`

Leads only see movement, and the PCCP story is mostly in the 1,541 devices already
cleared. `registry inspect` therefore prints **PCCP share by decision year**,
recomputed on every run from `silver_document_extraction.pccp_method` joined to
`silver_devices.decision_date`:

| Column | Meaning |
|---|---|
| `documents` | extracted Summaries for devices decided that year (finding 0019's denominator) |
| `clearance_letter` | the FDA letter sentence, as `k / n (p%)` — finding 0019's table, cell for cell |
| `summary_statement` | Summary-body positives without a letter sentence, shown apart |
| `has_pccp (either)` | the two together, as `k / n (p%)` |

The two positives are kept apart so the letter column stays comparable with
finding 0019 and the Summary positives stay visible as the edge of the
letter-sentence gap (ADR 0019 Consequences). Negative methods and `no_text` count
only toward `documents`. A Summary whose device is no longer in silver has no
decision year; it is counted on a separate line, not dropped silently.

## Consequences

- New PCCP clearances surface as `new_submission` + `pccp` leads as soon as their
  Summary is fetched and extracted. Until then the signal is `None`: a new device
  usually reaches silver (from the FDA list) before its Summary is fetched, so the
  first lead for it will often carry `signal_pccp = None`. The trend table, not the
  leads, is where a late-arriving PCCP shows up.
- `gold_device_leads` gains a nullable column. The table already exists on the
  maintainer's lakehouse, so `registry monitor` now appends with Delta schema merge
  (additive only); rows written before this ADR read `signal_pccp = None`, which is
  correct: the signal was not computed for them. A test pins the widening append.
- `inspect`'s year table is verified here against synthetic rows shaped like
  finding 0019's counts; reproducing it from the real lakehouse is a run of
  `registry inspect` on the maintainer's Mac.

## Alternatives considered

- **Make `has_pccp` a movement trigger.** Rejected: the first extraction would
  turn every one of the 73 positives into a lead in an append-only table, and an
  extractor fix would do it again. That is the false movement PR #14 removed.
- **Tag `pccp` on the current side only.** Rejected for consistency with the other
  signals (ADR 0016 Decision 2) and because a PCCP device leaving the list matters.
- **Count only clearance letters in the trend.** Simpler, but it hides the Summary
  positives; **merging them into one column** would stop the table matching
  finding 0019. Both columns, plus their sum, keep both readings.
- **Wait for 4C before shipping PCCP.** Rejected 2026-10-11 on finding 0019's
  trend: the indicator is rising now and the source is ready.
