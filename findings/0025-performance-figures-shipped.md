# 0025 — Sensitivity / specificity shipped from the LLM pass: built, not yet run on the Mac

**Date:** 2026-11-07 · **Status:** Built and tested offline. **Not yet run over the real
`4d.3` evidence**: that is in the maintainer's lakehouse on the Mac (step below).
· **Component:** [ADR 0023](../docs/adr/0023-shipping-llm-performance-figures.md),
following the maintainer's decision recorded in [finding 0024](0024-llm-extraction-framework.md)

## Verified (in this session, by the test suite)

- **Only accepted subject-device results count** (`tests/test_performance.py`). A row
  rejected by the checks, attributed to a predicate or an original device, not asked
  (`applies_to` None), marked a threshold (`is_result` false or None), from a reader
  study, or naming another measure never becomes a shipped figure.
- **A run that never asked for attribution ships nothing.** The recorded `4d-spike-4`
  slice has 236 accepted metrics over 43 documents. `select` gives 43 rows, none with a
  figure.
- **One figure per measure**, by `perf.2`: `single`, `overall` (one figure not tied to a
  subgroup beats subgroup figures), or `first_of_several` (flagged). Duplicate values
  count once. Sensitivity and specificity are chosen independently. A context counts as
  overall only when every word in it names the whole set ("all female" is a subgroup),
  and a document whose rows come from two runs is refused.
- **Scale.** 92.5 becomes 0.925; 0.93 with no `%` stays 0.93; "0.9%" becomes 0.009; 180
  is dropped.
- **Never False.** `reports_sensitivity_specificity` is True with a figure and None
  without one; the validator refuses False.
- **The curated rows get the figures by a join.** Through Spark, evidence goes to
  `silver_device_performance`, then into the curated row of `silver_evidence`. A curated
  device without a row keeps None, and an uncurated device never enters `silver_evidence`.

## Assumed (not verified here)

- The rule is tested on hand-built evidence rows: no `4d.3` answer is recorded in the
  fixtures, and only `4d.3` answers carry `applies_to` and `is_result`.
- How often `first_of_several` fires on real Summaries is unknown until the Mac run. Gold
  v2's metric rows suggest it will be common for documents with subgroup tables
  (K250151-style per-centre rows).

## To do (this finding is updated when it is done)

- [ ] On the Mac, over the `4d.3` run already in the lakehouse (56 documents):

  ```bash
  uv run registry build-performance -v
  uv run registry build-mart
  ```

  Record the counts printed (documents, with sensitivity, with specificity, basis split,
  needs review) here, and spot-check 5 `needs_review` rows against their PDFs.
- [ ] Record a few real `4d.3` responses as fixtures (like `responses/`), so the rule is
      also tested on real answers.

## How to re-check

```bash
uv run pytest -q tests/test_performance.py
```
