# 0023 — Shipping LLM sensitivity / specificity: a per-device table, one figure per measure by a fixed rule

**Status:** Accepted · **Date:** 2026-11-07
**Relates to:** [ADR 0007](0007-two-stage-mortality-flag.md), [ADR 0012](0012-silver-schema-and-supplement-handling.md), [ADR 0014](0014-gold-mortality-mart.md), [ADR 0016](0016-leads-filter-pre-curation-signals.md), [ADR 0021](0021-device-intended-use.md), [ADR 0022](0022-llm-assisted-evidence-extraction.md) · [finding 0024](../../findings/0024-llm-extraction-framework.md), [finding 0025](../../findings/0025-performance-figures-shipped.md)

## Context

ADR 0022 lets a field from the LLM pass ship only when it clears a bar on a fresh gold
draw. On gold set v2, metrics did: 14 / 15 accepted values right (weighted ~91%), and
every rejected metric was wrong (finding 0024). The maintainer decided on 2026-10-22 to
ship sensitivity and specificity. Nothing else from the pass ships.

Two things had to be decided: where the figures live, and which figure a device gets
when its Summary reports several (overall and per subgroup, per vessel and per patient,
several datasets).

`EvidenceRecord` (`silver_evidence`) has had `reports_sensitivity_specificity`,
`sensitivity` and `specificity` waiting since Phase 2. But that table means "the
curator's judgements": one row per curated device (133), and its `intended_use_text` is
the curator's excerpt. The leads read that column as the curated `mortality_language`
source (ADR 0016, ADR 0021), and the mart joins on the table (ADR 0014). Writing
uncurated devices into it would give the leads a curated label on text no curator
wrote.

## Decision 1 — a per-device table; the curated rows get the figures by a join

`registry build-performance` writes `silver_device_performance`
(`DevicePerformanceRecord`, schema generated per ADR 0008). It has one row per document
that one LLM run answered, for every device the pass covers. Rebuilt (overwrite) from
`silver_llm_evidence`, offline.

`registry build-mart` joins it onto the curated rows: `EvidenceRecord`'s three fields
plus a new `performance_run_id` are filled for the curated devices that have a row. No
other `EvidenceRecord` field changes, and no uncurated device enters `silver_evidence`.
The figure's quote, page and basis stay on the performance row (`performance_run_id`
names it).

Chosen by the maintainer over two alternatives (below).

## Decision 2 — what counts

From one run (one prompt version, one model digest; default the current
`PROMPT_VERSION`), a metric row counts only if:

- the checks **accepted** it (flagged rows count, and mark the device for review);
- `metric` is `sensitivity` or `specificity`, in the `standalone` setting;
- the model attributed it to the **subject device** (`applies_to = subject_device`);
- the model marked it a **result** (`is_result = true`);
- it has its quote and page(s).

A run whose prompt never asked for attribution (`4d-spike-4`) therefore ships nothing.
The one accepted error in gold v2 was a predicate's figure credited to the subject
device, so attribution is the place to look when a figure is challenged.

## Decision 3 — one figure per measure (`perf.1`; amended to `perf.2` in review and `perf.3` after the spot check)

Distinct figures only (the same value twice is one candidate), in document order:

| `*_basis` | Rule |
|---|---|
| `single` | the only distinct figure |
| `overall` | of several, exactly one is not tied to a subgroup: no `context`, or a context made **only** of words naming the whole set ("overall", "all", "total", "pooled", "combined", "entire", "primary", "whole") and filler ("sites", "patients", "dataset", …). "Overall (all sites)" is overall; "all female" and "primary care" are subgroups. `perf.1` matched the word anywhere, which let a subgroup ship as an unflagged headline (PR #26 review). |
| `first_of_several` | otherwise the first in document order, among the overall figures if there are any, else among all; `needs_review` is set |

**Sensitivity and specificity are a pair** (`perf.3`). When a device has both, they
come from one context the two share (compared loosely: "per-vessel" is "Per vessel"):
the one overall shared context, else the only shared one, else the first shared one in
document order, with overall ones first, flagged. A measure chosen from a subgroup
context for the pair's sake is `first_of_several`, so it is flagged. With no shared
context the two are chosen apart and the device is flagged. `perf.2` chose them
independently, which on the real run paired one model's sensitivity with another
model's specificity (K200717, finding 0025): a figure pair no model has. A document's
rows must come from
one `run_id` (`build-evidence` keeps one response per document, digest and prompt); a
document mixing two runs is refused, while different documents from different batches of
the same prompt and digest are normal. Every chosen figure carries its
value as written, label, context, quote, page(s), basis and candidate count.

The rule is deliberately simple and visible. "First in document order" follows how
Summaries are written (the primary endpoint first, subgroups after), and every row the
rule has to guess on is flagged rather than silently decided.

## Decision 4 — scale and absence

Figures are stored as proportions (0–1, as `EvidenceRecord` already constrains them),
beside the figure as written. A figure over 1, or one written with `%` in its quote, is
a percentage. A result outside 0–1 is not a proportion and is dropped.

`reports_sensitivity_specificity` is **True** when either figure was chosen and **None**
otherwise, never False. Gold v2 measured the precision of accepted figures, not whether
a device with none truly reports none (ADR 0012: None is "not established", not "no").

## Consequences

- Every device the LLM pass covers gets a reviewable headline figure, with its quote and
  page. The curated devices also carry it in `silver_evidence`, and the gold mart (ADR
  0014) shows `reports_sensitivity_specificity`, `sensitivity`, `specificity` and
  `performance_run_id` on every confirmed row (None where no figure shipped).
- The leads, the curation report and the mart's filter are unchanged: `silver_evidence`
  still holds exactly the curated judgements.
- A new step in the Mac run order: `extract-evidence` → `build-evidence` →
  **`build-performance`** → `build-mart`.
- `needs_review` rows (flagged checks, or a guess between several figures) are where a
  reviewer starts. A later rule version can change the choice without a model call:
  bump `SELECTION_VERSION` and re-run `build-performance`.
- Precision is as measured on 15 accepted metrics: a true rate as low as ~70% is still
  consistent with gold v2 (finding 0024). The figures are evidence, and they never set
  `mortality_confirmed_flag`.

## Alternatives considered

- **Write every covered device into `silver_evidence`.** Rejected: the leads would label
  uncurated text "curated", and the table would stop meaning "judgements".
- **`EvidenceRecord` for the curated devices only.** Smallest change, but the figures
  would exist for 133 devices and nowhere else.
- **Columns on `silver_devices`.** Visible everywhere, but derived columns in the
  differ's input need an ADR 0020-style "never a movement" rule. A separate table keeps
  them out of the diff structurally.
- **Average or pick the highest of several figures.** Rejected: an average is a figure no
  document states, and the highest flatters the device. A stated figure, flagged when
  chosen among several, stays checkable.
